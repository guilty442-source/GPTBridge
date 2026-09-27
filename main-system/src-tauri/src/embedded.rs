//! embedded.rs ??port of src-ui/main/embedded-browser.ts.
//!
//! In-app browser sessions backed by Tauri child webviews (WebView2) instead
//! of Electron BrowserView.  Sessions are created hidden, positioned only
//! after an explicit show with clamped bounds, and detached on window
//! minimize/hide/resize-boundary events ??same screen-pollution contract.
//!
//! Threading contract: the session registry lock is NEVER held while calling
//! into webview/window APIs ??those dispatch onto the main event loop, and a
//! caller holding the lock while waiting on the main thread deadlocks
//! against main-thread handlers that also need the lock.  All webview ops go
//! through ``run_on_main_thread`` so WebView2 HWND affinity is respected.

use std::collections::HashMap;
use std::sync::Mutex;
use std::sync::OnceLock;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, LogicalPosition, LogicalSize, Manager};
use tauri::{WebviewUrl, webview::WebviewBuilder};

use crate::bridge;

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct BrowserBounds {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

pub struct EmbeddedSession {
    pub id: String,
    pub owner_module: String,
    pub url: String,
    pub created_at_ms: i64,
    pub bounds: Option<BrowserBounds>,
    pub visible: bool,
    /// Child webview label once materialised inside the main window.
    pub webview_label: Option<String>,
}

pub struct EmbeddedState {
    pub sessions: HashMap<String, EmbeddedSession>,
    /// Pending execute results: key = request id, value slot filled by the
    /// loopback bridge when the evaluated script POSTs its result back.
    pub pending_results: HashMap<String, std::sync::mpsc::Sender<serde_json::Value>>,
}

impl EmbeddedState {
    fn new() -> Self {
        Self {
            sessions: HashMap::new(),
            pending_results: HashMap::new(),
        }
    }
}

pub fn embedded_state() -> &'static Mutex<EmbeddedState> {
    static STATE: OnceLock<Mutex<EmbeddedState>> = OnceLock::new();
    STATE.get_or_init(|| Mutex::new(EmbeddedState::new()))
}

fn main_window(app: &AppHandle) -> Option<tauri::Window> {
    app.get_window("main")
}

fn clamp_bounds(
    app: &AppHandle,
    bounds: BrowserBounds,
) -> Option<BrowserBounds> {
    let window = main_window(app)?;
    let size = window.inner_size().ok()?;
    let scale = window.scale_factor().unwrap_or(1.0);
    let content_w = size.to_logical::<f64>(scale).width;
    let content_h = size.to_logical::<f64>(scale).height;
    let x = bounds.x.round().max(0.0).min(content_w);
    let y = bounds.y.round().max(0.0).min(content_h);
    let width = bounds.width.round().max(0.0).min(content_w - x);
    let height = bounds.height.round().max(0.0).min(content_h - y);
    if width < 1.0 || height < 1.0 {
        return None;
    }
    Some(BrowserBounds { x, y, width, height })
}

fn webview_label(id: &str) -> String {
    format!("embedded-{id}")
}

/// Run a closure on the main event loop and wait for its result.
/// WebView2 child-webview operations have HWND thread affinity; dispatching
/// here keeps worker-thread callers (bridge handlers, IPC commands) safe.
fn on_main<R: Send + 'static>(
    app: &AppHandle,
    f: impl FnOnce(&AppHandle) -> R + Send + 'static,
) -> Result<R, String> {
    let (tx, rx) = std::sync::mpsc::channel::<Result<R, String>>();
    let handle = app.clone();
    app.run_on_main_thread(move || {
        let result =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| f(&handle)));
        let _ = tx.send(result.map_err(|_| "WEBVIEW_OP_PANICKED".to_string()));
    })
    .map_err(|e| format!("MAIN_DISPATCH_FAILED:{e}"))?;
    rx.recv_timeout(Duration::from_secs(15))
        .map_err(|_| "MAIN_DISPATCH_TIMEOUT".to_string())?
}

/// Detach (hide) a materialised webview on the main thread.
/// Caller must NOT hold the registry lock.
fn detach_webview_on_main(app: &AppHandle, label: &str) {
    let label = label.to_string();
    let _ = on_main(app, move |app| {
        if let Some(window) = app.get_window("main") {
            if let Some(webview) = crate::find_webview(&window, &label) {
                let _ = webview.hide();
            }
        }
    });
}

/// Called on window minimize/hide ??detach every visible session.
pub fn hide_all_sessions(app: &AppHandle) {
    let labels: Vec<String> = {
        let mut state = embedded_state().lock().unwrap();
        let mut labels = Vec::new();
        for session in state.sessions.values_mut() {
            if session.visible {
                if let Some(label) = &session.webview_label {
                    labels.push(label.clone());
                }
                session.visible = false;
            }
        }
        labels
    };
    for label in labels {
        detach_webview_on_main(app, &label);
    }
}

/// Re-clamp visible sessions after a resize (same contract as the Electron
/// resize handler): out-of-bounds sessions detach instead of overlaying.
/// Runs on the main thread (window event) ??safe to call webview ops inline.
pub fn on_window_resized(app: &AppHandle) {
    // Collect updates under the lock, apply afterwards.
    let updates: Vec<(String, String, Option<BrowserBounds>)> = {
        let mut state = embedded_state().lock().unwrap();
        let ids: Vec<String> = state.sessions.keys().cloned().collect();
        let mut out = Vec::new();
        for id in ids {
            let Some(session) = state.sessions.get_mut(&id) else {
                continue;
            };
            if !session.visible {
                continue;
            }
            let Some(bounds) = session.bounds else {
                session.visible = false;
                if let Some(label) = &session.webview_label {
                    out.push((id.clone(), label.clone(), None));
                }
                continue;
            };
            match clamp_bounds(app, bounds) {
                Some(clamped) => {
                    session.bounds = Some(clamped);
                    if let Some(label) = &session.webview_label {
                        out.push((id.clone(), label.clone(), Some(clamped)));
                    }
                }
                None => {
                    session.visible = false;
                    session.bounds = None;
                    if let Some(label) = &session.webview_label {
                        out.push((id.clone(), label.clone(), None));
                    }
                }
            }
        }
        out
    };
    // Already on the main thread when invoked from the window-event handler.
    if let Some(window) = main_window(app) {
        for (_id, label, bounds) in updates {
            if let Some(webview) = crate::find_webview(&window, &label) {
                match bounds {
                    Some(b) => {
                        let _ = webview.set_position(LogicalPosition::new(b.x, b.y));
                        let _ = webview.set_size(LogicalSize::new(b.width, b.height));
                    }
                    None => {
                        let _ = webview.hide();
                    }
                }
            }
        }
    }
}

fn ensure_webview(
    app: &AppHandle,
    session_id: &str,
    url: &str,
) -> Result<Option<String>, String> {
    let label = webview_label(session_id);
    let parsed_url = url
        .parse::<tauri::Url>()
        .map_err(|e| format!("INVALID_URL:{e}"))?;
    let label_clone = label.clone();
    on_main(app, move |app| {
        let Some(window) = app.get_window("main") else {
            return Err("MAIN_WINDOW_NOT_AVAILABLE".to_string());
        };
        if crate::find_webview(&window, &label_clone).is_some() {
            return Ok(Some(label_clone));
        }
        // Materialise the child webview immediately (hidden) so navigate/
        // execute work before any show ??mirrors the detached BrowserView
        // contract.
        let builder =
            WebviewBuilder::new(label_clone.clone(), WebviewUrl::External(parsed_url));
        let webview = window
            .add_child(
                builder,
                LogicalPosition::new(0.0, 0.0),
                LogicalSize::new(1.0, 1.0),
            )
            .map_err(|e| format!("WEBVIEW_CREATE_FAILED:{e}"))?;
        // Sessions materialise detached ??nothing is displayed until an
        // explicit show with clamped bounds (screen-pollution contract).
        let _ = webview.hide();
        Ok(Some(label_clone))
    })?
}

pub fn create_session(
    app: &AppHandle,
    id: String,
    owner_module: String,
    url: String,
    bounds: Option<BrowserBounds>,
) -> serde_json::Value {
    if main_window(app).is_none() {
        return serde_json::json!({"ok": false, "id": id, "url": url, "message": "MAIN_WINDOW_NOT_AVAILABLE"});
    }

    let existing_label = {
        let mut state = embedded_state().lock().unwrap();
        if let Some(existing) = state.sessions.get_mut(&id) {
            existing.url = url.clone();
            if let Some(b) = bounds {
                existing.bounds = clamp_bounds(app, b);
            }
            existing.webview_label.clone()
        } else {
            let created = EmbeddedSession {
                id: id.clone(),
                owner_module,
                url: url.clone(),
                created_at_ms: std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis() as i64)
                    .unwrap_or(0),
                bounds: bounds.and_then(|b| clamp_bounds(app, b)),
                visible: false,
                webview_label: None,
            };
            state.sessions.insert(id.clone(), created);
            None
        }
    };

    if let Some(label) = existing_label {
        // Existing session: navigate the live webview on the main thread.
        let parsed: Option<tauri::Url> = url.parse().ok();
        if let Some(parsed) = parsed {
            let _ = on_main(app, move |app| {
                if let Some(window) = app.get_window("main") {
                    if let Some(webview) = crate::find_webview(&window, &label) {
                        let _ = webview.navigate(parsed);
                    }
                }
            });
        }
        return serde_json::json!({"ok": true, "id": id, "url": url});
    }

    // Materialise immediately-but-hidden; creation failures degrade the
    // session record rather than fail-closing the caller (tool UIs create
    // ahead of showing, same as BrowserView).
    match ensure_webview(app, &id, &url) {
        Ok(label) => {
            embedded_state()
                .lock()
                .unwrap()
                .sessions
                .get_mut(&id)
                .map(|s| s.webview_label = label);
        }
        Err(err) => {
            embedded_state().lock().unwrap().sessions.remove(&id);
            return serde_json::json!({"ok": false, "id": id, "url": url, "message": err});
        }
    }
    serde_json::json!({"ok": true, "id": id, "url": url})
}

pub fn navigate_session(app: &AppHandle, id: &str, url: &str) -> serde_json::Value {
    let label = {
        let mut state = embedded_state().lock().unwrap();
        let Some(session) = state.sessions.get_mut(id) else {
            return serde_json::json!({"ok": false, "message": "SESSION_NOT_FOUND"});
        };
        session.url = url.to_string();
        session.webview_label.clone()
    };
    let parsed: Option<tauri::Url> = url.parse().ok();
    if let (Some(label), Some(parsed)) = (label, parsed) {
        let _ = on_main(app, move |app| {
            if let Some(window) = app.get_window("main") {
                if let Some(webview) = crate::find_webview(&window, &label) {
                    let _ = webview.navigate(parsed);
                }
            }
        });
    }
    serde_json::json!({"ok": true})
}

pub fn session_url(app: &AppHandle, id: &str) -> Option<String> {
    let (label, fallback) = {
        let state = embedded_state().lock().unwrap();
        let session = state.sessions.get(id)?;
        (session.webview_label.clone(), session.url.clone())
    };
    let label = label?;
    on_main(app, move |app| {
        app.get_window("main")
            .and_then(|w| crate::find_webview(&w, &label))
            .and_then(|wv| wv.url().ok().map(|u| u.to_string()))
    })
    .ok()
    .flatten()
    .or(Some(fallback))
}

/// Evaluate a script inside the session webview and return the JSON result.
///
/// ``Webview::eval`` does not return the script value, so the wrapper posts
/// the result to the loopback bridge (a ``text/plain`` simple request ??no
/// CORS preflight ??so the body always reaches the bridge even on
/// cross-origin pages).
pub fn execute_script(
    app: &AppHandle,
    id: &str,
    script: &str,
) -> Result<serde_json::Value, String> {
    let (label, request_id) = {
        let state = embedded_state().lock().unwrap();
        let Some(session) = state.sessions.get(id) else {
            return Err("SESSION_NOT_FOUND".to_string());
        };
        (
            session.webview_label.clone(),
            format!("exec-{}-{}", std::process::id(), now_nanos()),
        )
    };
    let Some(label) = label else {
        return Err("WEBVIEW_NOT_MATERIALISED".to_string());
    };

    let (tx, rx) = std::sync::mpsc::channel::<serde_json::Value>();
    embedded_state()
        .lock()
        .unwrap()
        .pending_results
        .insert(request_id.clone(), tx);

    let bridge_port = bridge::bridge_port();
    let bridge_token = bridge::bridge_token();
    let wrapper = format!(
        "Promise.resolve().then(function(){{return (function(){{ {script} }})();}}).then(function(r){{try{{var x=new XMLHttpRequest();x.open('POST','http://127.0.0.1:{bridge_port}/__exec_result',true);x.setRequestHeader('Content-Type','text/plain');x.send({token_json}+'\\n'+JSON.stringify({{request_id:{rid_json},result:r===undefined?null:r}}));}}catch(e){{}}}}).catch(function(e){{try{{var x=new XMLHttpRequest();x.open('POST','http://127.0.0.1:{bridge_port}/__exec_result',true);x.setRequestHeader('Content-Type','text/plain');x.send({token_json}+'\\n'+JSON.stringify({{request_id:{rid_json},error:String(e)}}));}}catch(e2){{}}}});",
        script = script,
        bridge_port = bridge_port,
        token_json = serde_json::to_string(&bridge_token).unwrap_or_default(),
        rid_json = serde_json::to_string(&request_id).unwrap_or_default(),
    );

    let eval_result = on_main(app, move |app| {
        app.get_window("main")
            .and_then(|w| crate::find_webview(&w, &label))
            .ok_or_else(|| "WEBVIEW_NOT_MATERIALISED".to_string())
            .and_then(|wv| wv.eval(&wrapper).map_err(|e| format!("EVAL_FAILED:{e}")))
    });
    match eval_result {
        Err(e) | Ok(Err(e)) => {
            embedded_state()
                .lock()
                .unwrap()
                .pending_results
                .remove(&request_id);
            return Err(e);
        }
        Ok(Ok(())) => {}
    }

    let result = rx
        .recv_timeout(Duration::from_secs(30))
        .map_err(|_| "EXECUTE_TIMEOUT".to_string());
    match result {
        Ok(payload) => {
            if let Some(error) = payload.get("error") {
                Err(error.as_str().unwrap_or("EVAL_ERROR").to_string())
            } else {
                Ok(payload.get("result").cloned().unwrap_or(serde_json::Value::Null))
            }
        }
        Err(e) => Err(e),
    }
}

fn now_nanos() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
}

/// Route a result delivered by the bridge back to a waiting execute call.
pub fn deliver_exec_result(request_id: &str, payload: serde_json::Value) -> bool {
    let tx = embedded_state()
        .lock()
        .unwrap()
        .pending_results
        .remove(request_id);
    match tx {
        Some(tx) => {
            let _ = tx.send(payload);
            true
        }
        None => false,
    }
}

pub fn resize_session(
    app: &AppHandle,
    id: &str,
    bounds: BrowserBounds,
) -> serde_json::Value {
    let (label, visible, clamped) = {
        let mut state = embedded_state().lock().unwrap();
        let Some(session) = state.sessions.get_mut(id) else {
            return serde_json::json!({"ok": false, "message": "SESSION_NOT_FOUND"});
        };
        match clamp_bounds(app, bounds) {
            None => {
                // Out of bounds: detach instead of overlaying.
                session.visible = false;
                session.bounds = None;
                (session.webview_label.clone(), false, None)
            }
            Some(clamped) => {
                session.bounds = Some(clamped);
                (session.webview_label.clone(), session.visible, Some(clamped))
            }
        }
    };
    if let Some(label) = label {
        if visible {
            if let Some(b) = clamped {
                let _ = on_main(app, move |app| {
                    if let Some(window) = app.get_window("main") {
                        if let Some(webview) = crate::find_webview(&window, &label) {
                            let _ = webview.set_position(LogicalPosition::new(b.x, b.y));
                            let _ = webview.set_size(LogicalSize::new(b.width, b.height));
                        }
                    }
                });
            }
        } else if clamped.is_none() {
            detach_webview_on_main(app, &label);
        }
    }
    serde_json::json!({"ok": true, "hidden": clamped.is_none()})
}

pub fn show_session(app: &AppHandle, id: &str) -> serde_json::Value {
    let (label, bounds) = {
        let mut state = embedded_state().lock().unwrap();
        let Some(session) = state.sessions.get_mut(id) else {
            return serde_json::json!({"ok": false, "message": "SESSION_NOT_FOUND"});
        };
        let Some(bounds) = session.bounds.and_then(|b| clamp_bounds(app, b)) else {
            return serde_json::json!({"ok": false, "message": "BROWSER_VIEW_BOUNDS_REQUIRED"});
        };
        session.bounds = Some(bounds);
        (session.webview_label.clone(), bounds)
    };

    let Some(label) = label else {
        return serde_json::json!({"ok": false, "message": "WEBVIEW_NOT_MATERIALISED"});
    };
    match on_main(app, move |app| {
        let Some(window) = app.get_window("main") else {
            return Err("MAIN_WINDOW_NOT_AVAILABLE".to_string());
        };
        let Some(webview) = crate::find_webview(&window, &label) else {
            return Err("WEBVIEW_NOT_MATERIALISED".to_string());
        };
        let _ = webview.set_position(LogicalPosition::new(bounds.x, bounds.y));
        let _ = webview.set_size(LogicalSize::new(bounds.width, bounds.height));
        let _ = webview.show();
        let _ = webview.set_focus();
        Ok(())
    }) {
        Err(e) | Ok(Err(e)) => {
            return serde_json::json!({"ok": false, "message": e});
        }
        Ok(Ok(())) => {}
    }
    embedded_state()
        .lock()
        .unwrap()
        .sessions
        .get_mut(id)
        .map(|s| s.visible = true);
    serde_json::json!({"ok": true, "bounds": bounds})
}

pub fn hide_session(app: &AppHandle, id: &str) -> serde_json::Value {
    let label = {
        let mut state = embedded_state().lock().unwrap();
        let Some(session) = state.sessions.get_mut(id) else {
            return serde_json::json!({"ok": false, "message": "SESSION_NOT_FOUND"});
        };
        session.visible = false;
        session.webview_label.clone()
    };
    if let Some(label) = label {
        detach_webview_on_main(app, &label);
    }
    serde_json::json!({"ok": true})
}

pub fn close_session(app: &AppHandle, id: &str) -> serde_json::Value {
    let label = {
        let mut state = embedded_state().lock().unwrap();
        state.sessions.remove(id).map(|s| s.webview_label).flatten()
    };
    if let Some(label) = label {
        let _ = on_main(app, move |app| {
            if let Some(window) = app.get_window("main") {
                if let Some(webview) = crate::find_webview(&window, &label) {
                    let _ = webview.close();
                }
            }
        });
    }
    serde_json::json!({"ok": true})
}

pub fn list_sessions() -> serde_json::Value {
    let state = embedded_state().lock().unwrap();
    let items: Vec<serde_json::Value> = state
        .sessions
        .values()
        .map(|s| {
            serde_json::json!({
                "id": s.id,
                "ownerModule": s.owner_module,
                "url": s.url,
                "createdAt": s.created_at_ms,
            })
        })
        .collect();
    serde_json::Value::Array(items)
}

pub fn close_module_sessions(app: &AppHandle, owner_module: &str) -> i64 {
    let ids: Vec<String> = {
        let state = embedded_state().lock().unwrap();
        state
            .sessions
            .values()
            .filter(|s| s.owner_module == owner_module)
            .map(|s| s.id.clone())
            .collect()
    };
    let mut count = 0i64;
    for id in ids {
        let _ = close_session(app, &id);
        count += 1;
    }
    count
}

pub fn close_all_sessions(app: &AppHandle) {
    let ids: Vec<String> = {
        embedded_state().lock().unwrap().sessions.keys().cloned().collect()
    };
    for id in ids {
        let _ = close_session(app, &id);
    }
}

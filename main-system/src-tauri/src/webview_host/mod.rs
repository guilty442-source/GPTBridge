//! webview_host — Tauri WebView Host layer (port of the retired
//! src-ui/main/embedded-browser.ts).
//!
//! Helper-process architecture: each session is hosted by a dedicated
//! ``gptbridge-shell.exe --embedded-worker`` process.  This machine's
//! WebView2 runtime wedges any host → controller call once a process owns
//! more than one controller (post-loop creation deadlocks in EBW.dll —
//! wry#1665/#583 class — and even ops on pre-built pool views stall
//! 5–60 s).  A worker owns exactly one controller — the reliable
//! first-controller path — so sessions spawn a worker, and this module
//! proxies every lifecycle operation to it over a token-guarded loopback
//! endpoint (``worker.rs`` / ``worker_client.rs``).
//!
//! The worker's window is reparented under the main window's HWND
//! (``parent_raw`` → WS_CHILD), so it is clipped to the parent client area
//! and moves/hides with it — BrowserView-equivalent containment.
//!
//! Sessions are created hidden, positioned only after an explicit show with
//! clamped bounds, and hidden on window minimize/resize-boundary events —
//! same screen-pollution contract as the Electron host.
//!
//! Threading contract: the session registry lock must NEVER be held across
//! a worker HTTP call or process spawn — those block for tens of ms to
//! seconds while main-thread window-event handlers also take the lock.

pub(crate) mod worker;

mod geometry;
mod worker_client;
mod worker_eval;
mod worker_server;

use std::collections::HashMap;
use std::sync::Mutex;
use std::sync::OnceLock;

use tauri::{AppHandle, Manager};

use geometry::{clamp_bounds, physical_bounds};
use worker_client::{spawn_worker, worker_hide, worker_request, worker_shutdown, WorkerRef};

pub use geometry::{current_content_size, record_content_size, BrowserBounds};

/// Find a child webview inside a window by label.
pub(crate) fn find_webview(window: &tauri::Window, label: &str) -> Option<tauri::Webview> {
    window
        .webviews()
        .into_iter()
        .find(|w| w.label() == label)
}

pub struct EmbeddedSession {
    pub id: String,
    pub owner_module: String,
    pub url: String,
    pub created_at_ms: i64,
    pub bounds: Option<BrowserBounds>,
    pub visible: bool,
    /// Live worker endpoint once materialised.
    pub worker: Option<WorkerRef>,
}

pub struct EmbeddedState {
    pub sessions: HashMap<String, EmbeddedSession>,
    /// Pending execute results retained for bridge compatibility (the
    /// worker hosts its own eval-result channel; nothing inserts here).
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

fn embedded_state() -> &'static Mutex<EmbeddedState> {
    static STATE: OnceLock<Mutex<EmbeddedState>> = OnceLock::new();
    STATE.get_or_init(|| Mutex::new(EmbeddedState::new()))
}

fn main_window(app: &AppHandle) -> Option<tauri::Window> {
    app.get_window("main")
}

/// Called on window minimize/hide — detach every visible session.  Child
/// windows also hide automatically with the parent; this keeps the
/// session `visible` flags truthful.  Runs on the main thread inside the
/// window-event handler, so the blocking worker calls are handed to a
/// helper thread — a wedged worker must never stall the event loop.
pub fn hide_all_sessions(_app: &AppHandle) {
    let workers: Vec<WorkerRef> = {
        let mut state = embedded_state().lock().unwrap();
        let mut workers = Vec::new();
        for session in state.sessions.values_mut() {
            if session.visible {
                if let Some(worker) = &session.worker {
                    workers.push(worker.clone());
                }
                session.visible = false;
            }
        }
        workers
    };
    if workers.is_empty() {
        return;
    }
    std::thread::spawn(move || {
        for worker in workers {
            worker_hide(&worker);
        }
    });
}

/// Re-clamp visible sessions after a resize (same contract as the Electron
/// resize handler): out-of-bounds sessions detach instead of overlaying.
/// Runs on the main thread inside the window-event handler — lock scope is
/// kept to the registry update; worker calls run afterwards.
pub fn on_window_resized(app: &AppHandle) {
    let updates: Vec<(WorkerRef, Option<BrowserBounds>)> = {
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
                if let Some(worker) = &session.worker {
                    out.push((worker.clone(), None));
                }
                continue;
            };
            match clamp_bounds(app, bounds) {
                Some(clamped) => {
                    session.bounds = Some(clamped);
                    if let Some(worker) = &session.worker {
                        out.push((worker.clone(), Some(clamped)));
                    }
                }
                None => {
                    session.visible = false;
                    session.bounds = None;
                    if let Some(worker) = &session.worker {
                        out.push((worker.clone(), None));
                    }
                }
            }
        }
        out
    };
    if updates.is_empty() {
        return;
    }
    // Blocking worker calls off the main thread — this handler runs inside
    // the event loop's Resized dispatch and must never stall it.
    std::thread::spawn(move || {
        for (worker, bounds) in updates {
            match bounds {
                Some(b) => {
                    let _ = worker_request(&worker, "POST", "/bounds", &physical_bounds(&b));
                }
                None => worker_hide(&worker),
            }
        }
    });
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

    // Clamp outside the lock — window interaction must never block while
    // the registry is held.
    let clamped = bounds.and_then(|b| clamp_bounds(app, b));

    let existing_worker = {
        let mut state = embedded_state().lock().unwrap();
        if let Some(existing) = state.sessions.get_mut(&id) {
            existing.url = url.clone();
            if bounds.is_some() {
                existing.bounds = clamped;
            }
            existing.worker.clone()
        } else {
            let created = EmbeddedSession {
                id: id.clone(),
                owner_module,
                url: url.clone(),
                created_at_ms: std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis() as i64)
                    .unwrap_or(0),
                bounds: clamped,
                visible: false,
                worker: None,
            };
            state.sessions.insert(id.clone(), created);
            None
        }
    };

    if let Some(worker) = existing_worker {
        // Existing session: navigate the live worker view.
        let _ = worker_request(
            &worker,
            "POST",
            "/navigate",
            &serde_json::json!({"url": url}),
        );
        return serde_json::json!({"ok": true, "id": id, "url": url});
    }

    // Materialise the worker process; failures degrade the session record
    // rather than fail-closing the caller (same as BrowserView).
    match spawn_worker(app, &id, &url) {
        Ok(worker) => {
            let mut state = embedded_state().lock().unwrap();
            if let Some(session) = state.sessions.get_mut(&id) {
                session.worker = Some(worker.clone());
                if let Some(b) = session.bounds {
                    drop(state);
                    let _ = worker_request(&worker, "POST", "/bounds", &physical_bounds(&b));
                }
            }
        }
        Err(err) => {
            embedded_state().lock().unwrap().sessions.remove(&id);
            return serde_json::json!({"ok": false, "id": id, "url": url, "message": err});
        }
    }
    serde_json::json!({"ok": true, "id": id, "url": url})
}

pub fn navigate_session(app: &AppHandle, id: &str, url: &str) -> serde_json::Value {
    let worker = {
        let mut state = embedded_state().lock().unwrap();
        let Some(session) = state.sessions.get_mut(id) else {
            return serde_json::json!({"ok": false, "message": "SESSION_NOT_FOUND"});
        };
        session.url = url.to_string();
        session.worker.clone()
    };
    if let Some(worker) = worker {
        let _ = worker_request(
            &worker,
            "POST",
            "/navigate",
            &serde_json::json!({"url": url}),
        );
    }
    let _ = app;
    serde_json::json!({"ok": true})
}

pub fn session_url(app: &AppHandle, id: &str) -> Option<String> {
    let (worker, fallback) = {
        let state = embedded_state().lock().unwrap();
        let session = state.sessions.get(id)?;
        (session.worker.clone(), session.url.clone())
    };
    let _ = app;
    // Live URL when materialised; otherwise the last requested URL.
    let live = worker.and_then(|w| {
        worker_request(&w, "GET", "/url", &serde_json::json!({}))
            .and_then(|v| v["url"].as_str().map(|s| s.to_string()))
            .filter(|s| !s.is_empty())
    });
    live.or(Some(fallback))
}

/// Evaluate a script inside the session webview and return the JSON result.
/// Proxied to the worker, which runs the eval and returns the value.
pub fn execute_script(
    app: &AppHandle,
    id: &str,
    script: &str,
) -> Result<serde_json::Value, String> {
    let worker = {
        let state = embedded_state().lock().unwrap();
        let Some(session) = state.sessions.get(id) else {
            return Err("SESSION_NOT_FOUND".to_string());
        };
        session.worker.clone()
    };
    let _ = app;
    let Some(worker) = worker else {
        return Err("WEBVIEW_NOT_MATERIALISED".to_string());
    };
    let response = worker_request(
        &worker,
        "POST",
        "/eval",
        &serde_json::json!({"script": script}),
    )
    .ok_or_else(|| "WORKER_UNREACHABLE".to_string())?;
    if response["ok"] == serde_json::Value::Bool(true) {
        Ok(response.get("result").cloned().unwrap_or(serde_json::Value::Null))
    } else {
        Err(response["message"]
            .as_str()
            .unwrap_or("EVAL_ERROR")
            .to_string())
    }
}

/// Route a result delivered by the bridge back to a waiting execute call.
/// Retained for bridge API compatibility; workers own their eval channel.
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
    // Clamp before locking (window interaction must not run under the lock).
    let clamped = clamp_bounds(app, bounds);
    let (worker, visible) = {
        let mut state = embedded_state().lock().unwrap();
        let Some(session) = state.sessions.get_mut(id) else {
            return serde_json::json!({"ok": false, "message": "SESSION_NOT_FOUND"});
        };
        match clamped {
            None => {
                // Out of bounds: detach instead of overlaying.
                session.visible = false;
                session.bounds = None;
                (session.worker.clone(), false)
            }
            Some(b) => {
                session.bounds = Some(b);
                (session.worker.clone(), session.visible)
            }
        }
    };
    if let Some(worker) = worker {
        if visible {
            if let Some(b) = clamped {
                let _ = worker_request(&worker, "POST", "/bounds", &physical_bounds(&b));
            }
        } else if clamped.is_none() {
            worker_hide(&worker);
        }
    }
    serde_json::json!({"ok": true, "hidden": clamped.is_none()})
}

pub fn show_session(app: &AppHandle, id: &str) -> serde_json::Value {
    // Read the stored bounds under the lock, clamp outside it.
    let raw_bounds = {
        let state = embedded_state().lock().unwrap();
        let Some(session) = state.sessions.get(id) else {
            return serde_json::json!({"ok": false, "message": "SESSION_NOT_FOUND"});
        };
        session.bounds
    };
    let Some(bounds) = raw_bounds.and_then(|b| clamp_bounds(app, b)) else {
        return serde_json::json!({"ok": false, "message": "BROWSER_VIEW_BOUNDS_REQUIRED"});
    };
    let worker = {
        let mut state = embedded_state().lock().unwrap();
        if let Some(session) = state.sessions.get_mut(id) {
            session.bounds = Some(bounds);
            session.worker.clone()
        } else {
            None
        }
    };
    let Some(worker) = worker else {
        return serde_json::json!({"ok": false, "message": "WEBVIEW_NOT_MATERIALISED"});
    };
    let _ = worker_request(&worker, "POST", "/bounds", &physical_bounds(&bounds));
    let _ = worker_request(&worker, "POST", "/show", &serde_json::json!({}));
    embedded_state()
        .lock()
        .unwrap()
        .sessions
        .get_mut(id)
        .map(|s| s.visible = true);
    serde_json::json!({"ok": true, "bounds": bounds})
}

pub fn hide_session(app: &AppHandle, id: &str) -> serde_json::Value {
    let worker = {
        let mut state = embedded_state().lock().unwrap();
        let Some(session) = state.sessions.get_mut(id) else {
            return serde_json::json!({"ok": false, "message": "SESSION_NOT_FOUND"});
        };
        session.visible = false;
        session.worker.clone()
    };
    let _ = app;
    if let Some(worker) = worker {
        worker_hide(&worker);
    }
    serde_json::json!({"ok": true})
}

pub fn close_session(app: &AppHandle, id: &str) -> serde_json::Value {
    let worker = {
        let mut state = embedded_state().lock().unwrap();
        state.sessions.remove(id).and_then(|s| s.worker)
    };
    let _ = app;
    if let Some(worker) = worker {
        worker_shutdown(&worker);
    }
    serde_json::json!({"ok": true})
}

/// Close every session owned by a module (module unload contract).
pub fn close_module_sessions(app: &AppHandle, owner_module: &str) -> usize {
    let workers: Vec<WorkerRef> = {
        let mut state = embedded_state().lock().unwrap();
        let ids: Vec<String> = state
            .sessions
            .values()
            .filter(|s| s.owner_module == owner_module)
            .map(|s| s.id.clone())
            .collect();
        let mut workers = Vec::new();
        for id in ids {
            if let Some(session) = state.sessions.remove(&id) {
                if let Some(worker) = session.worker {
                    workers.push(worker);
                }
            }
        }
        workers
    };
    let _ = app;
    let count = workers.len();
    for worker in workers {
        worker_shutdown(&worker);
    }
    count
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
                "visible": s.visible,
            })
        })
        .collect();
    serde_json::json!({"ok": true, "sessions": items})
}

/// Close every live session (window close / app shutdown).  Workers are
/// asked to exit then bounded-killed so a wedged worker never leaks.
/// Shutdown runs on a helper thread: serial per-worker waits must not
/// stall the main event loop on the close path, and the worker-side
/// parent-death watchdog remains the ultimate orphan backstop.
pub fn close_all_sessions(app: &AppHandle) {
    let workers: Vec<WorkerRef> = {
        let mut state = embedded_state().lock().unwrap();
        let mut workers = Vec::new();
        let ids: Vec<String> = state.sessions.keys().cloned().collect();
        for id in ids {
            if let Some(session) = state.sessions.remove(&id) {
                if let Some(worker) = session.worker {
                    workers.push(worker);
                }
            }
        }
        workers
    };
    let _ = app;
    if workers.is_empty() {
        return;
    }
    std::thread::spawn(move || {
        for worker in workers {
            worker_shutdown(&worker);
        }
    });
}

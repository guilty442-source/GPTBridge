//! embedded.rs — port of src-ui/main/embedded-browser.ts.
//!
//! Helper-process architecture: each session is hosted by a dedicated
//! ``gptbridge-shell.exe --embedded-worker`` process.  This machine's
//! WebView2 runtime wedges any host → controller call once a process owns
//! more than one controller (post-loop creation deadlocks in EBW.dll —
//! wry#1665/#583 class — and even ops on pre-built pool views stall
//! 5–60 s).  A worker owns exactly one controller — the reliable
//! first-controller path — so sessions spawn a worker, and this module
//! proxies every lifecycle operation to it over a token-guarded loopback
//! endpoint (``embedded_worker.rs``).
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

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::OnceLock;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

use crate::http_util;
use crate::paths;

const WORKER_TOKEN_HEADER: &str = "x-gptbridge-worker-token";
const WORKER_READY_TIMEOUT: Duration = Duration::from_secs(25);
const WORKER_OP_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct BrowserBounds {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

#[derive(Debug, Clone)]
pub struct WorkerRef {
    pub port: u16,
    pub token: String,
    pub pid: u32,
    pub state_file: PathBuf,
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

pub fn embedded_state() -> &'static Mutex<EmbeddedState> {
    static STATE: OnceLock<Mutex<EmbeddedState>> = OnceLock::new();
    STATE.get_or_init(|| Mutex::new(EmbeddedState::new()))
}

fn main_window(app: &AppHandle) -> Option<tauri::Window> {
    app.get_window("main")
}

/// Last logical content size delivered by the main window's Resized events.
/// Reading ``inner_size``/``scale_factor`` mid-handler touches tao's
/// per-window ``window_state`` lock — an AB-BA hazard against in-flight
/// WndProc dispatch — so geometry is cached from the event payload instead.
fn content_size() -> &'static Mutex<Option<(f64, f64, f64)>> {
    static SIZE: OnceLock<Mutex<Option<(f64, f64, f64)>>> = OnceLock::new();
    SIZE.get_or_init(|| Mutex::new(None))
}

pub fn record_content_size(size: tauri::PhysicalSize<u32>, scale_factor: f64) {
    // A hidden/not-yet-realised window can report 0x0 — a zero size must
    // never poison the cache (every bounds clamp would fail-closed-hide).
    if size.width == 0 || size.height == 0 {
        return;
    }
    let logical = size.to_logical::<f64>(scale_factor);
    *content_size().lock().unwrap() = Some((logical.width, logical.height, scale_factor));
}

/// Cached logical content size for callers that must not touch window
/// state (resize handlers, bridge workers).
pub fn current_content_size() -> Option<(f64, f64)> {
    content_size()
        .lock()
        .unwrap()
        .map(|(w, h, _)| (w, h))
}

fn current_scale_factor() -> f64 {
    content_size()
        .lock()
        .unwrap()
        .map(|(_, _, s)| s)
        .unwrap_or(1.0)
}

fn clamp_bounds(app: &AppHandle, bounds: BrowserBounds) -> Option<BrowserBounds> {
    if main_window(app).is_none() {
        return None;
    }
    let (content_w, content_h) = current_content_size()?;
    let x = bounds.x.round().max(0.0).min(content_w);
    let y = bounds.y.round().max(0.0).min(content_h);
    let width = bounds.width.round().max(0.0).min(content_w - x);
    let height = bounds.height.round().max(0.0).min(content_h - y);
    if width < 1.0 || height < 1.0 {
        return None;
    }
    Some(BrowserBounds { x, y, width, height })
}

fn sanitize_id(id: &str) -> String {
    id.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

fn worker_state_path(session_id: &str) -> PathBuf {
    paths::path_library()
        .workspace_root
        .join("main-system")
        .join("runtime")
        .join("state")
        .join(format!("embedded-worker-{}.json", sanitize_id(session_id)))
}

fn random_token() -> String {
    let mut buf = [0u8; 32];
    let _ = getrandom::getrandom(&mut buf);
    hex::encode(buf)
}

fn main_window_hwnd(app: &AppHandle) -> isize {
    main_window(app)
        .and_then(|w| w.hwnd().ok())
        .map(|h| h.0 as isize)
        .unwrap_or(0)
}

/// Spawn a dedicated worker process and wait (bounded) for its loopback
/// endpoint to publish.  Called on a worker thread; no registry lock held.
fn spawn_worker(app: &AppHandle, session_id: &str, url: &str) -> Result<WorkerRef, String> {
    let exe = std::env::current_exe().map_err(|e| format!("SELF_EXE_UNAVAILABLE:{e}"))?;
    let state_file = worker_state_path(session_id);
    let _ = std::fs::remove_file(&state_file);
    let token = random_token();
    let parent_hwnd = main_window_hwnd(app);
    let parent_pid = std::process::id();

    // Worker diagnostics land in a bounded per-session log rather than the
    // parent's stderr (worker exit codes otherwise surface as opaque
    // WORKER_UNREACHABLE failures).
    let log_path = paths::path_library()
        .workspace_root
        .join("main-system")
        .join("runtime")
        .join("logs")
        .join(format!("embedded-worker-{}.log", sanitize_id(session_id)));
    let stderr_redirect = std::fs::File::create(&log_path)
        .map(std::process::Stdio::from)
        .unwrap_or_else(|_| std::process::Stdio::inherit());

    let child = std::process::Command::new(exe)
        .arg("--embedded-worker")
        .arg("--session-id")
        .arg(session_id)
        .arg("--url")
        .arg(url)
        .arg("--parent-hwnd")
        .arg(format!("0x{parent_hwnd:x}"))
        .arg("--parent-pid")
        .arg(parent_pid.to_string())
        .arg("--token")
        .arg(&token)
        .arg("--state-file")
        .arg(&state_file)
        .stderr(stderr_redirect)
        .spawn()
        .map_err(|e| format!("WORKER_SPAWN_FAILED:{e}"))?;
    let child_pid = child.id();
    drop(child);

    let deadline = std::time::Instant::now() + WORKER_READY_TIMEOUT;
    while std::time::Instant::now() < deadline {
        if let Ok(text) = std::fs::read_to_string(&state_file) {
            if let Ok(state) = serde_json::from_str::<serde_json::Value>(&text) {
                if let Some(port) = state["port"].as_u64().map(|p| p as u16) {
                    if port != 0 {
                        return Ok(WorkerRef {
                            port,
                            token,
                            pid: child_pid,
                            state_file,
                        });
                    }
                }
            }
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    Err("WORKER_READY_TIMEOUT".to_string())
}

/// One HTTP round-trip to a session's worker.  Returns the parsed JSON
/// body or ``None`` on transport failure.
fn worker_request(
    worker: &WorkerRef,
    method: &str,
    path: &str,
    body: &serde_json::Value,
) -> Option<serde_json::Value> {
    let headers = [(WORKER_TOKEN_HEADER, worker.token.as_str())];
    let response = if method == "GET" {
        http_util::get("127.0.0.1", worker.port, path, &headers, WORKER_OP_TIMEOUT)
    } else {
        http_util::post(
            "127.0.0.1",
            worker.port,
            path,
            &headers,
            body.to_string().as_bytes(),
            WORKER_OP_TIMEOUT,
        )
    }?;
    if response.status != 200 {
        return None;
    }
    serde_json::from_slice(&response.body).ok()
}

fn physical_bounds(bounds: &BrowserBounds) -> serde_json::Value {
    let scale = current_scale_factor();
    serde_json::json!({
        "x": bounds.x * scale,
        "y": bounds.y * scale,
        "width": bounds.width * scale,
        "height": bounds.height * scale,
    })
}

/// Tell a worker to park itself (1x1 at the parent origin — the hidden
/// state; child windows also vanish automatically with the parent).
fn worker_hide(worker: &WorkerRef) {
    let _ = worker_request(worker, "POST", "/hide", &serde_json::json!({}));
}

/// Tell a worker to terminate, then bounded-kill the process tree so a
/// wedged worker never lingers.
fn worker_shutdown(worker: &WorkerRef) {
    let _ = worker_request(worker, "POST", "/close", &serde_json::json!({}));
    std::thread::sleep(Duration::from_millis(400));
    // Worker exits on its own via /close; taskkill only as a bounded
    // backstop for a wedged worker (tree kill covers WebView2 children).
    let _ = std::process::Command::new("taskkill")
        .args(["/PID", &worker.pid.to_string(), "/T", "/F"])
        .output();
    let _ = std::fs::remove_file(&worker.state_file);
}

/// Called on window minimize/hide — detach every visible session.  Child
/// windows also hide automatically with the parent; this keeps the
/// session `visible` flags truthful.
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
    for worker in workers {
        worker_hide(&worker);
    }
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
    for (worker, bounds) in updates {
        match bounds {
            Some(b) => {
                let _ = worker_request(&worker, "POST", "/bounds", &physical_bounds(&b));
            }
            None => worker_hide(&worker),
        }
    }
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
    serde_json::json!({"ok": true, "hidden": clamped.is_none(), "diag": {"content": current_content_size(), "mainWindow": main_window(app).is_some()}})
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
    for worker in workers {
        worker_shutdown(&worker);
    }
}

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
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::OnceLock;

use tauri::{AppHandle, Manager};

use gptbridge_core::native::paths;

use geometry::{clamp_bounds, physical_bounds};
use worker_client::{worker_hide, worker_request, WorkerRef};

pub use geometry::{current_content_size, record_content_size, BrowserBounds};

/// Find a child webview inside a window by label.
pub(crate) fn find_webview(window: &tauri::Window, label: &str) -> Option<tauri::Webview> {
    window.webviews().into_iter().find(|w| w.label() == label)
}
mod sessions;
pub use sessions::*;

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

pub(super) fn embedded_state() -> &'static Mutex<EmbeddedState> {
    static STATE: OnceLock<Mutex<EmbeddedState>> = OnceLock::new();
    STATE.get_or_init(|| Mutex::new(EmbeddedState::new()))
}

/// Which window hosts embedded sessions plus where worker state/log files
/// live.  The main shell uses the "main" window and main-system runtime
/// dirs; a ``--tool-window`` process retargets both to its own window and
/// the tool's runtime directories (the Electron host kept tool sessions
/// inside the tool window — BrowserView-equivalent containment).
pub struct HostConfig {
    pub window_label: &'static str,
    pub state_dir: PathBuf,
    pub log_dir: PathBuf,
    /// Per-session WebView2 user-data root — each worker must get its own
    /// folder because a shared UDF is locked ERROR_BUSY (0x800700AA) by
    /// whichever process opens it first.
    pub worker_data_root: PathBuf,
    /// When set, workers are told to push navigation events to this
    /// loopback endpoint (the tool-window bridge /event channel).
    pub worker_events: bool,
}

fn default_host() -> HostConfig {
    let runtime_root = paths::path_library()
        .workspace_root
        .join("main-system")
        .join("runtime");
    HostConfig {
        window_label: "main",
        state_dir: runtime_root.join("state"),
        log_dir: runtime_root.join("logs"),
        worker_data_root: runtime_root.join("embedded-webview"),
        worker_events: false,
    }
}

static HOST_SLOT: OnceLock<Mutex<HostConfig>> = OnceLock::new();

pub(crate) fn host() -> std::sync::MutexGuard<'static, HostConfig> {
    HOST_SLOT
        .get_or_init(|| Mutex::new(default_host()))
        .lock()
        .unwrap()
}

/// Install the host context before any session op runs (tool-window mode
/// calls this during startup; the main shell keeps the defaults).
pub fn configure_host(config: HostConfig) {
    *host() = config;
}

pub(super) fn host_window(app: &AppHandle) -> Option<tauri::Window> {
    let label = host().window_label;
    app.get_window(label)
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
    // bounded-concurrency/v1: blocking worker calls drain through one
    // bounded queue + single dispatcher — this handler runs inside the
    // event loop's Resized dispatch and must never stall it, and a
    // resize storm must never spawn a thread per event.  Queue full →
    // drop-oldest (a superseded resize batch is stale by definition).
    enqueue_resize(updates);
}

/// Serialized resize-job queue: one dispatcher thread, bounded capacity,
/// drop-oldest overflow.
const RESIZE_QUEUE_CAPACITY: usize = 32;
type ResizeBatch = Vec<(WorkerRef, Option<BrowserBounds>)>;
static RESIZE_QUEUE: Mutex<std::collections::VecDeque<ResizeBatch>> =
    Mutex::new(std::collections::VecDeque::new());
static RESIZE_CV: std::sync::Condvar = std::sync::Condvar::new();
static RESIZE_STARTED: OnceLock<()> = OnceLock::new();

fn enqueue_resize(updates: ResizeBatch) {
    RESIZE_STARTED.get_or_init(|| {
        std::thread::Builder::new()
            .name("webview-resize-dispatch".into())
            .spawn(|| loop {
                let batch = {
                    let mut q = RESIZE_QUEUE.lock().unwrap();
                    loop {
                        if let Some(b) = q.pop_front() {
                            break b;
                        }
                        q = RESIZE_CV.wait(q).unwrap();
                    }
                };
                for (worker, bounds) in batch {
                    match bounds {
                        Some(b) => {
                            let _ =
                                worker_request(&worker, "POST", "/bounds", &physical_bounds(&b));
                        }
                        None => worker_hide(&worker),
                    }
                }
            })
            .ok();
    });
    {
        let mut q = RESIZE_QUEUE.lock().unwrap();
        while q.len() >= RESIZE_QUEUE_CAPACITY {
            q.pop_front(); // drop-oldest
        }
        q.push_back(updates);
    }
    RESIZE_CV.notify_one();
}

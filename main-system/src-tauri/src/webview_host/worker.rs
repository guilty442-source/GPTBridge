//! worker.rs — dedicated helper process hosting ONE embedded browser
//! session (entry point + shared state).
//!
//! Why a separate process: this machine's WebView2 runtime wedges any host
//! → controller call once a process owns more than one controller (post-loop
//! creation deadlocks in EBW.dll; even pooled first-N-controller ops stall
//! 5–60 s).  A dedicated process owns exactly one controller — the reliable
//! first-controller path — so each session spawns
//! ``gptbridge-shell.exe --embedded-worker``.
//!
//! The worker creates a frameless ``WebviewWindow``, reparents it under the
//! main window's HWND via ``SetParent`` (WS_CHILD: clipped to the parent
//! client area, moves and hides with it — BrowserView-equivalent
//! containment), then serves a token-guarded loopback endpoint the parent
//! shell proxies session operations through (``worker_server.rs``).
//!
//! Exit contract: the worker exits when the parent PID dies, when it
//! receives ``POST /close``, or when its state file is deleted.

use std::net::TcpListener;
use std::sync::Mutex;
use std::sync::OnceLock;
use std::time::Duration;

use tauri::{WebviewUrl, WebviewWindowBuilder};

use super::worker_server;

/// eprintln! panics on a broken stderr handle; worker diagnostics are
/// best-effort and must never take the process down.
macro_rules! worker_log {
    ($($arg:tt)*) => {{
        use std::io::Write;
        let _ = writeln!(std::io::stderr().lock(), $($arg)*);
        let _ = std::io::stderr().flush();
    }};
}

pub struct WorkerArgs {
    pub session_id: String,
    pub url: String,
    pub parent_hwnd: isize,
    pub parent_pid: u32,
    pub token: String,
    pub state_file: std::path::PathBuf,
    /// Tool-window mode: the parent tool bridge's loopback endpoint that
    /// receives embedded-browser:event notifications (Electron emitted
    /// these from the BrowserView itself; a worker process must push them
    /// over HTTP).
    pub event_port: u16,
    pub event_token: String,
    /// Per-session WebView2 user-data folder — overrides any inherited
    /// WEBVIEW2_USER_DATA_FOLDER so a worker never collides (ERROR_BUSY)
    /// with the parent window's profile or another session.
    pub user_data_folder: std::path::PathBuf,
}

/// Parse ``--embedded-worker`` argv.  Returns ``None`` when the flag is not
/// present (normal shell mode).
pub fn worker_args() -> Option<WorkerArgs> {
    let args: Vec<String> = std::env::args().collect();
    if !args.iter().any(|a| a == "--embedded-worker") {
        return None;
    }
    let get = |key: &str| -> String {
        args.iter()
            .position(|a| a == key)
            .and_then(|i| args.get(i + 1))
            .cloned()
            .unwrap_or_default()
    };
    Some(WorkerArgs {
        session_id: get("--session-id"),
        url: get("--url"),
        parent_hwnd: get("--parent-hwnd")
            .trim_start_matches("0x")
            .trim_start_matches("0X")
            .parse::<isize>()
            .or_else(|_| {
                isize::from_str_radix(
                    &get("--parent-hwnd")
                        .trim_start_matches("0x")
                        .trim_start_matches("0X"),
                    16,
                )
            })
            .unwrap_or(0),
        parent_pid: get("--parent-pid").parse::<u32>().unwrap_or(0),
        token: get("--token"),
        state_file: std::path::PathBuf::from(get("--state-file")),
        event_port: get("--event-port").parse::<u16>().unwrap_or(0),
        event_token: get("--event-token"),
        user_data_folder: std::path::PathBuf::from(get("--user-data-folder")),
    })
}

pub(crate) fn token_cell() -> &'static Mutex<String> {
    static TOKEN: OnceLock<Mutex<String>> = OnceLock::new();
    TOKEN.get_or_init(|| Mutex::new(String::new()))
}

/// Optional parent tool-bridge event channel (--event-port/--event-token).
pub(crate) fn event_channel() -> &'static Mutex<(u16, String)> {
    static CH: OnceLock<Mutex<(u16, String)>> = OnceLock::new();
    CH.get_or_init(|| Mutex::new((0, String::new())))
}

pub(crate) fn session_id_cell() -> &'static Mutex<String> {
    static ID: OnceLock<Mutex<String>> = OnceLock::new();
    ID.get_or_init(|| Mutex::new(String::new()))
}

/// Last reported document title (``on_document_title_changed`` feed).
pub(crate) fn title_cell() -> &'static Mutex<String> {
    static TITLE: OnceLock<Mutex<String>> = OnceLock::new();
    TITLE.get_or_init(|| Mutex::new(String::new()))
}

/// Navigation/history tracking — Electron ``navigationHistory`` parity.
/// wry exposes no history object, so the worker records the URL stack and
/// drives ``history.back()/forward()`` in the page; renderer-initiated
/// navigations are captured through the ``on_navigation`` callback.
pub(crate) struct NavState {
    pub(crate) history: Vec<String>,
    pub(crate) cursor: usize,
    pub(crate) loading: bool,
}

pub(crate) fn nav_state() -> &'static Mutex<NavState> {
    static NAV: OnceLock<Mutex<NavState>> = OnceLock::new();
    NAV.get_or_init(|| {
        Mutex::new(NavState {
            history: Vec::new(),
            cursor: 0,
            loading: false,
        })
    })
}

pub(crate) fn nav_record(url: &str) {
    let mut nav = nav_state().lock().unwrap();
    if nav.history.get(nav.cursor).map(String::as_str) == Some(url) {
        return;
    }
    // A fresh navigation truncates any forward entries (browser parity).
    let end = nav.cursor + 1;
    nav.history.truncate(end);
    nav.history.push(url.to_string());
    nav.cursor = nav.history.len() - 1;
}

/// bounded-concurrency/v1: events funnel through one bounded channel
/// into a single dispatcher thread — a stalled bridge must never block
/// the webview thread, and an event storm must never spawn a thread
/// per event.  Queue full → drop (telemetry drop/reject policy).
const EVENT_QUEUE_CAPACITY: usize = 256;

fn event_tx() -> &'static std::sync::mpsc::SyncSender<(u16, String, String)> {
    static TX: OnceLock<std::sync::mpsc::SyncSender<(u16, String, String)>> =
        OnceLock::new();
    TX.get_or_init(|| {
        let (tx, rx) = std::sync::mpsc::sync_channel::<(u16, String, String)>(
            EVENT_QUEUE_CAPACITY,
        );
        std::thread::Builder::new()
            .name("webview-event-dispatch".into())
            .spawn(move || {
                while let Ok((port, token, payload)) = rx.recv() {
                    let _ = gptbridge_core::ipc::http::post(
                        "127.0.0.1",
                        port,
                        "/event",
                        &[("x-gptbridge-bridge-token", token.as_str())],
                        payload.as_bytes(),
                        Duration::from_secs(5),
                    );
                }
            })
            .ok();
        tx
    })
}

/// Push an embedded-browser event to the parent tool bridge (which emits
/// ``embedded-browser:event`` into the tool renderer).  Fire-and-forget
/// through the bounded dispatch queue — a stalled bridge must never
/// block the webview thread, and a full queue drops the event.
pub(crate) fn push_event(event_type: &str, url: &str, detail: serde_json::Value) {
    let (port, token) = event_channel().lock().unwrap().clone();
    if port == 0 || token.is_empty() {
        return;
    }
    let mut body = serde_json::json!({
        "id": session_id_cell().lock().unwrap().clone(),
        "type": event_type,
        "url": url,
    });
    if let (Some(obj), Some(extra)) = (body.as_object_mut(), detail.as_object()) {
        for (k, v) in extra {
            obj.insert(k.clone(), v.clone());
        }
    }
    let _ = event_tx().try_send((port, token, body.to_string()));
}

pub(crate) fn pending_cell(
) -> &'static Mutex<std::collections::HashMap<String, std::sync::mpsc::Sender<serde_json::Value>>> {
    static P: OnceLock<
        Mutex<std::collections::HashMap<String, std::sync::mpsc::Sender<serde_json::Value>>>,
    > = OnceLock::new();
    P.get_or_init(|| Mutex::new(std::collections::HashMap::new()))
}

fn app_handle() -> &'static Mutex<Option<tauri::AppHandle>> {
    static H: OnceLock<Mutex<Option<tauri::AppHandle>>> = OnceLock::new();
    H.get_or_init(|| Mutex::new(None))
}

fn publish_state(path: &std::path::Path, port: u16) {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let body = serde_json::json!({
        "host": "127.0.0.1",
        "port": port,
        "pid": std::process::id(),
    });
    let _ = std::fs::write(
        path,
        serde_json::to_string_pretty(&body).unwrap_or_default(),
    );
}

/// Dispatch a closure to the worker's own event loop and wait (bounded) for
/// its JSON result.  First-controller ops on the worker's loop are reliable.
pub(crate) fn on_main(
    f: impl FnOnce(&tauri::AppHandle) -> serde_json::Value + Send + 'static,
) -> serde_json::Value {
    let app = match app_handle().lock().unwrap().clone() {
        Some(a) => a,
        None => return serde_json::json!({"ok": false, "message": "APP_NOT_READY"}),
    };
    let (tx, rx) = std::sync::mpsc::channel();
    let app2 = app.clone();
    if app
        .run_on_main_thread(move || {
            let _ = tx.send(f(&app2));
        })
        .is_err()
    {
        return serde_json::json!({"ok": false, "message": "DISPATCH_FAILED"});
    }
    rx.recv_timeout(Duration::from_secs(15))
        .unwrap_or_else(|_| serde_json::json!({"ok": false, "message": "OP_TIMEOUT"}))
}

pub(crate) fn worker_port() -> u16 {
    WORKER_PORT.load(std::sync::atomic::Ordering::SeqCst)
}

static WORKER_PORT: std::sync::atomic::AtomicU16 = std::sync::atomic::AtomicU16::new(0);

/// Parent-death watchdog: exit when the parent shell's PID disappears so a
/// crashed parent never leaks worker/webview process trees.
fn start_parent_watchdog(parent_pid: u32) {
    if parent_pid == 0 {
        return;
    }
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(2));
        if !parent_alive(parent_pid) {
            worker_log!("[embedded-worker] PARENT_GONE {parent_pid}");
            std::process::exit(0);
        }
    });
}

#[cfg(windows)]
fn parent_alive(pid: u32) -> bool {
    use windows_sys::Win32::System::Threading::{GetExitCodeProcess, OpenProcess};
    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    const STILL_ACTIVE: i32 = 259;
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle == std::ptr::null_mut() {
            return false;
        }
        let mut code: u32 = 0;
        let ok = GetExitCodeProcess(handle, &mut code);
        windows_sys::Win32::Foundation::CloseHandle(handle);
        ok != 0 && code as i32 == STILL_ACTIVE
    }
}

#[cfg(not(windows))]
fn parent_alive(_pid: u32) -> bool {
    true
}

/// The parent's raw HWND for ``parent_raw`` — a child window is confined
/// to the parent client area (WS_CHILD — BrowserView-equivalent
/// containment).  Cross-process parenting is legal on Windows.
#[cfg(windows)]
fn parent_handle(raw: isize) -> Option<windows::Win32::Foundation::HWND> {
    if raw == 0 {
        None
    } else {
        Some(windows::Win32::Foundation::HWND(
            raw as *mut std::ffi::c_void,
        ))
    }
}

/// Entry point for ``--embedded-worker`` mode.  Owns the process forever;
/// never returns to the normal shell path.
pub fn run(args: WorkerArgs) -> i32 {
    *token_cell().lock().unwrap() = args.token.clone();
    *session_id_cell().lock().unwrap() = args.session_id.clone();
    *event_channel().lock().unwrap() = (args.event_port, args.event_token.clone());
    nav_state().lock().unwrap().history.push(args.url.clone());
    start_parent_watchdog(args.parent_pid);

    // Isolate the session profile: an inherited WEBVIEW2_USER_DATA_FOLDER
    // would collide with the host window's own webview (ERROR_BUSY) and
    // kill this worker's webview outright.  Must be set before any
    // webview initialises.
    if !args.user_data_folder.as_os_str().is_empty() {
        std::env::set_var("WEBVIEW2_USER_DATA_FOLDER", &args.user_data_folder);
    } else {
        std::env::remove_var("WEBVIEW2_USER_DATA_FOLDER");
    }
    std::env::remove_var("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS");

    // Loopback listener first so the parent can poll the state file as soon
    // as it lands — the webview may still be warming up.
    let listener = match TcpListener::bind("127.0.0.1:0") {
        Ok(l) => l,
        Err(e) => {
            worker_log!("[embedded-worker] LISTENER_BIND_FAILED {e}");
            return 2;
        }
    };
    let port = listener.local_addr().map(|a| a.port()).unwrap_or_default();
    WORKER_PORT.store(port, std::sync::atomic::Ordering::SeqCst);
    worker_log!(
        "[embedded-worker] session={} pid={} port={port}",
        args.session_id,
        std::process::id()
    );
    // bounded-concurrency/v1: governor-sized worker pool + bounded
    // pending queue; a full queue closes the connection — never a
    // thread per connection.
    let workers = crate::governor_budget::resolve_workers("network", 2, 8);
    // Declared B16 latency envelope for a queued connection.
    const PENDING_DEADLINE: std::time::Duration =
        std::time::Duration::from_millis(2000);
    let pending = crate::governor_budget::bounded_conn_pool(
        workers,
        32,
        PENDING_DEADLINE,
        (),
        |_, stream| worker_server::handle_connection(stream),
    );
    std::thread::spawn(move || {
        for incoming in listener.incoming() {
            match incoming {
                Ok(stream) => {
                    // Reject: close immediately (IPC callers retry).
                    let _ = pending.try_send((stream, std::time::Instant::now()));
                }
                Err(_) => std::thread::sleep(Duration::from_millis(50)),
            }
        }
    });

    let url = args.url.clone();
    let parent_hwnd = args.parent_hwnd;
    let app = tauri::Builder::default()
        .setup(move |app| {
            let parsed: tauri::Url = url
                .parse()
                .unwrap_or_else(|_| "about:blank".parse().unwrap());
            let mut builder =
                WebviewWindowBuilder::new(app, "session", WebviewUrl::External(parsed))
                    .title("embedded-browser")
                    .visible(false)
                    .decorations(false)
                    .resizable(false)
                    .minimizable(false)
                    .maximizable(false)
                    .skip_taskbar(true)
                    .shadow(false)
                    .focused(false)
                    .always_on_top(false)
                    .inner_size(1.0, 1.0)
                    .position(0.0, 0.0)
                    // Tool-window parity: renderer-initiated navigations update
                    // the recorded stack and push events to the parent bridge.
                    .on_navigation(|url| {
                        let url = url.to_string();
                        nav_record(&url);
                        push_event("navigate", &url, serde_json::json!({"url": url}));
                        true
                    })
                    .on_document_title_changed(|_webview, title| {
                        *title_cell().lock().unwrap() = title;
                    })
                    .on_page_load(|_webview, payload| {
                        let url = payload.url().to_string();
                        match payload.event() {
                            tauri::webview::PageLoadEvent::Started => {
                                nav_state().lock().unwrap().loading = true;
                                push_event("loading-start", &url, serde_json::json!({}));
                            }
                            tauri::webview::PageLoadEvent::Finished => {
                                nav_state().lock().unwrap().loading = false;
                                push_event("loading-stop", &url, serde_json::json!({}));
                            }
                        }
                    });
            #[cfg(windows)]
            {
                if let Some(parent) = parent_handle(parent_hwnd) {
                    builder = builder.parent_raw(parent);
                }
            }
            builder.build()?;
            *app_handle().lock().unwrap() = Some(app.handle().clone());
            // Publish only once the webview exists and the app handle is
            // live — a visible state file IS the parent's "fully ready"
            // signal; publishing earlier raced ops into APP_NOT_READY.
            publish_state(&args.state_file, port);
            Ok(())
        })
        .build(tauri::generate_context!());

    match app {
        Ok(app) => {
            app.run(|_app, _event| {});
            0
        }
        Err(e) => {
            worker_log!("[embedded-worker] TAURI_BUILD_FAILED {e}");
            3
        }
    }
}

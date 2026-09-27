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
    })
}

pub(crate) fn token_cell() -> &'static Mutex<String> {
    static TOKEN: OnceLock<Mutex<String>> = OnceLock::new();
    TOKEN.get_or_init(|| Mutex::new(String::new()))
}

pub(crate) fn pending_cell() -> &'static Mutex<std::collections::HashMap<String, std::sync::mpsc::Sender<serde_json::Value>>> {
    static P: OnceLock<Mutex<std::collections::HashMap<String, std::sync::mpsc::Sender<serde_json::Value>>>> =
        OnceLock::new();
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
    let _ = std::fs::write(path, serde_json::to_string_pretty(&body).unwrap_or_default());
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
        Some(windows::Win32::Foundation::HWND(raw as *mut std::ffi::c_void))
    }
}

/// Entry point for ``--embedded-worker`` mode.  Owns the process forever;
/// never returns to the normal shell path.
pub fn run(args: WorkerArgs) -> i32 {
    *token_cell().lock().unwrap() = args.token.clone();
    start_parent_watchdog(args.parent_pid);

    // Loopback listener first so the parent can poll the state file as soon
    // as it lands — the webview may still be warming up.
    let listener = match TcpListener::bind("127.0.0.1:0") {
        Ok(l) => l,
        Err(e) => {
            worker_log!("[embedded-worker] LISTENER_BIND_FAILED {e}");
            return 2;
        }
    };
    let port = listener
        .local_addr()
        .map(|a| a.port())
        .unwrap_or_default();
    WORKER_PORT.store(port, std::sync::atomic::Ordering::SeqCst);
    worker_log!(
        "[embedded-worker] session={} pid={} port={port}",
        args.session_id,
        std::process::id()
    );
    std::thread::spawn(move || {
        for incoming in listener.incoming() {
            match incoming {
                Ok(stream) => {
                    std::thread::spawn(move || worker_server::handle_connection(stream));
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
            let mut builder = WebviewWindowBuilder::new(app, "session", WebviewUrl::External(parsed))
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
                .position(0.0, 0.0);
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

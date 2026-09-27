//! worker_client.rs — parent-side client of the embedded-worker contract.
//!
//! Spawns the dedicated ``--embedded-worker`` helper process, waits
//! (bounded) for its loopback endpoint to publish, and proxies every
//! session lifecycle operation to it over the token-guarded endpoint.
//!
//! Threading contract: the session-registry lock must NEVER be held across
//! a worker HTTP call or process spawn — those block for tens of ms to
//! seconds while main-thread window-event handlers also take the lock.

use std::path::PathBuf;
use std::time::Duration;

use tauri::{AppHandle, Manager};

use gptbridge_core::ipc::http;
use gptbridge_core::native::paths;
use gptbridge_core::security::sanitize_id;

const WORKER_TOKEN_HEADER: &str = "x-gptbridge-worker-token";
const WORKER_READY_TIMEOUT: Duration = Duration::from_secs(25);
const WORKER_OP_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug, Clone)]
pub struct WorkerRef {
    pub port: u16,
    pub token: String,
    pub pid: u32,
    pub state_file: PathBuf,
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
    app.get_window("main")
        .and_then(|w| w.hwnd().ok())
        .map(|h| h.0 as isize)
        .unwrap_or(0)
}

/// Spawn a dedicated worker process and wait (bounded) for its loopback
/// endpoint to publish.  Called on a worker thread; no registry lock held.
pub(crate) fn spawn_worker(
    app: &AppHandle,
    session_id: &str,
    url: &str,
) -> Result<WorkerRef, String> {
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
    // A worker that never published its endpoint received no ops — tree-kill
    // it so a wedged spawn cannot linger as an orphan webview host.
    let _ = std::process::Command::new("taskkill")
        .args(["/PID", &child_pid.to_string(), "/T", "/F"])
        .output();
    let _ = std::fs::remove_file(&state_file);
    Err("WORKER_READY_TIMEOUT".to_string())
}

/// One HTTP round-trip to a session's worker.  Returns the parsed JSON
/// body or ``None`` on transport failure.
pub(crate) fn worker_request(
    worker: &WorkerRef,
    method: &str,
    path: &str,
    body: &serde_json::Value,
) -> Option<serde_json::Value> {
    let headers = [(WORKER_TOKEN_HEADER, worker.token.as_str())];
    let response = if method == "GET" {
        http::get("127.0.0.1", worker.port, path, &headers, WORKER_OP_TIMEOUT)
    } else {
        http::post(
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

/// Tell a worker to park itself (1x1 at the parent origin — the hidden
/// state; child windows also vanish automatically with the parent).
pub(crate) fn worker_hide(worker: &WorkerRef) {
    let _ = worker_request(worker, "POST", "/hide", &serde_json::json!({}));
}

/// Tell a worker to terminate, then bounded-kill the process tree so a
/// wedged worker never lingers.
pub(crate) fn worker_shutdown(worker: &WorkerRef) {
    let _ = worker_request(worker, "POST", "/close", &serde_json::json!({}));
    std::thread::sleep(Duration::from_millis(400));
    // Worker exits on its own via /close; taskkill only as a bounded
    // backstop for a wedged worker (tree kill covers WebView2 children).
    let _ = std::process::Command::new("taskkill")
        .args(["/PID", &worker.pid.to_string(), "/T", "/F"])
        .output();
    let _ = std::fs::remove_file(&worker.state_file);
}

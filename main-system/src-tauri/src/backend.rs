//! backend.rs — port of src-ui/main/python-backend.ts.
//!
//! Architecture boundary (A60/A61): the desktop shell only wakes the screen
//! and spawns the startup core (boot_core); boot_core supervises main.py and
//! generates its own governance bootstrap token.  This module ONLY spawns and
//! stops boot_core, tracks liveness, attaches to an existing governed
//! backend, and applies the bounded auto-restart policy.

use std::fs;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use crate::http_util;
use crate::paths::{self, RuntimePathLibrary};
use crate::session;

const AUTO_RESTART_MAX_ATTEMPTS: u32 = 5;
const AUTO_RESTART_BASE_DELAY_MS: u64 = 2_000;
const AUTO_RESTART_MAX_DELAY_MS: u64 = 30_000;
const ATTACHED_MONITOR_INTERVAL_MS: u64 = 10_000;
const ATTACHED_MONITOR_FAILURE_LIMIT: u32 = 3;
const GRACEFUL_EXIT_MS: u64 = 10_000;
const PRODUCT_VERSION: &str = "1.0.0";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendStatus {
    Idle,
    Starting,
    Running,
    Stopping,
    Error,
}

impl BackendStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Starting => "starting",
            Self::Running => "running",
            Self::Stopping => "stopping",
            Self::Error => "error",
        }
    }
}

struct State {
    status: BackendStatus,
    child: Option<Child>,
    started_at: Option<std::time::SystemTime>,
    ready_at: Option<std::time::SystemTime>,
    message: String,
    last_error: String,
    auto_restart_attempts: u32,
    manual_shutdown: bool,
    shutdown_token: String,
}

impl State {
    fn new() -> Self {
        Self {
            status: BackendStatus::Idle,
            child: None,
            started_at: None,
            ready_at: None,
            message: "backend idle".to_string(),
            last_error: String::new(),
            auto_restart_attempts: 0,
            manual_shutdown: false,
            shutdown_token: String::new(),
        }
    }
}

fn state() -> &'static Mutex<State> {
    static STATE: OnceLock<Mutex<State>> = OnceLock::new();
    STATE.get_or_init(|| Mutex::new(State::new()))
}

fn monitor_started() -> &'static AtomicBool {
    static FLAG: OnceLock<AtomicBool> = OnceLock::new();
    FLAG.get_or_init(|| AtomicBool::new(false))
}

fn report(event: &str, payload: serde_json::Value) {
    // println!/eprintln! panic on broken inherited handles — the shell can
    // outlive the launcher's stdio, so diagnostics stay best-effort.
    use std::io::Write;
    let _ = writeln!(std::io::stderr().lock(), "[Main System] {event} {payload}");
    let _ = std::io::stderr().flush();
    let _ = writeln!(std::io::stdout().lock(), "[Main System] {event} {payload}");
    let _ = std::io::stdout().flush();
}

pub fn get_backend_status() -> BackendStatus {
    state().lock().unwrap().status
}

pub fn backend_runtime_info() -> serde_json::Value {
    let s = state().lock().unwrap();
    let startup_ms = match (s.started_at, s.ready_at) {
        (Some(start), Some(ready)) => ready
            .duration_since(start)
            .ok()
            .map(|d| d.as_millis() as i64),
        _ => None,
    };
    serde_json::json!({
        "status": s.status.as_str(),
        "ready": s.status == BackendStatus::Running,
        "startedAt": s.started_at.map(|t| epoch_ms(t)),
        "readyAt": s.ready_at.map(|t| epoch_ms(t)),
        "startupMs": startup_ms,
        "message": s.message,
        "lastError": s.last_error,
    })
}

fn epoch_ms(t: std::time::SystemTime) -> i64 {
    t.duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn probe_existing_backend() -> bool {
    let port = session::resolve_backend_port();
    let Some(response) = http_util::get(
        session::LOOPBACK_HOST,
        port,
        session::BACKEND_HEALTH_PATH,
        &[],
        Duration::from_secs(8),
    ) else {
        return false;
    };
    let Ok(payload) = serde_json::from_slice::<serde_json::Value>(&response.body) else {
        return false;
    };
    payload["workspace_instance_id"].as_str() == Some(session::workspace_instance_id().as_str())
        && payload["version"].as_str() == Some(PRODUCT_VERSION)
        && payload["backend_runtime_ready"].as_bool() == Some(true)
}

fn pid_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::CloseHandle;
        use windows_sys::Win32::System::Threading::{GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION};
        unsafe {
            let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if handle.is_null() {
                return false;
            }
            let mut code: u32 = 0;
            let ok = GetExitCodeProcess(handle, &mut code);
            CloseHandle(handle);
            return ok != 0 && code == 259; // STILL_ACTIVE
        }
    }
    #[cfg(unix)]
    {
        std::path::Path::new(&format!("/proc/{pid}")).exists()
    }
    #[allow(unreachable_code)]
    false
}

fn has_live_supervisor(lib: &RuntimePathLibrary) -> bool {
    let state_path = lib
        .workspace_root
        .join("main-system")
        .join("runtime")
        .join("state")
        .join("boot-core.json");
    if let Ok(raw) = fs::read_to_string(&state_path) {
        if let Ok(state_json) = serde_json::from_str::<serde_json::Value>(&raw) {
            let pid = state_json["pid"].as_u64().unwrap_or(0) as u32;
            let status = state_json["status"].as_str().unwrap_or("");
            if pid > 0 && status != "stopped" && pid_alive(pid) {
                return true;
            }
        }
    }
    // The gateway answering /health for this workspace is authoritative
    // evidence that a supervisor is already running.
    session::is_gateway_alive()
}

fn request_graceful_backend_shutdown(token: &str) -> bool {
    let port = session::resolve_backend_port();
    http_util::get(
        session::LOOPBACK_HOST,
        port,
        "/shutdown",
        &[
            ("X-GPTBridge-Shutdown-Token", token),
            ("X-GPTBridge-Shutdown-Reason", "hot-update"),
        ],
        Duration::from_millis(1_500),
    )
    .map(|r| r.status == 200)
    .unwrap_or(false)
}

fn taskkill_pid(pid: u32) {
    let _ = Command::new("taskkill.exe")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(|mut c| {
            // taskkill that never reports must not block the quit path.
            let deadline = Instant::now() + Duration::from_secs(5);
            loop {
                match c.try_wait() {
                    Ok(Some(_)) | Err(_) => break,
                    Ok(None) if Instant::now() > deadline => break,
                    Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                }
            }
        });
}

fn kill_managed_backend_processes(exclude_pid: Option<u32>) {
    // Stop both sides of the managed pair; an attached backend has no child
    // handle at all, so read pids from the boot-core state file.
    let lib = paths::path_library();
    let state_path = lib
        .workspace_root
        .join("main-system")
        .join("runtime")
        .join("state")
        .join("boot-core.json");
    let Ok(raw) = fs::read_to_string(&state_path) else {
        return;
    };
    let Ok(state_json) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return;
    };
    for key in ["pid", "backend_pid"] {
        let pid = state_json[key].as_u64().unwrap_or(0) as u32;
        if pid == 0 || Some(pid) == exclude_pid {
            continue;
        }
        if !pid_alive(pid) {
            continue;
        }
        taskkill_pid(pid);
    }
}

fn spawn_boot_core(lib: &RuntimePathLibrary, auto_kill_backend_port: bool) {
    {
        let mut s = state().lock().unwrap();
        if s.manual_shutdown {
            s.status = BackendStatus::Idle;
            s.message = "backend start cancelled by shutdown".to_string();
            return;
        }
        s.message = "spawning boot_core (startup core)".to_string();
        s.last_error.clear();
    }

    let shutdown_token = {
        let mut buf = [0u8; 32];
        let _ = getrandom::getrandom(&mut buf);
        hex::encode(buf)
    };

    let mut args = vec![
        "-u".to_string(),
        lib.boot_core_entry.to_string_lossy().to_string(),
        "--serve".to_string(),
    ];
    if auto_kill_backend_port {
        args.push("--auto-kill-backend-port".to_string());
    }

    let mut command = Command::new(&lib.python_executable);
    command
        .args(&args)
        .current_dir(&lib.workspace_root)
        .env("GPTBRIDGE_PROJECT_ROOT", &lib.workspace_root)
        .env("GPTBRIDGE_RELEASE_ROOT", &lib.workspace_root)
        .env("GPTBRIDGE_APP_VERSION", PRODUCT_VERSION)
        .env("GPTBRIDGE_IPC_STATE_ROOT", session::ipc_state_root())
        .env(
            "GPTBRIDGE_IPC_SESSION_TOKEN",
            session::backend_session_token().unwrap_or_default(),
        )
        .env("GPTBRIDGE_SHUTDOWN_TOKEN", &shutdown_token)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW — the governed backend never owns a console.
        command.creation_flags(0x0800_0000);
    }

    match command.spawn() {
        Ok(child) => {
            let child_pid = child.id();
            let mut s = state().lock().unwrap();
            s.shutdown_token = shutdown_token;
            s.child = Some(child);
            s.status = BackendStatus::Running;
            s.ready_at = Some(std::time::SystemTime::now());
            s.message = "boot_core supervising backend".to_string();
            s.auto_restart_attempts = 0;
            drop(s);
            report("backend.boot_core.spawned", serde_json::json!({ "pid": child_pid }));
            ensure_monitor_thread();
        }
        Err(err) => {
            let mut s = state().lock().unwrap();
            s.status = BackendStatus::Error;
            s.message = format!("boot_core spawn failed: {err}");
            drop(s);
            report("backend.boot_core.spawn_failed", serde_json::json!({ "error": err.to_string() }));
            schedule_auto_restart();
        }
    }
}

/// Single supervisor thread: watches the owned child, runs the
/// attached-backend probe loop, and consumes the auto-restart queue.
fn ensure_monitor_thread() {
    if monitor_started().swap(true, Ordering::SeqCst) {
        return;
    }
    std::thread::spawn(monitor_loop);
}

fn monitor_loop() {
    let mut attached_failures = 0u32;
    let mut pending_restart_at: Option<Instant> = None;
    let mut next_attached_probe = Instant::now();
    loop {
        std::thread::sleep(Duration::from_millis(200));

        // Fire a pending auto-restart.
        if let Some(at) = pending_restart_at {
            if Instant::now() >= at {
                pending_restart_at = None;
                let manual = state().lock().unwrap().manual_shutdown;
                if !manual {
                    report("backend.auto_restart.firing", serde_json::json!({}));
                    start_backend_inner(true);
                }
            }
        }

        let mut s = state().lock().unwrap();

        // Child liveness.
        if let Some(child) = s.child.as_mut() {
            match child.try_wait() {
                Ok(Some(exit)) => {
                    let code = exit.code();
                    drop(child);
                    s.child = None;
                    report(
                        "backend.boot_core.exited",
                        serde_json::json!({ "code": code }),
                    );
                    if code == Some(0) || s.manual_shutdown {
                        s.status = BackendStatus::Idle;
                        s.message = "backend stopped".to_string();
                        s.auto_restart_attempts = 0;
                        s.manual_shutdown = false;
                    } else {
                        s.status = BackendStatus::Error;
                        s.message = format!(
                            "boot_core exited unexpectedly (code {code:?}), auto-restarting..."
                        );
                        let attempts = s.auto_restart_attempts;
                        if s.manual_shutdown {
                            // nothing
                        } else if attempts >= AUTO_RESTART_MAX_ATTEMPTS {
                            s.message = format!(
                                "boot_core auto-restart exhausted ({AUTO_RESTART_MAX_ATTEMPTS} attempts), giving up"
                            );
                        } else {
                            s.auto_restart_attempts = attempts + 1;
                            let delay = (AUTO_RESTART_BASE_DELAY_MS
                                * 2u64.saturating_pow(attempts))
                            .min(AUTO_RESTART_MAX_DELAY_MS);
                            pending_restart_at =
                                Some(Instant::now() + Duration::from_millis(delay));
                            report(
                                "backend.auto_restart.scheduled",
                                serde_json::json!({ "attempt": attempts + 1, "delay_ms": delay }),
                            );
                        }
                    }
                }
                Ok(None) => {}
                Err(_) => {
                    s.child = None;
                }
            }
            continue;
        }

        // Attached-backend supervision.
        if s.manual_shutdown || s.status != BackendStatus::Running {
            attached_failures = 0;
            continue;
        }
        if s.ready_at.is_none() {
            continue;
        }
        // Attached-mode probes run on the 10s contract interval — the 200ms
        // tick only keeps child reaping and restart dispatch responsive.
        if Instant::now() < next_attached_probe {
            continue;
        }
        next_attached_probe =
            Instant::now() + Duration::from_millis(ATTACHED_MONITOR_INTERVAL_MS);
        drop(s);
        // Attached mode: re-check the attach invariant (healthy backend +
        // live supervisor); a sustained loss routes through auto-restart.
        let alive = probe_existing_backend() && has_live_supervisor(paths::path_library());
        let mut s = state().lock().unwrap();
        if s.child.is_some() || s.status != BackendStatus::Running {
            attached_failures = 0;
            continue;
        }
        if alive {
            attached_failures = 0;
        } else {
            attached_failures += 1;
            if attached_failures >= ATTACHED_MONITOR_FAILURE_LIMIT {
                s.status = BackendStatus::Error;
                s.message = "attached governed backend lost (health/supervisor probe failed), auto-restarting..."
                    .to_string();
                let attempts = s.auto_restart_attempts;
                if attempts >= AUTO_RESTART_MAX_ATTEMPTS {
                    s.message = format!(
                        "boot_core auto-restart exhausted ({AUTO_RESTART_MAX_ATTEMPTS} attempts), giving up"
                    );
                } else {
                    s.auto_restart_attempts = attempts + 1;
                    let delay = (AUTO_RESTART_BASE_DELAY_MS * 2u64.saturating_pow(attempts))
                        .min(AUTO_RESTART_MAX_DELAY_MS);
                    pending_restart_at = Some(Instant::now() + Duration::from_millis(delay));
                }
                attached_failures = 0;
            }
        }
        drop(s);
    }
}

fn schedule_auto_restart() {
    // The monitor thread owns restart timing; flag via status.
    ensure_monitor_thread();
}

fn start_backend_inner(force_replacement: bool) {
    {
        let mut s = state().lock().unwrap();
        if s.status == BackendStatus::Running {
            return;
        }
        if s.child.is_some() {
            return;
        }
    }

    let lib = paths::path_library();

    if !force_replacement && probe_existing_backend() && has_live_supervisor(lib) {
        let mut s = state().lock().unwrap();
        s.status = BackendStatus::Running;
        s.ready_at = Some(std::time::SystemTime::now());
        s.message = "attached to existing governed backend".to_string();
        drop(s);
        ensure_monitor_thread();
        return;
    }

    {
        let mut s = state().lock().unwrap();
        for (path, label) in [
            (&lib.python_executable, "Python executable"),
            (&lib.boot_core_entry, "boot_core entry"),
            (&lib.python_entry, "Python entry"),
        ] {
            if !path.exists() {
                s.status = BackendStatus::Error;
                s.message = format!("{label} not found: {}", path.display());
                report(
                    "backend.start.missing",
                    serde_json::json!({ "path": path.to_string_lossy() }),
                );
                return;
            }
        }
        if s.manual_shutdown {
            s.status = BackendStatus::Idle;
            s.message = "backend start cancelled by shutdown".to_string();
            return;
        }
        s.status = BackendStatus::Starting;
        s.started_at = Some(std::time::SystemTime::now());
        s.ready_at = None;
        s.message = "spawning boot_core".to_string();
    }
    spawn_boot_core(lib, true);
}

pub fn start_backend() {
    start_backend_inner(false);
}

pub fn ensure_backend_started() -> BackendStatus {
    {
        let mut s = state().lock().unwrap();
        if s.status == BackendStatus::Error {
            s.status = BackendStatus::Idle;
        }
        if s.child.is_none()
            && s.status != BackendStatus::Running
            && s.status != BackendStatus::Starting
        {
            drop(s);
            start_backend_inner(false);
        }
    }
    get_backend_status()
}

pub fn stop_backend() {
    let (child_opt, token) = {
        let mut s = state().lock().unwrap();
        s.manual_shutdown = true;
        if s.child.is_none() {
            if s.status == BackendStatus::Starting {
                s.status = BackendStatus::Idle;
                s.message = "backend start cancelled".to_string();
            }
            drop(s);
            kill_managed_backend_processes(None);
            return;
        }
        s.status = BackendStatus::Stopping;
        s.message = "stopping boot_core".to_string();
        (s.child.take(), s.shutdown_token.clone())
    };

    let mut child = match child_opt {
        Some(c) => c,
        None => return,
    };
    let child_pid = child.id();

    if !token.is_empty() {
        let _ = request_graceful_backend_shutdown(&token);
    }

    let deadline = Instant::now() + Duration::from_millis(GRACEFUL_EXIT_MS);
    let mut exited = false;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(_)) => {
                exited = true;
                break;
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(100)),
            Err(_) => break,
        }
    }
    if !exited {
        taskkill_pid(child_pid);
        let _ = child.wait();
    }
    // Complete-close guarantee: even when boot_core exited gracefully, a
    // still-running backend_pid would survive as an orphan.
    kill_managed_backend_processes(Some(child_pid));

    let mut s = state().lock().unwrap();
    s.shutdown_token.clear();
    s.status = BackendStatus::Idle;
    s.message = if exited {
        "backend stopped gracefully".to_string()
    } else {
        "boot_core process tree stopped after graceful timeout".to_string()
    };
}

pub fn restart_backend() -> BackendStatus {
    {
        let mut s = state().lock().unwrap();
        s.manual_shutdown = false;
    }
    stop_backend();
    {
        let mut s = state().lock().unwrap();
        s.status = BackendStatus::Idle;
        s.manual_shutdown = false;
    }
    start_backend_inner(true);
    get_backend_status()
}

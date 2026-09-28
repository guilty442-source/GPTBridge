//! backend.rs — port of src-ui/main/python-backend.ts (state + spawn/stop).
//!
//! Architecture boundary (A60/A61): the desktop shell only wakes the screen
//! and spawns the startup core (boot_core); boot_core supervises main.py and
//! generates its own governance bootstrap token.  This module ONLY spawns and
//! stops boot_core and tracks liveness; supervision/restart policy lives in
//! ``monitor.rs``, raw process ops in ``process.rs``.

use std::process::{Child, Command, Stdio};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use crate::app::{self, PRODUCT_VERSION};
use crate::ipc::{discovery, http};
use crate::lifecycle::{monitor, process};
use crate::native::paths::{self, RuntimePathLibrary};
use crate::security::token;

const GRACEFUL_EXIT_MS: u64 = 10_000;

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

pub(crate) struct State {
    pub(crate) status: BackendStatus,
    pub(crate) child: Option<Child>,
    pub(crate) started_at: Option<std::time::SystemTime>,
    pub(crate) ready_at: Option<std::time::SystemTime>,
    pub(crate) message: String,
    pub(crate) last_error: String,
    pub(crate) auto_restart_attempts: u32,
    pub(crate) manual_shutdown: bool,
    pub(crate) shutdown_token: String,
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

pub(crate) fn state() -> &'static Mutex<State> {
    static STATE: OnceLock<Mutex<State>> = OnceLock::new();
    STATE.get_or_init(|| Mutex::new(State::new()))
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

fn request_graceful_backend_shutdown(token_value: &str) -> bool {
    let port = discovery::resolve_backend_port();
    http::get(
        discovery::LOOPBACK_HOST,
        port,
        "/shutdown",
        &[
            ("X-GPTBridge-Shutdown-Token", token_value),
            ("X-GPTBridge-Shutdown-Reason", "hot-update"),
        ],
        Duration::from_millis(1_500),
    )
    .map(|r| r.status == 200)
    .unwrap_or(false)
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
        .env("GPTBRIDGE_IPC_STATE_ROOT", token::ipc_state_root())
        .env(
            "GPTBRIDGE_IPC_SESSION_TOKEN",
            token::backend_session_token().unwrap_or_default(),
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
            app::report(
                "backend.boot_core.spawned",
                serde_json::json!({ "pid": child_pid }),
            );
            monitor::ensure_monitor_thread();
        }
        Err(err) => {
            let mut s = state().lock().unwrap();
            s.status = BackendStatus::Error;
            s.message = format!("boot_core spawn failed: {err}");
            drop(s);
            app::report(
                "backend.boot_core.spawn_failed",
                serde_json::json!({ "error": err.to_string() }),
            );
            monitor::schedule_auto_restart();
        }
    }
}

pub(crate) fn start_backend_inner(force_replacement: bool) {
    {
        let s = state().lock().unwrap();
        if s.status == BackendStatus::Running {
            return;
        }
        if s.child.is_some() {
            return;
        }
    }

    let lib = paths::path_library();

    if !force_replacement && monitor::probe_existing_backend() && monitor::has_live_supervisor(lib)
    {
        let mut s = state().lock().unwrap();
        s.status = BackendStatus::Running;
        s.ready_at = Some(std::time::SystemTime::now());
        s.message = "attached to existing governed backend".to_string();
        drop(s);
        monitor::ensure_monitor_thread();
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
                app::report(
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
    let (child_opt, token_value) = {
        let mut s = state().lock().unwrap();
        s.manual_shutdown = true;
        if s.child.is_none() {
            if s.status == BackendStatus::Starting {
                s.status = BackendStatus::Idle;
                s.message = "backend start cancelled".to_string();
            }
            drop(s);
            process::kill_managed_backend_processes_now(None);
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

    if !token_value.is_empty() {
        let _ = request_graceful_backend_shutdown(&token_value);
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
        process::taskkill_pid(child_pid);
        let _ = child.wait();
    }
    // Complete-close guarantee: even when boot_core exited gracefully, a
    // still-running backend_pid would survive as an orphan.
    process::kill_managed_backend_processes_now(Some(child_pid));

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

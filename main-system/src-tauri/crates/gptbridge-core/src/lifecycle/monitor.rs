//! monitor.rs — backend supervision: child reaping, attached-backend probe
//! loop, and the bounded auto-restart queue.

use std::fs;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use super::backend::{self, BackendStatus};
use super::process;
use crate::app::{self, PRODUCT_VERSION};
use crate::ipc::{discovery, http};
use crate::native::paths::RuntimePathLibrary;
use crate::security::token;

const AUTO_RESTART_MAX_ATTEMPTS: u32 = 5;
const AUTO_RESTART_BASE_DELAY_MS: u64 = 2_000;
const AUTO_RESTART_MAX_DELAY_MS: u64 = 30_000;
const ATTACHED_MONITOR_INTERVAL_MS: u64 = 10_000;
const ATTACHED_MONITOR_FAILURE_LIMIT: u32 = 3;

fn monitor_started() -> &'static AtomicBool {
    static FLAG: OnceLock<AtomicBool> = OnceLock::new();
    FLAG.get_or_init(|| AtomicBool::new(false))
}

pub(crate) fn probe_existing_backend() -> bool {
    let port = discovery::resolve_backend_port();
    let Some(response) = http::get(
        discovery::LOOPBACK_HOST,
        port,
        discovery::BACKEND_HEALTH_PATH,
        &[],
        Duration::from_secs(8),
    ) else {
        return false;
    };
    let Ok(payload) = serde_json::from_slice::<serde_json::Value>(&response.body) else {
        return false;
    };
    payload["workspace_instance_id"].as_str() == Some(token::workspace_instance_id().as_str())
        && payload["version"].as_str() == Some(PRODUCT_VERSION)
        && payload["backend_runtime_ready"].as_bool() == Some(true)
}

pub(crate) fn has_live_supervisor(lib: &RuntimePathLibrary) -> bool {
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
            if pid > 0 && status != "stopped" && process::pid_alive(pid) {
                return true;
            }
        }
    }
    // The gateway answering /health for this workspace is authoritative
    // evidence that a supervisor is already running.
    discovery::is_gateway_alive()
}

/// Single supervisor thread: watches the owned child, runs the
/// attached-backend probe loop, and consumes the auto-restart queue.
pub(crate) fn ensure_monitor_thread() {
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
        // 500 ms tick: restart dispatch delays are ≥2 s and attached
        // probes run on the 10 s contract interval — a 200 ms tick only
        // burned idle wakeups.
        std::thread::sleep(Duration::from_millis(500));

        // Fire a pending auto-restart.
        if let Some(at) = pending_restart_at {
            if Instant::now() >= at {
                pending_restart_at = None;
                let manual = backend::state().lock().unwrap().manual_shutdown;
                if !manual {
                    app::report("backend.auto_restart.firing", serde_json::json!({}));
                    backend::start_backend_inner(true);
                }
            }
        }

        let mut s = backend::state().lock().unwrap();

        // Child liveness.
        if let Some(child) = s.child.as_mut() {
            match child.try_wait() {
                Ok(Some(exit)) => {
                    let code = exit.code();
                    s.child = None;
                    app::report(
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
                            app::report(
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
        // Attached-mode probes run on the 10s contract interval — the
        // 500 ms tick only keeps child reaping and restart dispatch
        // responsive.
        if Instant::now() < next_attached_probe {
            continue;
        }
        next_attached_probe = Instant::now() + Duration::from_millis(ATTACHED_MONITOR_INTERVAL_MS);
        drop(s);
        // Attached mode: re-check the attach invariant (healthy backend +
        // live supervisor); a sustained loss routes through auto-restart.
        let alive =
            probe_existing_backend() && has_live_supervisor(crate::native::paths::path_library());
        let mut s = backend::state().lock().unwrap();
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

pub(crate) fn schedule_auto_restart() {
    // The monitor thread owns restart timing; flag via status.
    ensure_monitor_thread();
}

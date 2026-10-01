//! resident.rs — governed resident-service supervision engine.
//!
//! One supervision pattern shared by every main-system-managed
//! companion process (A170: the supervisor lives outside the supervised
//! process).  Each service registers a ``ServiceSpec`` describing its
//! executable, arguments, governed environment, and restart contract;
//! the engine spawns it headless, reaps it, restarts inside the bounded
//! budget, and publishes truthful state/audit evidence.
//!
//! Current services:
//!
//! - ``channel-host``    — shared-layer ``background_service`` contract
//!   (``shared-layer/manifest.json``); C# A263 channel runtime.
//! - ``automation-host`` — the ``resident-core.json``
//!   ``periodic_scheduler`` (``GPTBridge.Automation.exe --watch``): one
//!   process hosting every ``automation-flows.json`` periodic flow —
//!   git sweep+sync, codex amendment intake / pin-sync / maintenance,
//!   and the six permission-automation flows — replacing the retired
//!   ``codex-pipeline`` and ``permission-host`` supervisors.
//!
//! State: ``main-system/runtime/state/<service>.json``; lifecycle events
//! append to the ``<service>`` audit ledger.

use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use crate::audit::append_audit_record;
use crate::tools::{spawn_hidden, workspace_root};

const REAP_INTERVAL: Duration = Duration::from_millis(500);
/// Deferred preflight cycles re-write state only on transition and once
/// per ``DEFER_STATE_REFRESH`` cycles (~1/min) — an externally-held
/// single-instance lock must not spin a state-file write every backoff
/// tick for the whole deferral window.
const DEFER_STATE_REFRESH: u32 = 60;

/// Governed resident-service contract handed to the supervisor.
pub struct ServiceSpec {
    /// Stable service identity — state file and audit ledger name.
    pub name: &'static str,
    pub entry: PathBuf,
    pub args: Vec<String>,
    pub working_dir: PathBuf,
    /// Explicit environment injected by the supervisor.
    pub env: Vec<(String, String)>,
    /// Environment keys passed through only when already set.
    pub env_allow: Vec<String>,
    /// Human-facing identity recorded in state/audit rows.
    pub label: String,
    pub auto_restart: bool,
    pub max_restart_attempts: u32,
    pub restart_backoff: Duration,
    pub force_close_suppresses_restart: bool,
    /// Optional pre-spawn gate.  ``Some(reason)`` defers this cycle for
    /// one backoff tick without consuming the restart budget — e.g. an
    /// externally-launched instance already holds the single-instance
    /// lock; supervision takes over once it exits.
    pub preflight: Option<fn() -> Option<String>>,
}

struct Service {
    stop: AtomicBool,
    started: AtomicBool,
    child: Mutex<Option<Child>>,
}

fn registry() -> &'static Mutex<HashMap<&'static str, Arc<Service>>> {
    static REGISTRY: OnceLock<Mutex<HashMap<&'static str, Arc<Service>>>> =
        OnceLock::new();
    REGISTRY.get_or_init(|| Mutex::new(HashMap::new()))
}

fn service(name: &'static str) -> Arc<Service> {
    let mut map = registry().lock().unwrap_or_else(|e| e.into_inner());
    Arc::clone(map.entry(name).or_insert_with(|| {
        Arc::new(Service {
            stop: AtomicBool::new(false),
            started: AtomicBool::new(false),
            child: Mutex::new(None),
        })
    }))
}

fn state_dir() -> PathBuf {
    workspace_root()
        .join("main-system")
        .join("runtime")
        .join("state")
}

fn write_state(spec: &ServiceSpec, status: &str, pid: u32, attempts: u32) {
    let dir = state_dir();
    let _ = std::fs::create_dir_all(&dir);
    let state = json!({
        "role": spec.name,
        "status": status,
        "pid": pid,
        "label": spec.label,
        "restart_attempts": attempts,
        "managed_by": "main-system",
        "updated_at": SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64(),
    });
    let tmp = dir.join(format!("{}.json.tmp", spec.name));
    if std::fs::write(&tmp, state.to_string()).is_ok() {
        let _ = std::fs::rename(&tmp, dir.join(format!("{}.json", spec.name)));
    }
}

fn audit(spec: &ServiceSpec, event: &str, payload: Value) {
    let mut record = payload;
    record["event"] = json!(format!("{}.{event}", spec.name));
    record["label"] = json!(spec.label);
    append_audit_record(spec.name, record);
}

fn spawn_service(spec: &ServiceSpec) -> Result<Child, String> {
    let mut command = std::process::Command::new(&spec.entry);
    command
        .current_dir(&spec.working_dir)
        .args(&spec.args)
        .env(
            "GPTBRIDGE_GOVERNANCE_PROJECT_ROOT",
            workspace_root().to_string_lossy().as_ref(),
        );
    for (key, value) in &spec.env {
        command.env(key, value);
    }
    // Manifest-declared passthrough: only the allow-listed keys.
    for key in &spec.env_allow {
        if let Ok(value) = std::env::var(key) {
            command.env(key, value);
        }
    }
    command.env("PATH", std::env::var("PATH").unwrap_or_default());
    spawn_hidden(command)
}

/// Wait for the child, polling so a governed stop is reaped promptly.
/// Returns ``Some(exit_code)`` on child exit or ``None`` when stopped.
fn await_child(service: &Service, child: &mut Child) -> Option<Option<i32>> {
    loop {
        if service.stop.load(Ordering::SeqCst) {
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        match child.try_wait() {
            Ok(Some(status)) => return Some(status.code()),
            Ok(None) => std::thread::sleep(REAP_INTERVAL),
            Err(_) => return Some(None),
        }
    }
}

/// One supervision cycle outcome.
enum Cycle {
    Restart,
    Stop,
}

fn supervise(spec: ServiceSpec, service: Arc<Service>) {
    let mut attempts = 0u32;
    let mut deferred = 0u32;
    while !service.stop.load(Ordering::SeqCst) {
        match run_cycle(&spec, &service, &mut attempts, &mut deferred) {
            Cycle::Restart => continue,
            Cycle::Stop => break,
        }
    }
}

/// Spawn → await → classify one service lifetime.  Bounded by the
/// contract restart budget; a governed stop is always honored.
fn run_cycle(
    spec: &ServiceSpec,
    service: &Service,
    attempts: &mut u32,
    deferred: &mut u32,
) -> Cycle {
    if let Some(gate) = spec.preflight {
        if let Some(reason) = gate() {
            if *deferred % DEFER_STATE_REFRESH == 0 {
                write_state(spec, "deferred", 0, *attempts);
            }
            if *deferred == 0 {
                audit(spec, "spawn-deferred", json!({ "reason": reason }));
            }
            *deferred = deferred.saturating_add(1);
            std::thread::sleep(spec.restart_backoff);
            return Cycle::Restart;
        }
    }
    *deferred = 0;
    let child = match spawn_service(spec) {
        Ok(c) => c,
        Err(code) => {
            audit(spec, "spawn-failed", json!({ "error_code": code }));
            write_state(spec, "spawn-failed", 0, *attempts);
            if !spec.auto_restart || *attempts >= spec.max_restart_attempts
            {
                return Cycle::Stop;
            }
            *attempts += 1;
            std::thread::sleep(spec.restart_backoff);
            return Cycle::Restart;
        }
    };
    let pid = child.id();
    *service.child.lock().unwrap_or_else(|e| e.into_inner()) = Some(child);
    write_state(spec, "running", pid, *attempts);
    audit(spec, "spawned",
        json!({ "pid": pid, "attempt": *attempts }));
    let exit = {
        let mut slot =
            service.child.lock().unwrap_or_else(|e| e.into_inner());
        match slot.as_mut() {
            Some(child) => await_child(service, child),
            None => None,
        }
    };
    *service.child.lock().unwrap_or_else(|e| e.into_inner()) = None;
    classify_exit(spec, pid, exit, attempts)
}

/// Classify a child exit against the restart contract.
fn classify_exit(
    spec: &ServiceSpec,
    pid: u32,
    exit: Option<Option<i32>>,
    attempts: &mut u32,
) -> Cycle {
    let Some(code) = exit else {
        write_state(spec, "stopped", 0, *attempts);
        audit(spec, "stopped", json!({ "pid": pid }));
        return Cycle::Stop;
    };
    audit(spec, "exited", json!({ "pid": pid, "code": code }));
    write_state(spec, "exited", 0, *attempts);
    let clean = code == Some(0);
    let suppressed =
        spec.force_close_suppresses_restart && code == Some(1);
    if clean || !spec.auto_restart || suppressed {
        write_state(spec, "stopped", 0, *attempts);
        return Cycle::Stop;
    }
    if *attempts >= spec.max_restart_attempts {
        write_state(spec, "exhausted", 0, *attempts);
        audit(spec, "restart-exhausted",
            json!({ "max_restart_attempts": spec.max_restart_attempts }));
        return Cycle::Stop;
    }
    *attempts += 1;
    audit(spec, "restart-scheduled",
        json!({ "attempt": *attempts, "backoff_s": spec.restart_backoff.as_secs() }));
    std::thread::sleep(spec.restart_backoff);
    Cycle::Restart
}

/// Start supervision for a service.  Idempotent per service name; the
/// caller is responsible for resolving the spec (fail-closed before
/// this call — an unserviceable contract must never reach spawn).
pub fn start(spec: ServiceSpec) {
    let service = service(spec.name);
    if service.started.swap(true, Ordering::SeqCst) {
        return;
    }
    audit(&spec, "supervisor-start", json!({}));
    std::thread::spawn(move || supervise(spec, service));
}

/// Record a fail-closed terminal state for a service whose contract
/// could not be satisfied (audited, no supervisor, no spawn).
pub fn unavailable(name: &'static str, label: &str, error_code: &str) {
    let spec = ServiceSpec {
        name,
        entry: PathBuf::new(),
        args: Vec::new(),
        working_dir: PathBuf::new(),
        env: Vec::new(),
        env_allow: Vec::new(),
        label: label.to_string(),
        auto_restart: false,
        max_restart_attempts: 0,
        restart_backoff: Duration::ZERO,
        force_close_suppresses_restart: true,
        preflight: None,
    };
    audit(&spec, "unavailable", json!({ "error_code": error_code }));
    write_state(&spec, "unavailable", 0, 0);
}

/// Governed shutdown of one service: suppress restarts and tear its
/// child down.  Used by per-service ``stop()`` wrappers.
#[allow(dead_code)]
pub fn stop(name: &'static str) {
    let svc = {
        let map = registry().lock().unwrap_or_else(|e| e.into_inner());
        map.get(name).map(Arc::clone)
    };
    let Some(service) = svc else { return };
    service.stop.store(true, Ordering::SeqCst);
    let mut slot =
        service.child.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(child) = slot.as_mut() {
        let _ = child.kill();
        let _ = child.wait();
    }
    *slot = None;
}

/// Governed shutdown of every supervised service: suppress restarts and
/// tear children down before the backend exits so companions never
/// outlive the IPC plane they are bound to.
pub fn stop_all() {
    let services: Vec<Arc<Service>> = registry()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .values()
        .map(Arc::clone)
        .collect();
    for service in services {
        service.stop.store(true, Ordering::SeqCst);
        let mut slot =
            service.child.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(child) = slot.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
        *slot = None;
    }
}

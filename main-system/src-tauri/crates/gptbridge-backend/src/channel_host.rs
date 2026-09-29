//! channel_host.rs — channel-layer automation (resident companion service).
//!
//! Owns the governed ``shared-layer/manifest.json::background_service``
//! contract for the information-channel gateway
//! (``GPTBridge.ChannelHost.exe``, the C# A263 channel runtime):
//!
//! - spawned by main-system after the IPC listener binds, with the
//!   authenticated WebSocket URL injected via
//!   ``GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL``;
//! - supervised by a dedicated thread: ``auto_restart`` +
//!   ``max_restart_attempts`` + ``restart_backoff_seconds`` are honored
//!   exactly; ``explicit_force_close_suppresses_restart`` decides whether a
//!   killed child is respawned;
//! - fail-closed: a missing/retired runtime entry or an exhausted restart
//!   budget ends supervision with an audited terminal state — never an
//!   unbounded retry loop;
//! - governed shutdown (``stop()``) suppresses restarts and tears the
//!   child down before the backend exits;
//! - state is published to ``main-system/runtime/state/channel-host.json``
//!   and lifecycle events append to ``channel-host.jsonl``.

use std::path::PathBuf;
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use gptbridge_core::security::token;

use crate::audit::append_audit_record;
use crate::tools::{spawn_hidden, workspace_root};

const CHANNEL_ID_DEFAULT: &str = "system";
const MANIFEST_PATH: &str = "shared-layer/manifest.json";
const STATE_FILE: &str = "channel-host.json";
const AUDIT_LEDGER: &str = "channel-host";
const REAP_INTERVAL: Duration = Duration::from_millis(200);

fn stop_flag() -> &'static AtomicBool {
    static FLAG: OnceLock<AtomicBool> = OnceLock::new();
    FLAG.get_or_init(|| AtomicBool::new(false))
}

fn started_flag() -> &'static AtomicBool {
    static FLAG: OnceLock<AtomicBool> = OnceLock::new();
    FLAG.get_or_init(|| AtomicBool::new(false))
}

fn child_slot() -> &'static Mutex<Option<Child>> {
    static SLOT: OnceLock<Mutex<Option<Child>>> = OnceLock::new();
    SLOT.get_or_init(|| Mutex::new(None))
}

struct BackgroundContract {
    entry: PathBuf,
    working_dir: PathBuf,
    auto_restart: bool,
    max_restart_attempts: u32,
    restart_backoff: Duration,
    force_close_suppresses_restart: bool,
    channel_id: String,
    env_allow: Vec<String>,
}

fn manifest_path() -> PathBuf {
    workspace_root().join(MANIFEST_PATH)
}

/// Resolve the governed manifest contract.  Every malformed or retired
/// field fails closed with ``Err(reason)`` — the supervisor never guesses.
fn load_contract() -> Result<BackgroundContract, String> {
    let raw = std::fs::read_to_string(manifest_path())
        .map_err(|e| format!("CHANNEL_MANIFEST_UNAVAILABLE:{e}"))?;
    let manifest: Value = serde_json::from_str(&raw)
        .map_err(|e| format!("CHANNEL_MANIFEST_INVALID:{e}"))?;
    if manifest["enabled"].as_bool() == Some(false) {
        return Err("CHANNEL_HOST_DISABLED".to_string());
    }
    let svc = &manifest["background_service"];
    if svc["managed_by"].as_str() != Some("main-system") {
        return Err("CHANNEL_OWNER_MISMATCH".to_string());
    }
    let (entry, working_dir) = resolve_entry(&manifest)?;
    let env_allow = manifest["environment"]["allow"]
        .as_array()
        .map(|a| {
            a.iter().filter_map(|v| v.as_str().map(String::from)).collect()
        })
        .unwrap_or_default();
    Ok(BackgroundContract {
        entry,
        working_dir,
        auto_restart: svc["auto_restart"].as_bool().unwrap_or(false),
        max_restart_attempts: svc["max_restart_attempts"].as_u64().unwrap_or(0) as u32,
        restart_backoff: Duration::from_secs(
            svc["restart_backoff_seconds"].as_u64().unwrap_or(1),
        ),
        force_close_suppresses_restart: svc
            ["explicit_force_close_suppresses_restart"]
            .as_bool()
            .unwrap_or(true),
        channel_id: std::env::var("GPTBRIDGE_CHANNEL_ID")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .unwrap_or_else(|| CHANNEL_ID_DEFAULT.to_string()),
        env_allow,
    })
}

/// Resolve the canonical native entry — fail-closed for retired or
/// missing runtime declarations.
fn resolve_entry(manifest: &Value) -> Result<(PathBuf, PathBuf), String> {
    let runtime = &manifest["runtime"];
    if runtime["type"].as_str() == Some("retired-python") {
        return Err("CHANNEL_RUNTIME_RETIRED".to_string());
    }
    let rel = runtime["native_entry"]
        .as_str()
        .or_else(|| runtime["entry"].as_str())
        .ok_or_else(|| "CHANNEL_ENTRY_MISSING".to_string())?;
    if rel.is_empty() || rel.ends_with(".py") {
        return Err("CHANNEL_ENTRY_RETIRED".to_string());
    }
    let shared_root = workspace_root().join("shared-layer");
    let entry = shared_root.join(rel);
    if !entry.is_file() {
        return Err(format!("CHANNEL_ENTRY_UNAVAILABLE:{rel}"));
    }
    Ok((entry, shared_root))
}

fn state_dir() -> PathBuf {
    workspace_root()
        .join("main-system")
        .join("runtime")
        .join("state")
}

fn write_state(status: &str, pid: u32, attempts: u32, channel_id: &str) {
    let dir = state_dir();
    let _ = std::fs::create_dir_all(&dir);
    let state = json!({
        "role": "channel-host",
        "status": status,
        "pid": pid,
        "channel_id": channel_id,
        "restart_attempts": attempts,
        "managed_by": "main-system",
        "updated_at": SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64(),
    });
    let tmp = dir.join(format!("{STATE_FILE}.tmp"));
    if std::fs::write(&tmp, state.to_string()).is_ok() {
        let _ = std::fs::rename(&tmp, dir.join(STATE_FILE));
    }
}

fn audit(event: &str, channel_id: &str, payload: Value) {
    let mut record = payload;
    record["event"] = json!(format!("channel-host.{event}"));
    record["channel_id"] = json!(channel_id);
    append_audit_record(AUDIT_LEDGER, record);
}

/// Authenticated WebSocket URL the channel host connects to — the same
/// ``?token=<session>&instance=<workspace-instance>`` contract the backend
/// itself enforces in ``auth::authorize_websocket``.
fn websocket_url(port: u16) -> Result<String, String> {
    let token_value = std::env::var("GPTBRIDGE_IPC_SESSION_TOKEN")
        .ok()
        .map(|t| t.trim().to_lowercase())
        .filter(|t| t.len() == 64 && t.chars().all(|c| c.is_ascii_hexdigit()))
        .map(Ok)
        .unwrap_or_else(token::backend_session_token)?;
    Ok(format!(
        "ws://127.0.0.1:{port}/?token={token_value}&instance={}",
        token::workspace_instance_id()
    ))
}

fn spawn_host(contract: &BackgroundContract, url: &str) -> Result<Child, String> {
    let mut command = std::process::Command::new(&contract.entry);
    command
        .current_dir(&contract.working_dir)
        .env("GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL", url)
        .env("GPTBRIDGE_CHANNEL_ID", &contract.channel_id)
        .env(
            "GPTBRIDGE_GOVERNANCE_PROJECT_ROOT",
            workspace_root().to_string_lossy().as_ref(),
        );
    // Manifest ``environment.allow`` — only the declared keys pass through.
    for key in &contract.env_allow {
        if let Ok(value) = std::env::var(key) {
            command.env(key, value);
        }
    }
    command.env("PATH", std::env::var("PATH").unwrap_or_default());
    spawn_hidden(command)
}

/// Wait for the child, polling so a governed stop is reaped promptly.
/// Returns ``Some(exit_code)`` on child exit or ``None`` when stopped.
fn await_child(child: &mut Child) -> Option<Option<i32>> {
    loop {
        if stop_flag().load(Ordering::SeqCst) {
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

fn supervise(contract: BackgroundContract, url: String) {
    let mut attempts = 0u32;
    while !stop_flag().load(Ordering::SeqCst) {
        match run_cycle(&contract, &url, &mut attempts) {
            Cycle::Restart => continue,
            Cycle::Stop => break,
        }
    }
}

/// Spawn → await → classify one channel-host lifetime.  Bounded by the
/// manifest restart budget; a governed stop is always honored.
fn run_cycle(
    contract: &BackgroundContract,
    url: &str,
    attempts: &mut u32,
) -> Cycle {
    let child = match spawn_host(contract, url) {
        Ok(c) => c,
        Err(code) => {
            audit("spawn-failed", &contract.channel_id,
                json!({ "error_code": code }));
            write_state("spawn-failed", 0, *attempts, &contract.channel_id);
            if !contract.auto_restart || *attempts >= contract.max_restart_attempts {
                return Cycle::Stop;
            }
            *attempts += 1;
            std::thread::sleep(contract.restart_backoff);
            return Cycle::Restart;
        }
    };
    let pid = child.id();
    *child_slot().lock().unwrap_or_else(|e| e.into_inner()) = Some(child);
    write_state("running", pid, *attempts, &contract.channel_id);
    audit("spawned", &contract.channel_id,
        json!({ "pid": pid, "attempt": *attempts }));
    let exit = {
        let mut slot = child_slot().lock().unwrap_or_else(|e| e.into_inner());
        match slot.as_mut() {
            Some(child) => await_child(child),
            None => None,
        }
    };
    *child_slot().lock().unwrap_or_else(|e| e.into_inner()) = None;
    classify_exit(contract, pid, exit, attempts)
}

/// Classify a child exit against the manifest restart contract.
fn classify_exit(
    contract: &BackgroundContract,
    pid: u32,
    exit: Option<Option<i32>>,
    attempts: &mut u32,
) -> Cycle {
    let Some(code) = exit else {
        write_state("stopped", 0, *attempts, &contract.channel_id);
        audit("stopped", &contract.channel_id, json!({ "pid": pid }));
        return Cycle::Stop;
    };
    audit("exited", &contract.channel_id, json!({ "pid": pid, "code": code }));
    write_state("exited", 0, *attempts, &contract.channel_id);
    let clean = code == Some(0);
    let suppressed = contract.force_close_suppresses_restart && code == Some(1);
    if clean || !contract.auto_restart || suppressed {
        write_state("stopped", 0, *attempts, &contract.channel_id);
        return Cycle::Stop;
    }
    if *attempts >= contract.max_restart_attempts {
        write_state("exhausted", 0, *attempts, &contract.channel_id);
        audit("restart-exhausted", &contract.channel_id,
            json!({ "max_restart_attempts": contract.max_restart_attempts }));
        return Cycle::Stop;
    }
    *attempts += 1;
    audit("restart-scheduled", &contract.channel_id,
        json!({ "attempt": *attempts, "backoff_s": contract.restart_backoff.as_secs() }));
    std::thread::sleep(contract.restart_backoff);
    Cycle::Restart
}

/// Start the channel-layer supervision thread.  Idempotent; every
/// precondition failure is fail-closed (audited, no supervisor, no spawn).
pub fn start(port: u16) {
    if started_flag().swap(true, Ordering::SeqCst) {
        return;
    }
    let contract = match load_contract() {
        Ok(c) => c,
        Err(code) => {
            audit("unavailable", CHANNEL_ID_DEFAULT,
                json!({ "error_code": code }));
            write_state("unavailable", 0, 0, CHANNEL_ID_DEFAULT);
            return;
        }
    };
    let url = match websocket_url(port) {
        Ok(u) => u,
        Err(code) => {
            audit("unavailable", &contract.channel_id,
                json!({ "error_code": code }));
            write_state("unavailable", 0, 0, &contract.channel_id);
            return;
        }
    };
    audit("supervisor-start", &contract.channel_id, json!({ "port": port }));
    std::thread::spawn(move || supervise(contract, url));
}

/// Governed shutdown: suppress restarts and tear the child down.  Called
/// before the backend exits so the companion process never outlives the
/// IPC plane it is bound to.
pub fn stop() {
    stop_flag().store(true, Ordering::SeqCst);
    let mut slot = child_slot().lock().unwrap_or_else(|e| e.into_inner());
    if let Some(child) = slot.as_mut() {
        let _ = child.kill();
        let _ = child.wait();
    }
    *slot = None;
    write_state("stopped", 0, 0, CHANNEL_ID_DEFAULT);
    audit("supervisor-stop", CHANNEL_ID_DEFAULT, json!({}));
}

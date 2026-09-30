//! channel_host.rs — channel-layer automation contract resolution.
//!
//! Resolves the governed ``shared-layer/manifest.json::background_service``
//! contract for the information-channel gateway
//! (``GPTBridge.ChannelHost.exe``, the C# A263 channel runtime) into a
//! ``resident::ServiceSpec`` and hands it to the shared supervision
//! engine:
//!
//! - spawned by main-system after the IPC listener binds, with the
//!   authenticated WebSocket URL injected via
//!   ``GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL``;
//! - ``auto_restart`` / ``max_restart_attempts`` /
//!   ``restart_backoff_seconds`` / ``explicit_force_close_suppresses_restart``
//!   are honored exactly by the resident engine;
//! - fail-closed: a missing/retired runtime entry ends supervision with
//!   an audited terminal state — never an unbounded retry loop;
//! - state publishes to ``main-system/runtime/state/channel-host.json``
//!   and lifecycle events append to the ``channel-host`` audit ledger.

use std::path::PathBuf;
use std::time::Duration;

use serde_json::Value;

use gptbridge_core::security::token;

use crate::resident::{self, ServiceSpec};
use crate::tools::workspace_root;

const CHANNEL_ID_DEFAULT: &str = "system";
const MANIFEST_PATH: &str = "shared-layer/manifest.json";
const SERVICE_NAME: &str = "channel-host";

fn manifest_path() -> PathBuf {
    workspace_root().join(MANIFEST_PATH)
}

/// Resolve the governed manifest contract.  Every malformed or retired
/// field fails closed with ``Err(reason)`` — the supervisor never guesses.
fn load_contract() -> Result<ServiceSpec, String> {
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
    Ok(ServiceSpec {
        name: SERVICE_NAME,
        entry,
        args: Vec::new(),
        working_dir,
        env: Vec::new(),
        env_allow,
        label: std::env::var("GPTBRIDGE_CHANNEL_ID")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .unwrap_or_else(|| CHANNEL_ID_DEFAULT.to_string()),
        auto_restart: svc["auto_restart"].as_bool().unwrap_or(false),
        max_restart_attempts: svc["max_restart_attempts"].as_u64().unwrap_or(0) as u32,
        restart_backoff: Duration::from_secs(
            svc["restart_backoff_seconds"].as_u64().unwrap_or(1),
        ),
        force_close_suppresses_restart: svc
            ["explicit_force_close_suppresses_restart"]
            .as_bool()
            .unwrap_or(true),
        preflight: None,
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

/// Start channel-layer supervision.  Every precondition failure is
/// fail-closed (audited terminal state, no supervisor, no spawn).
pub fn start(port: u16) {
    let mut spec = match load_contract() {
        Ok(s) => s,
        Err(code) => {
            resident::unavailable(SERVICE_NAME, CHANNEL_ID_DEFAULT, &code);
            return;
        }
    };
    match websocket_url(port) {
        Ok(url) => {
            spec.env.push((
                "GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL".to_string(),
                url,
            ));
            spec.env.push((
                "GPTBRIDGE_CHANNEL_ID".to_string(),
                spec.label.clone(),
            ));
            resident::start(spec);
        }
        Err(code) => {
            resident::unavailable(SERVICE_NAME, &spec.label, &code);
        }
    }
}

/// Governed shutdown — suppress restarts and tear the channel host
/// down before the backend exits so the companion never outlives the
/// IPC plane it is bound to.  Kept as the per-service shutdown entry
/// point; main() calls ``resident::stop_all`` directly.
#[allow(dead_code)]
pub fn stop() {
    resident::stop(SERVICE_NAME);
}

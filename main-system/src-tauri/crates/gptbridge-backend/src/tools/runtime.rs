//! Runtime entry resolution + health probing.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::Value;

use gptbridge_core::ipc::http;
use gptbridge_core::security;

const RUNTIME_PROBE_INTERVAL: Duration = Duration::from_millis(20);

/// Resolve ``runtime.native_entry`` — the governed C#/Rust host exe bound
/// to the tool root.  Mirrors ``resolve_special_unpacked_entry``: the
/// declared .py ``request_channel.runtime_entry`` is retired (B38/E180),
/// so absence of a native entry fails closed.
pub(super) fn resolve_native_entry(
    owner_root: &Path,
    owner_manifest: &Value,
) -> Result<PathBuf, String> {
    let raw = owner_manifest["runtime"]["native_entry"]
        .as_str()
        .unwrap_or_default()
        .trim();
    if raw.is_empty() {
        return Err("TOOL_RUNTIME_RETIRED:python-entry-pending-native-host".into());
    }
    let rel = Path::new(raw);
    if rel.is_absolute() || rel.components().any(|c| matches!(c, std::path::Component::ParentDir)) {
        return Err("PERMISSION_DENIED:native-entry-escapes-tool-root".into());
    }
    let candidate = owner_root.join(rel);
    if candidate.extension().map(|e| e == "exe") != Some(true) {
        return Err("PERMISSION_DENIED:native-entry-not-exe".into());
    }
    let canonical_root = owner_root
        .canonicalize()
        .map_err(|e| format!("TOOL_ROOT_INVALID:{e}"))?;
    let canonical = candidate
        .canonicalize()
        .map_err(|_| format!("LAUNCH_TARGET_MISSING:{raw}"))?;
    if !canonical.starts_with(&canonical_root) {
        return Err("PERMISSION_DENIED:native-entry-escapes-tool-root".into());
    }
    Ok(canonical)
}

pub(super) fn has_governed_source_runtime(manifest: &Value) -> bool {
    let launch = &manifest["launch"];
    let channel = &manifest["request_channel"];
    let special_unpacked = manifest["distribution"]["mode"].as_str()
        == Some("special-unpackaged")
        && manifest["distribution"]["package"].as_bool() == Some(false);
    let governed_channel = launch["background"].as_str() == Some("governed-source-channel")
        && channel["model"].as_str() == Some("governance-authenticated-shared-layer")
        && channel["direct_instruction"].as_str() == Some("PERMISSION_DENIED")
        && channel["runtime_entry"].as_str().map(|s| !s.trim().is_empty()) == Some(true);
    special_unpacked || governed_channel
}

/// Poll the tool runtime's ``/health`` until it reports the governed
/// contract (ok + governance_ready + tool_id + workspace_instance_id),
/// or the open budget expires — mirrors ``_check_source_runtime_ready``.
pub(super) fn await_runtime_ready(
    port: u16,
    governed_id: &str,
    deadline: Instant,
) -> bool {
    let instance = security::workspace_instance_id();
    while Instant::now() < deadline {
        if let Some(response) = http::get(
            "127.0.0.1",
            port,
            "/health",
            &[("Connection", "close")],
            Duration::from_millis(750),
        ) {
            if let Ok(payload) = serde_json::from_slice::<Value>(&response.body) {
                let ready = payload["ok"].as_bool() == Some(true)
                    && payload["governance_ready"].as_bool() == Some(true)
                    && payload["tool_id"].as_str() == Some(governed_id)
                    && payload["workspace_instance_id"].as_str() == Some(instance.as_str());
                if ready {
                    return true;
                }
            }
        }
        std::thread::sleep(RUNTIME_PROBE_INTERVAL);
    }
    false
}

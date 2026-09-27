//! discovery.rs — backend endpoint resolution and session descriptors
//! (second half of the retired ``ipcSession.ts`` port).
//!
//! Resolves the live backend gateway port without assuming a fixed number —
//! configured manifest port, boot_core's recorded active port, then +1/+2
//! generation offsets — and builds the WebSocket session descriptor the
//! renderer consumes.

use std::fs;
use std::time::Duration;

use super::http;
use crate::native::paths;
use crate::security::{ticket, token};

const DEFAULT_GATEWAY_PORT: u16 = 8765;
const GENERATION_PORT_OFFSETS: [u16; 2] = [1, 2];
const HEALTH_PROBE_TIMEOUT_MS: u64 = 350;
pub const LOOPBACK_HOST: &str = "127.0.0.1";
pub const BACKEND_HEALTH_PATH: &str = "/health?brief=1";

fn read_configured_gateway_port() -> u16 {
    let manifest_path = paths::path_library()
        .workspace_root
        .join("main-system")
        .join("config")
        .join("startup_manifest.json");
    fs::read_to_string(manifest_path)
        .ok()
        .and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
        .and_then(|m| m["ports"]["health_probe"].as_u64())
        .filter(|p| *p > 0 && *p <= 65535)
        .map(|p| p as u16)
        .unwrap_or(DEFAULT_GATEWAY_PORT)
}

fn read_active_backend_port() -> Option<u16> {
    let state_path = paths::path_library()
        .workspace_root
        .join("main-system")
        .join("runtime")
        .join("state")
        .join("boot-core.json");
    fs::read_to_string(state_path)
        .ok()
        .and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
        .and_then(|s| s["active_backend_port"].as_u64())
        .filter(|p| *p > 0 && *p <= 65535)
        .map(|p| p as u16)
}

fn probe_backend_port(port: u16) -> bool {
    let Some(response) = http::get(
        LOOPBACK_HOST,
        port,
        BACKEND_HEALTH_PATH,
        &[],
        Duration::from_millis(HEALTH_PROBE_TIMEOUT_MS),
    ) else {
        return false;
    };
    serde_json::from_slice::<serde_json::Value>(&response.body)
        .ok()
        .and_then(|p| p["workspace_instance_id"].as_str().map(String::from))
        .map(|id| id == token::workspace_instance_id())
        .unwrap_or(false)
}

/// Resolve the live backend endpoint without assuming a fixed port.
/// Probes candidates in parallel; falls back to the configured gateway.
pub fn resolve_backend_port() -> u16 {
    let configured = read_configured_gateway_port();
    let mut candidates: Vec<u16> = vec![configured];
    if let Some(active) = read_active_backend_port() {
        candidates.push(active);
    }
    for offset in GENERATION_PORT_OFFSETS {
        candidates.push(configured.saturating_add(offset));
    }
    candidates.sort_unstable();
    candidates.dedup();

    let results: Vec<bool> = std::thread::scope(|s| {
        candidates
            .iter()
            .map(|port| s.spawn(move || probe_backend_port(*port)))
            .collect::<Vec<_>>()
            .into_iter()
            .map(|h| h.join().unwrap_or(false))
            .collect()
    });
    candidates
        .iter()
        .zip(results.iter())
        .find(|(_, ok)| **ok)
        .map(|(port, _)| *port)
        .unwrap_or(configured)
}

/// Probe the configured gateway port for a live boot_core.
pub fn is_gateway_alive() -> bool {
    probe_backend_port(read_configured_gateway_port())
}

/// Session descriptor consumed by a UI surface to open the authenticated
/// WebSocket channel to the backend.
pub fn backend_session_descriptor() -> serde_json::Value {
    let session_token = token::backend_session_token().unwrap_or_default();
    let instance_id = token::workspace_instance_id();
    let port = resolve_backend_port();
    let ticket_value = ticket::create_websocket_session_ticket(&session_token, &instance_id);
    let ticket_enc = ticket::url_encode(&ticket_value);
    let instance_enc = ticket::url_encode(&instance_id);
    serde_json::json!({
        "workspaceInstanceId": instance_id,
        "websocketUrl": format!("ws://{LOOPBACK_HOST}:{port}/?ticket={ticket_enc}&instance={instance_enc}"),
    })
}

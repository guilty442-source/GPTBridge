//! auth.rs — IPC session-token / one-ticket authorization.
//!
//! Wire-compatible with the retired Python ``server_tokens`` contract:
//! ``?token=<session-token>&instance=<workspace-instance>`` or the
//! one-time ``?ticket=<expires>.<nonce>.<instance>.<hmac-sha256>`` form
//! issued by ``security::ticket``.  The session token lives in the
//! shared IPC state root so the shell, tools, and this backend agree on
//! the same credential without an interpreter hop.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use gptbridge_core::security::{self, token};
use hmac::{Hmac, Mac};
use sha2::Sha256;

const TICKET_TTL_MAX_SKEW_SECS: u64 = 0; // tickets are already future-dated
type HmacSha256 = Hmac<Sha256>;

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn used_nonces() -> &'static Mutex<HashMap<String, u64>> {
    static NONCES: std::sync::OnceLock<Mutex<HashMap<String, u64>>> =
        std::sync::OnceLock::new();
    NONCES.get_or_init(|| Mutex::new(HashMap::new()))
}

/// Resolve the expected session token: env override first (spawned by
/// the shell), then the shared state-root file — same precedence as the
/// retired Python backend.
fn expected_session_token() -> Option<String> {
    if let Ok(env) = std::env::var("GPTBRIDGE_IPC_SESSION_TOKEN") {
        let t = env.trim().to_lowercase();
        if t.len() == 64 && t.chars().all(|c| c.is_ascii_hexdigit()) {
            return Some(t);
        }
    }
    token::backend_session_token().ok()
}

fn verify_ticket(ticket: &str, provided_instance: &str, token: &str) -> bool {
    let parts: Vec<&str> = ticket.split('.').collect();
    if parts.len() != 4 {
        return false;
    }
    let (expires_text, nonce, ticket_instance, signature) =
        (parts[0], parts[1], parts[2], parts[3]);
    let Ok(expires_at) = expires_text.parse::<u64>() else {
        return false;
    };
    if expires_at < now_secs() + TICKET_TTL_MAX_SKEW_SECS
        || ticket_instance != provided_instance
    {
        return false;
    }
    let payload = format!("{expires_at}.{nonce}.{ticket_instance}");
    let mut mac =
        <HmacSha256 as Mac>::new_from_slice(token.as_bytes()).expect("HMAC any key size");
    mac.update(payload.as_bytes());
    if !security::constant_time_eq(
        hex::encode(mac.finalize().into_bytes()).as_bytes(),
        signature.as_bytes(),
    ) {
        return false;
    }
    // One-time ticket: reject nonce replay.
    let mut nonces = used_nonces().lock().unwrap_or_else(|e| e.into_inner());
    nonces.retain(|_, &mut until| until >= now_secs());
    if nonces.contains_key(nonce) {
        return false;
    }
    nonces.insert(nonce.to_string(), expires_at);
    true
}

/// Authorize a WebSocket upgrade request — mirrors the Python
/// ``_websocket_request_authorized`` semantics exactly.
pub fn authorize_websocket(query: &HashMap<String, String>) -> bool {
    let Some(expected) = expected_session_token() else {
        return false;
    };
    let provided_instance = query.get("instance").map(String::as_str).unwrap_or("");
    if provided_instance.is_empty()
        || !security::constant_time_eq(
            provided_instance.as_bytes(),
            token::workspace_instance_id().as_bytes(),
        )
    {
        return false;
    }
    if let Some(provided) = query.get("token") {
        if security::constant_time_eq(provided.trim().as_bytes(), expected.as_bytes()) {
            return true;
        }
    }
    if let Some(ticket) = query.get("ticket") {
        return verify_ticket(ticket.trim(), provided_instance, &expected);
    }
    false
}

/// Authorize the ``/shutdown`` request via its dedicated token header.
pub fn authorize_shutdown(header_value: Option<&str>) -> bool {
    let expected = std::env::var("GPTBRIDGE_SHUTDOWN_TOKEN").unwrap_or_default();
    let expected = expected.trim();
    if expected.is_empty() {
        return false;
    }
    match header_value {
        Some(provided) => {
            !provided.is_empty()
                && security::constant_time_eq(provided.as_bytes(), expected.as_bytes())
        }
        None => false,
    }
}

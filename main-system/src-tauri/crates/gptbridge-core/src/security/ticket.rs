//! ticket.rs — HMAC-SHA256 websocket session ticket.
//!
//! The renderer opens the backend WebSocket with a short-lived signed
//! ticket: ``expires_at.nonce.workspace_instance_id.signature`` where the
//! signature is HMAC-SHA256 over the payload keyed by the IPC capability
//! token.

use hmac::{Hmac, Mac};
use sha2::Sha256;

use super::random_hex;

/// Signed session ticket valid for 30 s — consumed once by the backend's
/// WebSocket handshake.
pub fn create_websocket_session_ticket(token: &str, workspace_instance_id: &str) -> String {
    let expires_at = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
        + 30;
    let nonce = random_hex(16);
    let payload = format!("{expires_at}.{nonce}.{workspace_instance_id}");
    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(token.as_bytes())
        .expect("HMAC accepts any key size");
    mac.update(payload.as_bytes());
    let signature = hex::encode(mac.finalize().into_bytes());
    format!("{payload}.{signature}")
}

/// RFC-3986 unreserved-character percent encoding.
pub fn url_encode(input: &str) -> String {
    input
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || "-._~".contains(c) {
                c.to_string()
            } else {
                format!("%{:02X}", c as u32)
            }
        })
        .collect()
}

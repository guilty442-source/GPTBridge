//! Security domain — IPC capability tokens, session tickets, and shared
//! constant-time / identifier-sanitising helpers.

pub mod ticket;
pub mod token;

pub use ticket::create_websocket_session_ticket;
pub use token::{backend_session_token, ipc_state_root, workspace_instance_id};

/// Cryptographically-random lowercase hex string of ``bytes`` entropy.
pub(crate) fn random_hex(bytes: usize) -> String {
    let mut buf = vec![0u8; bytes];
    let _ = getrandom::getrandom(&mut buf);
    hex::encode(buf)
}

/// Length-checked constant-time equality — used for bearer-token compares.
pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter()
        .zip(b.iter())
        .fold(0u8, |acc, (x, y)| acc | (x ^ y))
        == 0
}

/// Map an arbitrary session/entity id onto ``[a-zA-Z0-9_-]`` so it is safe
/// to embed in state-file names.
pub fn sanitize_id(id: &str) -> String {
    id.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

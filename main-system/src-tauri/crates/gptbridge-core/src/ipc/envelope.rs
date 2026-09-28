//! Versioned IPC request envelope shared by Rust/Tauri and legacy backends.
//!
//! The envelope is additive: existing JS callers still send `(channel, args)`;
//! Rust normalizes that shape before dispatch and validates the same bounded
//! contract that future Go/Rust control-plane callers will use.

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const IPC_ENVELOPE_VERSION: u16 = 1;
const MAX_REQUEST_ID: usize = 128;
const MAX_COMMAND: usize = 128;
const MAX_SCOPE_ITEMS: usize = 32;
const MAX_SCOPE_ITEM: usize = 128;
const MAX_DEADLINE_MS: u64 = 600_000;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RequestEnvelope {
    pub contract_version: u16,
    pub request_id: String,
    pub command: String,
    #[serde(default)]
    pub args: Value,
    pub requester: String,
    pub capability: String,
    #[serde(default)]
    pub scope: Vec<String>,
    pub deadline_ms: u64,
    pub cancellation_token: String,
    #[serde(default)]
    pub generation: String,
}

impl RequestEnvelope {
    pub fn from_legacy(command: String, args: Value) -> Self {
        let request_id = format!("ipc-{}", uuid_like_suffix());
        Self {
            contract_version: IPC_ENVELOPE_VERSION,
            cancellation_token: request_id.clone(),
            request_id,
            command,
            args,
            requester: "renderer".to_string(),
            capability: "ipc:invoke".to_string(),
            scope: vec!["ui".to_string()],
            deadline_ms: 30_000,
            generation: String::new(),
        }
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.contract_version != IPC_ENVELOPE_VERSION {
            return Err("IPC_ENVELOPE_VERSION_UNSUPPORTED".to_string());
        }
        bounded_token(&self.request_id, MAX_REQUEST_ID, "request_id")?;
        bounded_command(&self.command)?;
        bounded_token(&self.requester, MAX_REQUEST_ID, "requester")?;
        bounded_token(&self.capability, MAX_REQUEST_ID, "capability")?;
        if self.scope.is_empty() || self.scope.len() > MAX_SCOPE_ITEMS {
            return Err("IPC_ENVELOPE_SCOPE_INVALID".to_string());
        }
        for item in &self.scope {
            bounded_token(item, MAX_SCOPE_ITEM, "scope")?;
        }
        if self.deadline_ms == 0 || self.deadline_ms > MAX_DEADLINE_MS {
            return Err("IPC_ENVELOPE_DEADLINE_INVALID".to_string());
        }
        bounded_token(
            &self.cancellation_token,
            MAX_REQUEST_ID,
            "cancellation_token",
        )?;
        Ok(())
    }
}

fn bounded_token(value: &str, max: usize, field: &str) -> Result<(), String> {
    if value.is_empty() || value.len() > max || !value.is_ascii() {
        return Err(format!(
            "IPC_ENVELOPE_{}_INVALID",
            field.to_ascii_uppercase()
        ));
    }
    Ok(())
}

fn bounded_command(value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > MAX_COMMAND
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || b":_-".contains(&byte))
    {
        return Err("IPC_ENVELOPE_COMMAND_INVALID".to_string());
    }
    Ok(())
}

fn uuid_like_suffix() -> String {
    // A unique request label is useful for tracing, but authority does not
    // depend on it; validation and the outer Tauri capability gate remain the
    // security boundary.
    format!("{}-{}", std::process::id(), monotonic_nanos())
}

fn monotonic_nanos() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::{RequestEnvelope, IPC_ENVELOPE_VERSION};
    use serde_json::json;

    #[test]
    fn legacy_shape_normalizes_to_valid_envelope() {
        let envelope = RequestEnvelope::from_legacy("app:get-status".to_string(), json!([]));
        assert_eq!(envelope.contract_version, IPC_ENVELOPE_VERSION);
        assert!(envelope.validate().is_ok());
    }

    #[test]
    fn invalid_command_fails_closed() {
        let mut envelope = RequestEnvelope::from_legacy("app:get-status".to_string(), json!([]));
        envelope.command = "App:GetStatus".to_string();
        assert_eq!(envelope.validate().unwrap_err(), "IPC_ENVELOPE_COMMAND_INVALID");
    }

    #[test]
    fn deadline_and_scope_are_bounded() {
        let mut envelope = RequestEnvelope::from_legacy("app:get-status".to_string(), json!([]));
        envelope.deadline_ms = 0;
        assert_eq!(envelope.validate().unwrap_err(), "IPC_ENVELOPE_DEADLINE_INVALID");
        envelope.deadline_ms = 30_000;
        envelope.scope.clear();
        assert_eq!(envelope.validate().unwrap_err(), "IPC_ENVELOPE_SCOPE_INVALID");
    }
}

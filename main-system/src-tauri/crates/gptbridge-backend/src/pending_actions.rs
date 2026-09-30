//! Pending-action queue and automation-switch state readers for the runtime
//! status payload.  Port of the retired ``auto_action_policy`` read path:
//! the durable queue and switches are file contracts under
//! ``runtime/state`` — the backend projects them for the control surfaces
//! and never mutates them here.  Terminal records (``expired``, reconciled)
//! stay in the durable store as evidence but are excluded from every
//! pending-action presentation surface.

use serde_json::{json, Value};
use std::path::PathBuf;
use time::{format_description::well_known::Iso8601, OffsetDateTime, PrimitiveDateTime};

use gptbridge_core::native::paths;

const ACTIONABLE_STATUS: &str = "awaiting-confirmation";

fn state_dir() -> PathBuf {
    paths::path_library()
        .workspace_root
        .join("main-system")
        .join("runtime")
        .join("state")
}

fn read_state(name: &str) -> Option<Value> {
    let path = state_dir().join(name);
    let raw = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(raw.trim()).ok()
}

fn parse_expiry(raw: &str) -> Option<OffsetDateTime> {
    let text = raw.trim();
    if let Ok(moment) = OffsetDateTime::parse(text, &Iso8601::PARSING) {
        return Some(moment);
    }
    PrimitiveDateTime::parse(text, &Iso8601::PARSING)
        .ok()
        .map(|moment| moment.assume_utc())
}

fn expired(action: &Value) -> bool {
    let Some(raw) = action.get("expires_at").and_then(|v| v.as_str()) else {
        return false;
    };
    if raw.trim().is_empty() {
        return false;
    }
    match parse_expiry(raw) {
        Some(expiry) => OffsetDateTime::now_utc() > expiry,
        None => true,
    }
}

fn reconciled(action: &Value) -> bool {
    match action.get("reconciliation") {
        None | Some(Value::Null) => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Object(map)) => !map.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::String(text)) => !text.is_empty(),
        Some(_) => true,
    }
}

fn actionable(action: &Value) -> bool {
    action.is_object()
        && action.get("status").and_then(|v| v.as_str()) == Some(ACTIONABLE_STATUS)
        && !reconciled(action)
        && !expired(action)
}

/// Live, user-confirmable queue items — ``status == awaiting-confirmation``,
/// not reconciled, and inside the confirmation window.
pub fn actionable_pending_actions() -> Vec<Value> {
    match read_state("pending-actions.json") {
        Some(Value::Array(actions)) => actions.into_iter().filter(|a| actionable(a)).collect(),
        _ => Vec::new(),
    }
}

/// ``pending_action_cardinality`` projection: mode from repair count,
/// unresolved counts every actionable item.
pub fn pending_action_cardinality(actions: &[Value]) -> Value {
    let kind_is = |action: &Value, kind: &str| {
        action.get("kind").and_then(|v| v.as_str()) == Some(kind)
    };
    let repairs = actions.iter().filter(|a| kind_is(a, "repair")).count();
    let updates = actions.iter().filter(|a| kind_is(a, "update")).count();
    let mode = if repairs >= 2 {
        "MULTI_FAULT"
    } else if repairs == 1 {
        "SINGLE_FAULT"
    } else {
        "NO_FAULT"
    };
    json!({
        "mode": mode,
        "unresolved": actions.len(),
        "fault_count": repairs,
        "update_count": updates,
    })
}

/// Persisted automation switches with the pinned repair attribution —
/// automatic repair executes through the governed system-audit chain, so the
/// surface reports the standing policy rather than a stale persisted toggle.
/// A missing or unreadable store never enables execution.
pub fn automation_switches() -> Value {
    let data = read_state("automation-switches.json");
    let get = |key: &str| data.as_ref().and_then(|d| d.get(key));
    json!({
        "automatic_repair_enabled": true,
        "automatic_update_enabled": get("automatic_update_enabled")
            .and_then(|v| v.as_bool())
            .unwrap_or(false),
        "automatic_repair_managed_by": "system-audit-flow",
        "updated_at": get("updated_at").and_then(|v| v.as_str()).unwrap_or(""),
        "updated_by": get("updated_by").and_then(|v| v.as_str()).unwrap_or(""),
    })
}

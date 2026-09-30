//! resource_mode.rs — governed resource-mode get/set for the drawer.
//!
//! Port of ``tasks/resource_governor_signal.py``'s file contract:
//!   * rules   — ``main-system/config/resource-governor-rules.json``
//!               (``mode``, ``auto_mode``, ``modes``, ``power_saving_schedule``)
//!   * state   — ``runtime/state/resource-governor.json`` (live governor)
//!   * advisor — ``runtime/state/resource-mode-advisor.json``
//!   * audit   — ``runtime/state/resource-mode-audit.jsonl``
//! Writes are atomic (tmp + replace); the running governor hot-reloads the
//! rules file on its next cycle.  An explicit mode selection disables
//! ``auto_mode`` — user intent always wins over the demand advisor.

use std::path::PathBuf;

use serde_json::{json, Map, Value};
use time::OffsetDateTime;

use gptbridge_core::native::paths;

use crate::audit;

const GOVERNOR_MODES: [&str; 4] = ["sleep", "low", "medium", "high"];
const AUDIT_LEDGER: &str = "resource-mode-audit";

fn workspace_root() -> PathBuf {
    paths::path_library().workspace_root.clone()
}

fn rules_path() -> PathBuf {
    workspace_root()
        .join("main-system")
        .join("config")
        .join("resource-governor-rules.json")
}

fn state_dir() -> PathBuf {
    workspace_root()
        .join("main-system")
        .join("runtime")
        .join("state")
}

fn read_json(path: &PathBuf) -> Map<String, Value> {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

fn resolve_schedule(rules: &Map<String, Value>) -> Map<String, Value> {
    let defaults = Map::from_iter([
        ("enabled".to_string(), json!(false)),
        ("start".to_string(), json!("22:00")),
        ("end".to_string(), json!("07:00")),
        ("mode".to_string(), json!("sleep")),
    ]);
    match rules.get("power_saving_schedule") {
        Some(Value::Object(custom)) => {
            let mut merged = defaults;
            merged.extend(custom.clone());
            merged
        }
        _ => defaults,
    }
}

fn parse_hhmm(value: Option<&Value>, fallback: i32) -> i32 {
    let Some(text) = value.and_then(|v| v.as_str()) else {
        return fallback;
    };
    let mut parts = text.split(':');
    let (Some(h), Some(m)) = (parts.next(), parts.next()) else {
        return fallback;
    };
    match (h.parse::<i32>(), m.parse::<i32>()) {
        (Ok(h), Ok(m)) if (0..24).contains(&h) && (0..60).contains(&m) => h * 60 + m,
        _ => fallback,
    }
}

fn within_window(now_min: i32, start_min: i32, end_min: i32) -> bool {
    if start_min == end_min {
        return true;
    }
    if start_min < end_min {
        now_min >= start_min && now_min < end_min
    } else {
        now_min >= start_min || now_min < end_min
    }
}

fn local_minutes() -> i32 {
    let now = OffsetDateTime::now_local().unwrap_or_else(|_| OffsetDateTime::now_utc());
    now.hour() as i32 * 60 + now.minute() as i32
}

/// ``governor_mode()`` — configured mode + live applied mode + schedule state.
fn governor_mode() -> Value {
    let rules = read_json(&rules_path());
    let state = read_json(&state_dir().join("resource-governor.json"));
    let advisor = read_json(&state_dir().join("resource-mode-advisor.json"));

    let modes: Vec<String> = match rules.get("modes") {
        Some(Value::Object(map)) => map.keys().cloned().collect(),
        _ => GOVERNOR_MODES.iter().map(|m| m.to_string()).collect(),
    };
    let schedule = resolve_schedule(&rules);
    let start_min = parse_hhmm(schedule.get("start"), 22 * 60);
    let end_min = parse_hhmm(schedule.get("end"), 7 * 60);
    let enabled = schedule.get("enabled").and_then(|v| v.as_bool()) == Some(true);
    let active = enabled && within_window(local_minutes(), start_min, end_min);

    let advisor_payload = if advisor.is_empty() {
        Value::Null
    } else {
        json!({
            "target": advisor.get("target"),
            "reason": advisor.get("reason"),
            "at": advisor.get("at"),
        })
    };
    let rules_error = state
        .get("features")
        .and_then(|f| f.get("rules_error"))
        .cloned()
        .unwrap_or(Value::Null);

    let mut schedule_out = schedule.clone();
    schedule_out.insert("active".to_string(), json!(active));

    json!({
        "mode": rules.get("mode").and_then(|v| v.as_str()).unwrap_or("medium"),
        "applied": state.get("mode"),
        "modes": modes,
        "auto_mode": rules.get("auto_mode").and_then(|v| v.as_bool()) == Some(true),
        "running": !state.is_empty(),
        "advisor": advisor_payload,
        "rules_error": rules_error,
        "power_saving_schedule": schedule_out,
    })
}

/// ``_commit_rules`` — merge ``update`` into the rules file atomically and
/// append the mode-audit entry.  Returns ``Err`` when the rules file is
/// missing or the write fails (callers map to MODE_UNKNOWN/RULES_WRITE_FAILED).
fn commit_rules(update: Map<String, Value>, actor: &str) -> Result<Value, Value> {
    let path = rules_path();
    if !path.is_file() {
        return Err(json!({
            "ok": false,
            "error_code": "MODE_UNKNOWN",
            "message": format!("rules file missing: {}", path.display()),
            "resource_mode": governor_mode(),
        }));
    }
    let mut rules = read_json(&path);
    let previous = rules.get("mode").cloned().unwrap_or(Value::Null);
    rules.extend(update);
    let tmp = path.with_extension("json.tmp");
    let write_ok = std::fs::write(&tmp, format!("{}\n", serde_json::to_string_pretty(&rules).unwrap_or_default()))
        .and_then(|_| std::fs::rename(&tmp, &path));
    if write_ok.is_err() {
        return Err(json!({
            "ok": false,
            "error_code": "RULES_WRITE_FAILED",
            "message": "rules file write failed",
            "resource_mode": governor_mode(),
        }));
    }
    audit::append_audit_record(
        AUDIT_LEDGER,
        json!({
            "actor": actor,
            "previous": previous,
            "mode": rules.get("mode"),
            "auto_mode": rules.get("auto_mode").and_then(|v| v.as_bool()).unwrap_or(false),
        }),
    );
    Ok(governor_mode())
}

pub fn get(payload: &Value) -> Value {
    let _ = payload;
    json!({"ok": true, "resource_mode": governor_mode()})
}

/// Status-push accessor — the retired ``startup_status`` embedded the
/// governor snapshot as ``resource_mode`` on every push; the drawer's
/// mode row reads that field without a round-trip.
pub fn snapshot() -> Value {
    governor_mode()
}

/// ``app:set-resource-mode`` — ``mode`` = sleep/low/medium/high (or a custom
/// ``modes`` key) selects the preset; ``auto`` re-enables the advisor.
pub fn set(payload: &Value) -> Value {
    let mode = payload["mode"].as_str().unwrap_or("").trim().to_lowercase();
    if mode.is_empty() {
        return json!({
            "ok": false,
            "error_code": "MISSING_MODE",
            "message": "mode (low / medium / high / auto) is required",
        });
    }
    if mode == "auto" {
        return match commit_rules(
            Map::from_iter([("auto_mode".to_string(), json!(true))]),
            "authenticated-ui",
        ) {
            Ok(mode) => json!({"ok": true, "resource_mode": mode}),
            Err(err) => err,
        };
    }
    let rules = read_json(&rules_path());
    let known: Vec<String> = match rules.get("modes") {
        Some(Value::Object(map)) if !map.is_empty() => map.keys().cloned().collect(),
        _ => GOVERNOR_MODES.iter().map(|m| m.to_string()).collect(),
    };
    if !known.iter().any(|m| m == &mode) {
        return json!({
            "ok": false,
            "error_code": "MODE_UNKNOWN",
            "message": format!("unknown governor mode: '{mode}'"),
            "resource_mode": governor_mode(),
        });
    }
    match commit_rules(
        Map::from_iter([
            ("mode".to_string(), json!(mode)),
            ("auto_mode".to_string(), json!(false)),
        ]),
        "authenticated-ui",
    ) {
        Ok(mode) => json!({"ok": true, "resource_mode": mode}),
        Err(err) => err,
    }
}

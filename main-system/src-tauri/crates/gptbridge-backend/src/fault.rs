//! fault.rs — ``app:get-fault-analysis`` governed denial surface.
//!
//! Port of ``command_router/fault_analysis_handler.py``.  Per A137–A140/A435/
//! A6500 every fault query must route through the 星澄 sovereign's
//! ``review.global`` intent; the sovereign lived in the retired Python
//! process and has no native successor yet.  The faithful native port is
//! therefore the fail-closed denial — ``REVIEW_SOVEREIGN_UNAVAILABLE`` —
//! with the same allowlist, error codes, sanitization and audit ledger the
//! Python handler enforced.  Nothing here may bypass the sovereign gate.

use serde_json::{json, Value};

use crate::audit;

const AUDIT_LEDGER: &str = "fault-query-audit";

/// ``_QUERY_ALLOWLIST`` — identical to the retired handler.
const QUERY_ALLOWLIST: [&str; 6] = [
    "overview",
    "patterns",
    "knowledge",
    "component",
    "detail",
    "codex-health",
];

fn deny(code: &str, message: &str, query: &str, requester: &str) -> Value {
    audit::append_audit_record(
        AUDIT_LEDGER,
        json!({
            "event": "fault-query-denial",
            "query": query,
            "requester": requester,
            "error_code": code,
            "message": message,
        }),
    );
    json!({"ok": false, "error_code": code, "message": message})
}

/// ``app:get-fault-analysis`` — allowlist + attempt ledger first, then the
/// sovereign unavailability denial (``app.xingcheng_sovereign`` was part of
/// the retired Python process; there is no native holder of
/// ``review.global`` yet — fail closed, never approximate).
pub fn handle(payload: &Value) -> Value {
    let query = payload["query"].as_str().unwrap_or("").trim().to_lowercase();
    let requester = payload["requester"]
        .as_str()
        .unwrap_or("ui")
        .trim()
        .to_lowercase();
    audit::append_audit_record(
        AUDIT_LEDGER,
        json!({
            "event": "fault-query-attempt",
            "query": query,
            "requester": requester,
        }),
    );
    if query.is_empty() {
        return deny("MISSING_QUERY", "query is required", &query, &requester);
    }
    if !QUERY_ALLOWLIST.contains(&query.as_str()) {
        return deny(
            "UNKNOWN_QUERY",
            &format!("query '{query}' is not allowed"),
            &query,
            &requester,
        );
    }
    deny(
        "REVIEW_SOVEREIGN_UNAVAILABLE",
        "星澄 sovereign not started",
        &query,
        &requester,
    )
}

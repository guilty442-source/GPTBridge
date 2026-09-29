//! saga.rs — read-only workflow-operation diagnostics for the renderer.
//!
//! Port of ``command_router/saga_handler.py`` + the persisted-state half of
//! ``workflow/visualization.py`` (``visualize_stored_operation`` +
//! ``to_panel_payload``).  PostgreSQL ``gptbridge_workflow`` stays the
//! operation authority; this module only projects its persisted state —
//! it never mutates, claims, or reconciles (A137).
//!
//! Denial contract (unchanged): ``SAGA_RUNTIME_UNAVAILABLE`` when no
//! governed DSN is reachable, ``MISSING_OPERATION_ID``,
//! ``OPERATION_NOT_FOUND``, ``SAGA_STORE_ERROR`` — every attempt/result/
//! denial lands in ``runtime/state/saga-query-audit.jsonl``.

use std::time::SystemTime;

use serde_json::{json, Map, Value};

use crate::audit;
use crate::pg;

const DEFAULT_LIST_LIMIT: i64 = 50;
const MAX_LIST_LIMIT: i64 = 200;
const AUDIT_LEDGER: &str = "saga-query-audit";

fn epoch(value: Option<SystemTime>) -> f64 {
    value
        .and_then(|t| t.duration_since(SystemTime::UNIX_EPOCH).ok())
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Strip filesystem paths / implementation detail before text reaches the UI.
fn sanitize_error(error: &postgres::Error) -> String {
    let text = format!("{}", error);
    let mut out = String::with_capacity(text.len().min(200));
    for token in text.split_whitespace() {
        let looks_like_path = token.contains(":\\")
            || token.contains(":/")
            || token.starts_with('/');
        out.push_str(if looks_like_path { "<path>" } else { token });
        out.push(' ');
    }
    let trimmed = out.trim();
    if trimmed.is_empty() { "internal error".to_string() } else { trimmed[..trimmed.len().min(200)].to_string() }
}

fn audit(event: &str, extra: Map<String, Value>) {
    let mut record = Map::from_iter([("event".to_string(), json!(event))]);
    record.extend(extra);
    audit::append_audit_record(AUDIT_LEDGER, Value::Object(record));
}

fn deny(command: &str, code: &str, message: &str, requester: &str) -> Value {
    audit(
        "saga-query-denial",
        Map::from_iter([
            ("command".to_string(), json!(command)),
            ("requester".to_string(), json!(requester)),
            ("error_code".to_string(), json!(code)),
            ("message".to_string(), json!(message)),
        ]),
    );
    json!({"ok": false, "error_code": code, "message": message})
}

fn operation_summary(row: &postgres::Row) -> Value {
    let completed: Option<SystemTime> = row.get(10);
    json!({
        "operation_id": row.get::<_, String>(0),
        "operation_type": row.get::<_, String>(1),
        "module_id": row.get::<_, String>(2),
        "resource_id": row.get::<_, Option<String>>(3).unwrap_or_default(),
        "generation": row.get::<_, i64>(4),
        "status": row.get::<_, String>(5),
        "current_step": row.get::<_, Option<String>>(6).unwrap_or_default(),
        "correlation_id": row.get::<_, Option<String>>(7).unwrap_or_default(),
        "created_at": epoch(row.get::<_, Option<SystemTime>>(8)),
        "updated_at": epoch(row.get::<_, Option<SystemTime>>(9)),
        "completed_at": completed.map(|_| epoch(completed)),
    })
}

fn list_operations(payload: &Value, requester: &str) -> Value {
    let limit = payload["limit"]
        .as_i64()
        .unwrap_or(DEFAULT_LIST_LIMIT)
        .clamp(1, MAX_LIST_LIMIT);
    let Some(mut client) = pg::connect() else {
        return deny(
            "app:get-saga-operations",
            "SAGA_RUNTIME_UNAVAILABLE",
            "saga runtime not started",
            requester,
        );
    };
    let rows = client.query(
        "SELECT operation_id, operation_type, module_id, resource_id, \
         generation, status, current_step, correlation_id, \
         created_at, updated_at, completed_at \
         FROM gptbridge_workflow.operation \
         ORDER BY created_at DESC, operation_id LIMIT $1",
        &[&limit],
    );
    match rows {
        Ok(rows) => {
            let operations: Vec<Value> = rows.iter().map(operation_summary).collect();
            audit(
                "saga-query-result",
                Map::from_iter([
                    ("command".to_string(), json!("app:get-saga-operations")),
                    ("count".to_string(), json!(operations.len())),
                ]),
            );
            json!({"ok": true, "operations": operations})
        }
        Err(error) => deny(
            "app:get-saga-operations",
            "SAGA_STORE_ERROR",
            &sanitize_error(&error),
            requester,
        ),
    }
}

fn operation_detail(payload: &Value, requester: &str) -> Value {
    let operation_id = payload["operation_id"].as_str().unwrap_or("").trim();
    if operation_id.is_empty() {
        return deny(
            "app:get-saga-operation",
            "MISSING_OPERATION_ID",
            "operation_id is required",
            requester,
        );
    }
    let Some(mut client) = pg::connect() else {
        return deny(
            "app:get-saga-operation",
            "SAGA_RUNTIME_UNAVAILABLE",
            "saga runtime not started",
            requester,
        );
    };
    let op_row = match client.query(
        "SELECT operation_id, operation_type, module_id, resource_id, \
         generation, status, current_step, idempotency_key, \
         correlation_id, fingerprint, checkpoint, \
         created_at, updated_at, completed_at \
         FROM gptbridge_workflow.operation WHERE operation_id = $1",
        &[&operation_id],
    ) {
        Ok(rows) => match rows.into_iter().next() {
            Some(row) => row,
            None => {
                return deny(
                    "app:get-saga-operation",
                    "OPERATION_NOT_FOUND",
                    &format!("operation '{operation_id}' not found"),
                    requester,
                )
            }
        },
        Err(error) => {
            return deny(
                "app:get-saga-operation",
                "SAGA_STORE_ERROR",
                &sanitize_error(&error),
                requester,
            )
        }
    };
    let step_rows = match client.query(
        "SELECT step_id, step_order, engine, action_type, status, \
         attempt_count, result_hash, error_code \
         FROM gptbridge_workflow.operation_step \
         WHERE operation_id = $1 ORDER BY step_order, step_id",
        &[&operation_id],
    ) {
        Ok(rows) => rows,
        Err(error) => {
            return deny(
                "app:get-saga-operation",
                "SAGA_STORE_ERROR",
                &sanitize_error(&error),
                requester,
            )
        }
    };
    let operation = panel_payload(&op_row, &step_rows);
    audit(
        "saga-query-result",
        Map::from_iter([
            ("command".to_string(), json!("app:get-saga-operation")),
            ("operation_id".to_string(), json!(operation_id)),
        ]),
    );
    json!({"ok": true, "operation": operation})
}

/// ``to_panel_payload(visualize_stored_operation(op, step_rows))`` — nodes,
/// edges, timeline and metadata, projected straight from the stored rows
/// (design-time StepSpec fields are not persisted, same caveat as Python).
fn panel_payload(op: &postgres::Row, steps: &[postgres::Row]) -> Value {
    let operation_id: String = op.get(0);
    let operation_type: String = op.get(1);
    let module_id: String = op.get(2);
    let resource_id: String = op.get::<_, Option<String>>(3).unwrap_or_default();
    let generation: i64 = op.get(4);
    let status: String = op.get(5);
    let idempotency_key: String = op.get::<_, Option<String>>(7).unwrap_or_default();
    let correlation_id: String = op.get::<_, Option<String>>(8).unwrap_or_default();
    let created_at = epoch(op.get::<_, Option<SystemTime>>(11));
    let updated_at = epoch(op.get::<_, Option<SystemTime>>(12));

    let mut nodes = vec![json!({
        "id": format!("op:{operation_id}"),
        "label": format!("{operation_type}\n({module_id})"),
        "type": "operation",
        "status": status,
        "engine": "",
        "details": {
            "operation_id": operation_id,
            "module_id": module_id,
            "resource_id": resource_id,
            "generation": generation,
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id,
        },
    })];
    let mut edges: Vec<Value> = Vec::new();

    for (i, row) in steps.iter().enumerate() {
        let step_id: String = row.get(0);
        let step_order: i32 = row.get(1);
        let engine: String = row.get::<_, Option<String>>(2).unwrap_or_default();
        let action_type: String = row.get::<_, Option<String>>(3).unwrap_or_default();
        let step_status: String = row.get::<_, Option<String>>(4)
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "PENDING".to_string());
        let attempt_count: i32 = row.get::<_, Option<i32>>(5).unwrap_or(0);
        let error_code: String = row.get::<_, Option<String>>(7).unwrap_or_default();
        nodes.push(json!({
            "id": format!("step:{step_id}"),
            "label": format!("{step_id}\n({action_type})"),
            "type": "step",
            "status": step_status,
            "engine": engine,
            "details": {
                "step_id": step_id,
                "step_order": step_order,
                "action_type": action_type,
                "engine": engine,
                "attempt_count": attempt_count,
                "error_code": error_code,
            },
        }));
        edges.push(json!({
            "from": format!("op:{operation_id}"),
            "to": format!("step:{step_id}"),
            "type": "depends_on",
            "label": format!("order {step_order}"),
        }));
        if i > 0 {
            let prev: String = steps[i - 1].get(0);
            edges.push(json!({
                "from": format!("step:{prev}"),
                "to": format!("step:{step_id}"),
                "type": "depends_on",
            }));
        }
    }

    // Timeline: creation + terminal step events (the Operation.history
    // transition log is runtime-only and not persisted).
    let mut timeline: Vec<Value> = Vec::new();
    if created_at > 0.0 {
        timeline.push(json!({
            "timestamp": created_at,
            "event_type": "operation_created",
            "operation_id": operation_id,
            "step_id": "",
            "detail": {"operation_type": operation_type},
            "status": "created",
        }));
    }
    for row in steps {
        let step_status: String = row.get::<_, Option<String>>(4).unwrap_or_default();
        if step_status == "COMPLETED" || step_status == "FAILED" {
            let step_id: String = row.get(0);
            let event_type = if step_status == "COMPLETED" {
                "step_completed"
            } else {
                "step_failed"
            };
            timeline.push(json!({
                "timestamp": updated_at,
                "event_type": event_type,
                "operation_id": operation_id,
                "step_id": step_id,
                "detail": {
                    "attempt": 1,
                    "detail": "",
                    "error_code": row.get::<_, Option<String>>(7).unwrap_or_default(),
                },
                "status": step_status.to_lowercase(),
            }));
        }
    }
    timeline.sort_by(|a, b| {
        a["timestamp"].as_f64().partial_cmp(&b["timestamp"].as_f64())
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    json!({
        "operation_id": operation_id,
        "operation_type": operation_type,
        "module_id": module_id,
        "status": status,
        "nodes": nodes,
        "edges": edges,
        "timeline": timeline,
        "metadata": {
            "generation": generation,
            "module_id": module_id,
            "resource_id": resource_id,
        },
        "generated_at": audit::iso_now(),
    })
}

/// Dispatch ``app:get-saga-operations`` / ``app:get-saga-operation``.
/// Every attempt is ledgered before the store is touched.
pub fn handle(command: &str, payload: &Value) -> Value {
    let requester = payload["requester"]
        .as_str()
        .unwrap_or("ui")
        .trim()
        .to_lowercase();
    audit(
        "saga-query-attempt",
        Map::from_iter([
            ("command".to_string(), json!(command)),
            ("requester".to_string(), json!(requester)),
        ]),
    );
    if command == "app:get-saga-operations" {
        list_operations(payload, &requester)
    } else {
        operation_detail(payload, &requester)
    }
}

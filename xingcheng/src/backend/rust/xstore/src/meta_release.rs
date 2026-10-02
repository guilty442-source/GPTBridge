//! meta_release.rs — adapter candidate / evaluation / release /
//! runtime-state domain ops (§12-§16). These replicate the exact
//! ReleaseAdapter state machine of the PostgreSQL repository:
//!
//!   candidate --eval pass--> validated --stage--> staged --activate--> active
//!   active --(another activates)--> staged      rollback: previous->active
//!   candidate|validated --reject--> rejected
//!   candidate|staged|active --retire--> retired
//!
//! Single-active-candidate and the runtime singleton update are part of
//! the same atomic event bundle — a compare-and-commit under the writer
//! lease, never read-check-write across calls (§13,§75).

use crate::meta_domain as dom;
use crate::meta_domain::{audit_spec, s, spec_for};
use crate::meta_state::State;
use crate::meta_types as mt;
use serde_json::{json, Value};

fn runtime_rec(st: &State) -> Value {
    st.get(mt::RT_RUNTIME_STATE, "1")
        .map(|r| r.payload.clone())
        .unwrap_or_else(|| json!({
            "singleton_id": 1,
            "base_model_id": dom::BASE_MODEL_ID,
            "runtime_model_id": dom::BASE_MODEL_ID,
            "active_adapter_id": Value::Null,
            "previous_adapter_id": Value::Null,
            "automatic_weight_replacement": false,
            "updated_at": "",
        }))
}

fn set_status(st: &State, rec: &crate::meta_state::Record, status: &str, now: &str, out: &mut Vec<mt::EventSpec>, etype: &str) {
    let mut np = rec.payload.clone();
    np["status"] = json!(status);
    np["updated_at"] = json!(now);
    out.push(spec_for(st, mt::RT_CANDIDATE, &rec.record_id, etype, np));
}

fn set_runtime(st: &State, active: Value, previous: Value, now: &str, out: &mut Vec<mt::EventSpec>) {
    let mut rt = runtime_rec(st);
    rt["active_adapter_id"] = active;
    rt["previous_adapter_id"] = previous;
    rt["automatic_weight_replacement"] = json!(false); // §16 invariant
    rt["updated_at"] = json!(now);
    out.push(spec_for(st, mt::RT_RUNTIME_STATE, "1", "runtime-state-updated", rt));
}

/// Register a candidate: job must be completed; job_id and
/// artifact_sha256 unique; Rust owns the artifact hash (§29).
pub fn register_candidate(st: &State, p: &Value) -> Result<(Vec<mt::EventSpec>, Value), String> {
    let job_id = dom::need(p, "job_id")?;
    let artifact_path = dom::need(p, "artifact_path")?;
    let job = st
        .get(mt::RT_TRAINING_JOB, &job_id)
        .ok_or("META_JOB_MISSING")?;
    if s(&job.payload, "status") != "completed" {
        return Err("META_JOB_NOT_COMPLETED: adapter requires a completed training job".into());
    }
    if st.list(mt::RT_CANDIDATE).any(|r| s(&r.payload, "job_id") == job_id) {
        return Err(format!("META_UNIQUE: job_id {job_id} already registered"));
    }
    let data = std::fs::read(&artifact_path)
        .map_err(|e| format!("META_ARTIFACT_MISSING: {e}"))?;
    let artifact_sha = crate::hash::sha256_hex(&data);
    let supplied = s(p, "artifact_sha256");
    if !supplied.is_empty() && supplied != artifact_sha {
        return Err("META_ARTIFACT_HASH_MISMATCH".into());
    }
    if st.list(mt::RT_CANDIDATE).any(|r| s(&r.payload, "artifact_sha256") == artifact_sha) {
        return Err("META_UNIQUE: artifact_sha256 already registered".into());
    }
    let metrics_json = if s(p, "metrics_json").is_empty() {
        "{}".to_string()
    } else {
        let m = s(p, "metrics_json");
        serde_json::from_str::<Value>(&m)
            .map_err(|e| format!("META_JSON_INVALID: metrics_json: {e}"))?;
        m
    };
    let adapter_id = {
        let a = s(p, "adapter_id");
        if a.is_empty() { mt::new_id("star-transformer-adapter") } else { a }
    };
    let now = mt::now_iso();
    let adapter_format = {
        let f = s(p, "adapter_format");
        if f.is_empty() { "native-checkpoint".into() } else { f }
    };
    let mut specs = vec![spec_for(st, mt::RT_CANDIDATE, &adapter_id, "adapter-candidate-registered", json!({
        "adapter_id": adapter_id,
        "job_id": job_id,
        "dataset_id": s(&job.payload, "dataset_id"),
        "base_model_id": dom::BASE_MODEL_ID,
        "adapter_format": adapter_format,
        "artifact_path": artifact_path,
        "artifact_sha256": artifact_sha,
        "metrics_json": metrics_json,
        "status": "candidate",
        "created_at": now,
        "updated_at": now,
    }))];
    specs.push(audit_spec("adapter-candidate", &adapter_id, "adapter-candidate-registered", json!({
        "job_id": job_id,
        "dataset_id": s(&job.payload, "dataset_id"),
        "artifact_sha256": artifact_sha,
        "adapter_format": s(p, "adapter_format"),
    })));
    Ok((specs, json!({"result": "registered", "adapter_id": adapter_id})))
}

/// Record an evaluation: candidate must be candidate|validated;
/// UNIQUE(adapter_id, suite_sha256); a pass flips the candidate to
/// validated in the same commit (§14).
pub fn record_evaluation(st: &State, p: &Value) -> Result<(Vec<mt::EventSpec>, Value), String> {
    let adapter_id = dom::need(p, "adapter_id")?;
    let suite_id = dom::need(p, "suite_id")?;
    let cand = st
        .get(mt::RT_CANDIDATE, &adapter_id)
        .ok_or("META_CANDIDATE_MISSING")?;
    let status = s(&cand.payload, "status");
    if status != "candidate" && status != "validated" {
        return Err(format!("META_NOT_EVALUABLE: status {status}"));
    }
    let suite_sha = {
        let sh = s(p, "suite_sha256");
        if sh.is_empty() { mt::sha256_text(&suite_id) } else { sh }
    };
    if st.list(mt::RT_EVALUATION).any(|r| {
        s(&r.payload, "adapter_id") == adapter_id
            && s(&r.payload, "suite_sha256") == suite_sha
    }) {
        return Err("META_UNIQUE: adapter_id+suite_sha256 already evaluated".into());
    }
    for k in ["baseline_metrics_json", "adapter_metrics_json", "comparison_json", "quality_gates_json"] {
        let v = s(p, k);
        serde_json::from_str::<Value>(&v)
            .map_err(|e| format!("META_JSON_INVALID: {k}: {e}"))?;
    }
    let passed = p.get("passed").and_then(|x| x.as_bool()).unwrap_or(false);
    let evaluated_by = {
        let e = s(p, "evaluated_by");
        if e.is_empty() { "star-main-native-model".into() } else { e }
    };
    let evaluation_id = {
        let e = s(p, "evaluation_id");
        if e.is_empty() { mt::new_id("star-transformer-eval") } else { e }
    };
    let now = mt::now_iso();
    let mut specs = vec![spec_for(st, mt::RT_EVALUATION, &evaluation_id, "adapter-evaluated", json!({
        "evaluation_id": evaluation_id,
        "adapter_id": adapter_id,
        "suite_id": suite_id,
        "suite_sha256": suite_sha,
        "baseline_metrics_json": s(p, "baseline_metrics_json"),
        "adapter_metrics_json": s(p, "adapter_metrics_json"),
        "comparison_json": s(p, "comparison_json"),
        "quality_gates_json": s(p, "quality_gates_json"),
        "passed": passed,
        "evaluated_by": evaluated_by,
        "created_at": now,
    }))];
    if passed && status == "candidate" {
        set_status(st, cand, "validated", &now, &mut specs, "adapter-validated");
    }
    specs.push(audit_spec("adapter-candidate", &adapter_id, "adapter-evaluated", json!({
        "evaluation_id": evaluation_id,
        "suite_id": suite_id,
        "passed": passed,
        "evaluated_by": evaluated_by,
        "status": if passed { "validated" } else { status.as_str() },
    })));
    Ok((specs, json!({"result": "recorded", "evaluation_id": evaluation_id})))
}

/// ReleaseAdapter port (§15): stage/activate/rollback/retire — one
/// atomic bundle updating candidate statuses + the runtime singleton.
pub fn release(st: &State, p: &Value) -> Result<(Vec<mt::EventSpec>, Value), String> {
    let adapter_id = dom::need(p, "adapter_id")?;
    let action = s(p, "action").to_lowercase();
    if !dom::RELEASE_ACTIONS.contains(&action.as_str()) {
        return Err(format!("META_RELEASE_ACTION: unsupported {action}"));
    }
    let governed_by = dom::need(p, "governed_by")?;
    let reason = dom::need(p, "reason")?;
    let cand = st
        .get(mt::RT_CANDIDATE, &adapter_id)
        .ok_or("META_CANDIDATE_MISSING")?
        .clone();
    let status = s(&cand.payload, "status");
    let rt = runtime_rec(st);
    let active = s(&rt, "active_adapter_id");
    let previous = s(&rt, "previous_adapter_id");
    let now = mt::now_iso();
    let release_id = mt::new_id("star-transformer-release");
    let mut specs: Vec<mt::EventSpec> = Vec::new();
    let mut previous_adapter_id = Value::Null;
    let new_status: String;
    let mut touch_runtime = false;
    let mut new_active = if active.is_empty() { Value::Null } else { json!(active) };
    let mut new_previous = if previous.is_empty() { Value::Null } else { json!(previous) };

    match action.as_str() {
        "stage" => {
            if status != "validated" {
                return Err("META_STAGE_DENIED: only validated adapters can be staged".into());
            }
            new_status = "staged".into();
        }
        "activate" => {
            if status != "staged" {
                return Err("META_ACTIVATE_DENIED: only staged adapters can be activated".into());
            }
            if !active.is_empty() {
                let old = st.get(mt::RT_CANDIDATE, &active)
                    .ok_or("XSTORE_RECOVERY_REQUIRED: runtime active missing")?;
                set_status(st, old, "staged", &now, &mut specs, "adapter-demoted");
            }
            previous_adapter_id = if active.is_empty() { Value::Null } else { json!(active) };
            new_active = json!(adapter_id);
            new_previous = previous_adapter_id.clone();
            new_status = "active".into();
            touch_runtime = true;
        }
        "rollback" => {
            if previous.is_empty() {
                return Err("META_ROLLBACK_DENIED: no previous adapter".into());
            }
            if !active.is_empty() && active != adapter_id {
                let old = st.get(mt::RT_CANDIDATE, &active)
                    .ok_or("XSTORE_RECOVERY_REQUIRED: runtime active missing")?;
                set_status(st, old, "staged", &now, &mut specs, "adapter-demoted");
            }
            let prev_rec = st.get(mt::RT_CANDIDATE, &previous)
                .ok_or("XSTORE_RECOVERY_REQUIRED: previous adapter missing")?;
            set_status(st, prev_rec, "active", &now, &mut specs, "adapter-restored");
            previous_adapter_id = json!(previous);
            new_active = json!(previous);
            new_previous = Value::Null;
            new_status = "staged".into();
            touch_runtime = true;
        }
        _ => {
            // retire
            if !["staged", "active", "candidate"].contains(&status.as_str()) {
                return Err(format!("META_RETIRE_DENIED: status {status}"));
            }
            if status == "active" {
                if !previous.is_empty() {
                    let prev_rec = st.get(mt::RT_CANDIDATE, &previous)
                        .ok_or("XSTORE_RECOVERY_REQUIRED: previous adapter missing")?;
                    set_status(st, prev_rec, "active", &now, &mut specs, "adapter-restored");
                }
                previous_adapter_id = if previous.is_empty() { Value::Null } else { json!(previous) };
                new_active = previous_adapter_id.clone();
                new_previous = Value::Null;
                touch_runtime = true;
            }
            new_status = "retired".into();
        }
    }
    // NOTE: demotion events above were built against the pre-tx state;
    // spec_for recomputes chain position at apply time is not needed —
    // revisions are validated per-record, and each record appears at
    // most once per bundle here (a demoted record differs from the
    // released one by construction: activate/rollback demote `active`
    // which is never `adapter_id` since activate requires staged and
    // rollback/retire act on a different id).
    set_status(st, &cand, &new_status, &now, &mut specs, "adapter-status-changed");
    if touch_runtime {
        set_runtime(st, new_active, new_previous, &now, &mut specs);
    }
    specs.push(spec_for(st, mt::RT_RELEASE, &release_id, "adapter-released", json!({
        "release_id": release_id,
        "adapter_id": adapter_id,
        "action": action,
        "previous_adapter_id": previous_adapter_id,
        "governed_by": governed_by,
        "reason": reason,
        "created_at": now,
    })));
    specs.push(audit_spec("adapter-candidate", &adapter_id, "adapter-released", json!({
        "release_id": release_id,
        "action": action,
        "previous_adapter_id": previous_adapter_id,
        "governed_by": governed_by,
        "from_status": status,
        "to_status": new_status,
    })));
    Ok((specs, json!({
        "result": "released", "release_id": release_id,
        "status": new_status,
    })))
}

/// RejectAdapter port: candidate|validated -> rejected + audit.
pub fn reject_candidate(st: &State, p: &Value) -> Result<(Vec<mt::EventSpec>, Value), String> {
    let adapter_id = dom::need(p, "adapter_id")?;
    let governed_by = dom::need(p, "governed_by")?;
    let reason = dom::need(p, "reason")?;
    let cand = st
        .get(mt::RT_CANDIDATE, &adapter_id)
        .ok_or("META_CANDIDATE_MISSING")?;
    let status = s(&cand.payload, "status");
    if status != "candidate" && status != "validated" {
        return Err(format!("META_REJECT_DENIED: status {status}"));
    }
    let now = mt::now_iso();
    let mut specs = Vec::new();
    set_status(st, cand, "rejected", &now, &mut specs, "adapter-rejected");
    specs.push(audit_spec("adapter-candidate", &adapter_id, "adapter-rejected", json!({
        "governed_by": governed_by,
        "reason": reason,
    })));
    Ok((specs, json!({"result": "rejected", "adapter_id": adapter_id})))
}

/// Runtime singleton init — idempotent (mirrors Migrate()'s
/// INSERT ... ON CONFLICT DO NOTHING + base model refresh).
pub fn init_runtime(st: &State, p: &Value) -> Result<(Vec<mt::EventSpec>, Value), String> {
    if let Some(r) = st.get(mt::RT_RUNTIME_STATE, "1") {
        // PG also refreshes base/runtime ids when nothing is active.
        if s(&r.payload, "active_adapter_id").is_empty()
            || r.payload["active_adapter_id"].is_null()
        {
            let mut np = r.payload.clone();
            np["base_model_id"] = json!(dom::BASE_MODEL_ID);
            np["runtime_model_id"] = json!(dom::BASE_MODEL_ID);
            np["automatic_weight_replacement"] = json!(false);
            np["updated_at"] = json!(mt::now_iso());
            return Ok((
                vec![spec_for(st, mt::RT_RUNTIME_STATE, "1", "runtime-state-refreshed", np)],
                json!({"result": "refreshed"}),
            ));
        }
        return Ok((Vec::new(), json!({"result": "existing"})));
    }
    let mut rt = runtime_rec(st);
    rt["updated_at"] = json!(mt::now_iso());
    let _ = p;
    Ok((
        vec![spec_for(st, mt::RT_RUNTIME_STATE, "1", "runtime-state-initialized", rt)],
        json!({"result": "initialized"}),
    ))
}

//! meta_domain.rs — PostgreSQL semantics replication for the xingcheng
//! training metadata domain (§8-§16). Every PG constraint maps to a
//! validator here; every PG transaction maps to an atomic event bundle.
//!
//! Coverage (Repository.cs parity):
//!   create_dataset     transformer_training_dataset{+examples}
//!                      UNIQUE(content_sha256,snapshot_sha256),
//!                      immutable identity, example uniqueness
//!   create_job         transformer_training_job insert (queued)
//!   transition_job     queued->preflight->training->validating->
//!                      completed, sides cancelled/failed (§11)
//!   claim_job          serial-lane atomic claim (advisory-lock port)
//!   audit              standalone append-only audit event
//!   put/transition     generic record ops for the other domains
//!
//! Candidate/eval/release/runtime ops live in meta_release.rs.

use crate::meta_state::State;
use crate::meta_types as mt;
use serde_json::{json, Value};
use std::collections::HashSet;

pub const JOB_STATES: &[&str] = &[
    "queued", "preflight", "training", "validating", "completed",
    "failed", "cancelled",
];
pub const DATASET_STATES: &[&str] = &["prepared", "invalidated", "archived"];
pub const CANDIDATE_STATES: &[&str] = &[
    "candidate", "validated", "staged", "active", "rejected", "retired",
];
pub const RELEASE_ACTIONS: &[&str] =
    &["stage", "activate", "rollback", "retire"];
pub const BASE_MODEL_ID: &str = "xingcheng-native-transformer";
pub const TRAINING_METHOD: &str = "sft-native-full-parameter";

/// §11 — the only legal training-job transitions (FAIL-CLOSED else).
pub fn job_transition_ok(from: &str, to: &str) -> bool {
    match from {
        "queued" => matches!(to, "preflight" | "cancelled" | "failed"),
        "preflight" => matches!(to, "training" | "cancelled" | "failed"),
        "training" => matches!(to, "validating" | "cancelled" | "failed"),
        "validating" => matches!(to, "completed" | "failed"),
        _ => false,
    }
}

// ---- shared helpers --------------------------------------------------------

pub fn spec_for(
    st: &State,
    rt: &str,
    id: &str,
    event_type: &str,
    payload: Value,
) -> mt::EventSpec {
    let (prev, rev) = match st.get(rt, id) {
        Some(r) => (r.event_hash.clone(), r.revision + 1),
        None => (mt::GENESIS.into(), 1),
    };
    mt::EventSpec {
        event_type: event_type.into(),
        record_type: rt.into(),
        record_id: id.into(),
        payload,
        previous_revision_hash: prev,
        revision: rev,
    }
}

pub fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}
pub fn f64v(v: &Value, k: &str) -> f64 {
    v.get(k).and_then(|x| x.as_f64()).unwrap_or(0.0)
}
pub fn i64v(v: &Value, k: &str) -> i64 {
    v.get(k).and_then(|x| x.as_i64()).unwrap_or(0)
}

pub fn need(v: &Value, k: &str) -> Result<String, String> {
    let s = s(v, k);
    if s.is_empty() {
        Err(format!("META_FIELD_REQUIRED: {k}"))
    } else {
        Ok(s)
    }
}

pub fn need_sha256(v: &Value, k: &str) -> Result<String, String> {
    let s = s(v, k).to_lowercase();
    if s.len() != 64 || !s.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(format!("META_SHA256_INVALID: {k}"));
    }
    Ok(s)
}

fn need_json(v: &Value, k: &str) -> Result<String, String> {
    let s = need(v, k)?;
    serde_json::from_str::<Value>(&s)
        .map_err(|e| format!("META_JSON_INVALID: {k}: {e}"))?;
    Ok(s)
}

fn check_expected(st: &State, rt: &str, id: &str, p: &Value) -> Result<(), String> {
    if let Some(exp) = p.get("expected_revision").and_then(|x| x.as_u64()) {
        let cur = st.get(rt, id).map(|r| r.revision).unwrap_or(0);
        if cur != exp {
            return Err(format!(
                "XSTORE_REVISION_CONFLICT: {rt}/{id} at {cur}, expected {exp}"
            ));
        }
    }
    Ok(())
}

pub fn audit_spec(entity_type: &str, entity_id: &str, event_type: &str, payload: Value) -> mt::EventSpec {
    let eid = mt::new_id("star-transformer-audit");
    mt::EventSpec {
        event_type: event_type.into(),
        record_type: mt::RT_AUDIT.into(),
        record_id: eid.clone(),
        payload: json!({
            "event_id": eid,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": payload,
            "created_at": mt::now_iso(),
        }),
        previous_revision_hash: mt::GENESIS.into(),
        revision: 1,
    }
}

// ---- create_dataset ---------------------------------------------------------

fn validate_examples(v: &Value) -> Result<Vec<Value>, String> {
    let list = v
        .get("examples")
        .and_then(|x| x.as_array())
        .ok_or("META_FIELD_REQUIRED: examples")?;
    let mut seen = HashSet::new();
    let mut out = Vec::with_capacity(list.len());
    for (i, e) in list.iter().enumerate() {
        let split = s(e, "split").to_lowercase();
        if split != "train" && split != "validation" {
            return Err("META_EXAMPLE_SPLIT: must be train|validation".into());
        }
        let scope = s(e, "database_scope").to_lowercase();
        if !["main", "investment", "mathematical", "coding"].contains(&scope.as_str()) {
            return Err("META_EXAMPLE_SCOPE: unsupported database_scope".into());
        }
        let ch = need_sha256(e, "content_sha256")?;
        if !seen.insert(ch.clone()) {
            return Err("META_EXAMPLE_DUP: duplicate content hash".into());
        }
        let q = f64v(e, "quality_score");
        if !(0.8..=1.0).contains(&q) {
            return Err("META_EXAMPLE_QUALITY: outside [0.8,1.0]".into());
        }
        let rev = i64v(e, "source_revision");
        if rev < 1 {
            return Err("META_EXAMPLE_REVISION: must be >= 1".into());
        }
        for k in ["owner_model_id", "source_example_id", "source_type"] {
            if s(e, k).is_empty() {
                return Err(format!("META_FIELD_REQUIRED: examples.{k}"));
            }
        }
        out.push(json!({
            "ordinal": (i + 1) as i64,
            "split": split,
            "owner_model_id": s(e, "owner_model_id"),
            "database_scope": scope,
            "source_example_id": s(e, "source_example_id"),
            "source_revision": rev,
            "content_sha256": ch,
            "source_type": s(e, "source_type"),
            "quality_score": q,
        }));
    }
    Ok(out)
}

fn sha256_file(path: &str) -> Result<String, String> {
    let data = std::fs::read(path)
        .map_err(|e| format!("META_SNAPSHOT_MISSING: {e}"))?;
    Ok(crate::hash::sha256_hex(&data))
}

/// UNIQUE(content_sha256,snapshot_sha256) — a repeat registers nothing:
/// the PG semantics are "return the existing row" (§9 immutability).
pub fn create_dataset(st: &State, p: &Value) -> Result<(Vec<mt::EventSpec>, Value), String> {
    let content = need_sha256(p, "content_sha256")?;
    let snap_sha = need_sha256(p, "snapshot_sha256")?;
    let manifest_json = need_json(p, "source_manifest_json")?;
    let snap_path = need(p, "snapshot_path")?;
    if p.get("skip_file_check").and_then(|x| x.as_bool()) != Some(true)
        && sha256_file(&snap_path)? != snap_sha
    {
        return Err("META_SNAPSHOT_HASH_MISMATCH".into());
    }
    let examples = validate_examples(p)?;
    let train = examples.iter().filter(|e| e["split"] == "train").count() as i64;
    let val = examples.len() as i64 - train;
    if train < 1 || val < 1 {
        return Err("META_DATASET_EMPTY_SPLIT: need train+validation".into());
    }
    let dataset_id = format!(
        "star-transformer-dataset-{}",
        &mt::sha256_text(&format!("{content}:{snap_sha}"))[..24]
    );
    let uniq = format!("{content}|{snap_sha}");
    if let Some(r) = st
        .list(mt::RT_DATASET)
        .find(|r| s(&r.payload, "content_sha256") == content
            && s(&r.payload, "snapshot_sha256") == snap_sha)
    {
        return Ok((Vec::new(), json!({
            "result": "existing",
            "dataset_id": r.record_id,
            "unique_key": uniq,
        })));
    }
    let min_q = examples
        .iter()
        .map(|e| e["quality_score"].as_f64().unwrap_or(0.0))
        .fold(f64::MAX, f64::min);
    let fmt = {
        let f = s(p, "format_version");
        if f.is_empty() { "star-transformer-sft/v1".into() } else { f }
    };
    let created_by = {
        let c = s(p, "created_by");
        if c.is_empty() { "star-main-native-model".into() } else { c }
    };
    let now = mt::now_iso();
    let mut specs = Vec::with_capacity(examples.len() + 2);
    specs.push(spec_for(st, mt::RT_DATASET, &dataset_id, "dataset-created", json!({
        "dataset_id": dataset_id,
        "content_sha256": content,
        "format_version": fmt,
        "base_model_id": BASE_MODEL_ID,
        "runtime_model_id": BASE_MODEL_ID,
        "example_count": examples.len() as i64,
        "training_example_count": train,
        "validation_example_count": val,
        "minimum_quality_score": min_q,
        "source_manifest_json": manifest_json,
        "snapshot_path": snap_path,
        "snapshot_sha256": snap_sha,
        "state": "prepared",
        "created_by": created_by,
        "created_at": now,
    })));
    for e in &examples {
        let ord = e["ordinal"].as_i64().unwrap_or(0);
        let rid = format!("{dataset_id}/{ord}");
        let mut ep = e.clone();
        ep["dataset_id"] = json!(dataset_id);
        specs.push(spec_for(
            st, mt::RT_DATASET_EXAMPLE, &rid, "dataset-example-registered", ep,
        ));
    }
    specs.push(audit_spec("training-dataset", &dataset_id, "dataset-created", json!({
        "content_sha256": content,
        "snapshot_sha256": snap_sha,
        "example_count": examples.len(),
        "training_example_count": train,
        "validation_example_count": val,
        "created_by": created_by,
    })));
    Ok((specs, json!({"result": "created", "dataset_id": dataset_id})))
}

// ---- jobs -------------------------------------------------------------------

pub fn create_job(st: &State, p: &Value) -> Result<(Vec<mt::EventSpec>, Value), String> {
    let dataset_id = need(p, "dataset_id")?;
    let ds = st
        .get(mt::RT_DATASET, &dataset_id)
        .ok_or("META_DATASET_MISSING: transformer training dataset does not exist")?;
    if s(&ds.payload, "state") != "prepared" {
        return Err("META_DATASET_NOT_PREPARED".into());
    }
    let cfg_json = need_json(p, "configuration_json")?;
    let job_id = {
        let j = s(p, "job_id");
        if j.is_empty() { mt::new_id("star-transformer-job") } else { j }
    };
    if st.get(mt::RT_TRAINING_JOB, &job_id).is_some() {
        return Err(format!("XSTORE_REVISION_CONFLICT: {job_id} exists"));
    }
    let requested_by = {
        let r = s(p, "requested_by");
        if r.is_empty() { "star-main-native-model".into() } else { r }
    };
    let cfg_sha = mt::sha256_text(&cfg_json);
    let payload = json!({
        "job_id": job_id,
        "dataset_id": dataset_id,
        "base_model_id": BASE_MODEL_ID,
        "training_method": TRAINING_METHOD,
        "configuration_json": cfg_json,
        "configuration_sha256": cfg_sha,
        "status": "queued",
        "output_path": "",
        "error_code": "",
        "error_message": "",
        "requested_by": requested_by,
        "retry_of_job_id": Value::Null,
        "created_at": mt::now_iso(),
        "started_at": "",
        "completed_at": "",
    });
    let mut v = payload.clone();
    if !s(p, "retry_of_job_id").is_empty() {
        let rj = s(p, "retry_of_job_id");
        if st.get(mt::RT_TRAINING_JOB, &rj).is_none() {
            return Err("META_JOB_RETRY_PARENT_MISSING".into());
        }
        v["retry_of_job_id"] = json!(rj);
    }
    let mut specs = vec![spec_for(st, mt::RT_TRAINING_JOB, &job_id, "job-queued", v)];
    specs.push(audit_spec("training-job", &job_id, "training-job-created", json!({
        "dataset_id": dataset_id,
        "configuration_sha256": cfg_sha,
        "requested_by": requested_by,
    })));
    Ok((specs, json!({"result": "queued", "job_id": job_id})))
}

pub fn transition_job(st: &State, p: &Value) -> Result<(Vec<mt::EventSpec>, Value), String> {
    let job_id = need(p, "job_id")?;
    let to = s(p, "to").to_lowercase();
    if !JOB_STATES.contains(&to.as_str()) {
        return Err("META_JOB_STATE_UNKNOWN".into());
    }
    let rec = st
        .get(mt::RT_TRAINING_JOB, &job_id)
        .ok_or("META_JOB_MISSING")?;
    check_expected(st, mt::RT_TRAINING_JOB, &job_id, p)?;
    let from = s(&rec.payload, "status");
    if !job_transition_ok(&from, &to) {
        return Err(format!(
            "XSTORE_INVALID_TRANSITION: {from} -> {to}"
        ));
    }
    let mut np = rec.payload.clone();
    let now = mt::now_iso();
    np["status"] = json!(to);
    if !s(p, "output_path").is_empty() {
        np["output_path"] = json!(s(p, "output_path"));
    }
    let mut code = s(p, "error_code");
    code.truncate(96);
    let mut msg = s(p, "error_message");
    msg.truncate(1000);
    np["error_code"] = json!(code);
    np["error_message"] = json!(msg);
    if to == "training" {
        np["started_at"] = json!(now);
    }
    if ["completed", "failed", "cancelled"].contains(&to.as_str()) {
        np["completed_at"] = json!(now);
    }
    let mut specs = vec![spec_for(st, mt::RT_TRAINING_JOB, &job_id, "job-transitioned", np)];
    specs.push(audit_spec("training-job", &job_id, "training-job-transitioned", json!({
        "from": from, "to": to, "error_code": code,
    })));
    Ok((specs, json!({"result": "transitioned", "job_id": job_id, "status": to})))
}

/// Serial-training-lane atomic claim (advisory-lock port, §13/§76):
/// under the writer lease, verify still-queued + no sibling in flight,
/// then transition to preflight — all in one commit.
pub fn claim_job(st: &State, p: &Value) -> Result<(Vec<mt::EventSpec>, Value), String> {
    let job_id = need(p, "job_id")?;
    let rec = match st.get(mt::RT_TRAINING_JOB, &job_id) {
        None => return Ok((Vec::new(), json!({"result": "missing"}))),
        Some(r) => r,
    };
    if s(&rec.payload, "status") != "queued" {
        return Ok((Vec::new(), json!({
            "result": "not_queued", "status": s(&rec.payload, "status"),
        })));
    }
    let busy = st.list(mt::RT_TRAINING_JOB).any(|r| {
        ["preflight", "training", "validating"]
            .contains(&s(&r.payload, "status").as_str())
    });
    if busy {
        return Ok((Vec::new(), json!({"result": "busy"})));
    }
    let mut np = rec.payload.clone();
    np["status"] = json!("preflight");
    np["error_code"] = json!("");
    np["error_message"] = json!("");
    let mut specs = vec![spec_for(st, mt::RT_TRAINING_JOB, &job_id, "job-preflight", np)];
    specs.push(audit_spec("training-job", &job_id, "training-job-transitioned", json!({
        "from": "queued", "to": "preflight",
        "error_code": "", "serial_claim": true,
    })));
    Ok((specs, json!({"result": "claimed", "job_id": job_id})))
}

// ---- generic ops ------------------------------------------------------------

/// Generic typed insert/update for the non-PG-table domains
/// (capability evidence, generation, lifecycle, teacher, resource
/// receipts, ...). Identity: record_type + record_id; append-only types
/// reject a second write (§5); stateful types upsert with revision++.
pub fn generic_put(st: &State, rt: &str, p: &Value) -> Result<(Vec<mt::EventSpec>, Value), String> {
    if !mt::is_known_type(rt) {
        return Err(format!("XSTORE_TYPE_UNKNOWN: {rt}"));
    }
    let rec = p.get("record").cloned().unwrap_or(Value::Null);
    let id = need(&rec, "record_id")?;
    if mt::is_singleton(rt) && id != "1" && rt == mt::RT_RUNTIME_STATE {
        return Err("META_SINGLETON_ID: runtime_model_state id must be 1".into());
    }
    let existing = st.get(rt, &id);
    if mt::is_append_only(rt) && existing.is_some() {
        return Err(format!("XSTORE_IMMUTABLE: {rt}/{id}"));
    }
    check_expected(st, rt, &id, p)?;
    let mut payload = rec.clone();
    payload["updated_at"] = json!(mt::now_iso());
    if existing.is_none() {
        payload["created_at"] = json!(mt::now_iso());
    }
    let ev = if existing.is_some() { "record-updated" } else { "record-created" };
    Ok((
        vec![spec_for(st, rt, &id, ev, payload)],
        json!({"result": ev, "record_id": id}),
    ))
}

/// Generic state transition for dataset state / singleton documents
/// (job and candidate transitions go through their domain ops — the
/// state machines are not expressible as generic field writes).
pub fn generic_transition(st: &State, rt: &str, p: &Value) -> Result<(Vec<mt::EventSpec>, Value), String> {
    let id = need(p, "record_id")?;
    let rec = st.get(rt, &id).ok_or("META_RECORD_MISSING")?;
    if mt::is_append_only(rt) {
        return Err(format!("XSTORE_IMMUTABLE: {rt}/{id}"));
    }
    check_expected(st, rt, &id, p)?;
    let to = s(p, "to");
    if to.is_empty() {
        return Err("META_FIELD_REQUIRED: to".into());
    }
    let mut np = rec.payload.clone();
    match rt {
        mt::RT_DATASET => {
            if !DATASET_STATES.contains(&to.as_str()) {
                return Err("META_DATASET_STATE_UNKNOWN".into());
            }
            np["state"] = json!(to);
        }
        mt::RT_TRAINING_JOB | mt::RT_CANDIDATE | mt::RT_RUNTIME_STATE => {
            return Err(format!(
                "XSTORE_OP_DENIED: {rt} transitions are domain ops"
            ));
        }
        _ => {
            np["state"] = json!(to);
        }
    }
    np["updated_at"] = json!(mt::now_iso());
    Ok((
        vec![spec_for(st, rt, &id, "record-transitioned", np)],
        json!({"result": "transitioned", "record_id": id, "state": to}),
    ))
}

/// Standalone audit event (the audit_event record type — §17: this log
/// position IS the canonical audit entry).
pub fn audit_event(p: &Value) -> Result<(Vec<mt::EventSpec>, Value), String> {
    let et = need(p, "event_type")?;
    let entity_type = need(p, "entity_type")?;
    let entity_id = need(p, "entity_id")?;
    let payload = p.get("payload").cloned().unwrap_or(json!({}));
    let spec = audit_spec(&entity_type, &entity_id, &et, payload);
    Ok((vec![spec], json!({"result": "audited"})))
}

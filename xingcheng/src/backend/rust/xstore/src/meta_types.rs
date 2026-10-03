//! meta_types.rs — xstore metadata plane: canonical types (§4,§6,§27,§28).
//!
//! The event log under <store>/metadata/events/ is the sole canonical
//! record of metadata state (§22); every MaterializedRecord is a
//! derived projection that must be rebuildable from events alone.
//!
//! Canonical serialization (§28): serde_json::Value maps are BTreeMap-
//! ordered, so `to_string(&Value)` yields sorted-key compact bytes —
//! that byte string is the only canonical encoding. C# callers never
//! rebuild hash bytes; they ship a JSON payload and Rust re-canonicalizes
//! it before hashing.

use crate::hash;
use serde_json::{json, Value};

pub const GENESIS: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";
pub const EVENT_FORMAT: &str = "star-xstore-metadata-event/v1";
pub const RECEIPT_FORMAT: &str = "star-xstore-metadata-receipt/v1";
pub const SNAPSHOT_FORMAT: &str = "star-xstore-metadata-snapshot/v1";
pub const SNAPBODY_FORMAT: &str = "star-xstore-metadata-snapshot-body/v1";
pub const SCHEMA_FORMAT: &str = "star-xstore-metadata-schema/v1";
pub const SCHEMA_IDENTITY: &str = "xingcheng-metadata/v1";

// ---- record types ---------------------------------------------------------

pub const RT_DATASET: &str = "training_dataset";
pub const RT_DATASET_EXAMPLE: &str = "dataset_example";
pub const RT_TRAINING_JOB: &str = "training_job";
pub const RT_CANDIDATE: &str = "adapter_candidate";
pub const RT_EVALUATION: &str = "adapter_evaluation";
pub const RT_RELEASE: &str = "adapter_release";
pub const RT_RUNTIME_STATE: &str = "runtime_model_state";
pub const RT_AUDIT: &str = "audit_event";
pub const RT_CAP_EVIDENCE: &str = "capability_evidence";
pub const RT_CAP_FLOOR: &str = "capability_floor";
pub const RT_CAP_DELTA: &str = "capability_delta";
pub const RT_CAP_BINDING: &str = "capability_binding";
pub const RT_CAP_MATURITY: &str = "capability_maturity";
pub const RT_GENERATION: &str = "generation_record";
pub const RT_LIFECYCLE: &str = "lifecycle_state";
pub const RT_SELFLEARN: &str = "self_learning_state";
pub const RT_MATURATION: &str = "maturation_state";
pub const RT_TEACHER: &str = "teacher_evidence";
pub const RT_GRANT_REF: &str = "resource_grant_ref";
pub const RT_RES_RECEIPT: &str = "resource_usage_receipt";
pub const RT_SCHEMA_META: &str = "schema_metadata";
pub const RT_MIGRATION: &str = "migration_marker";
/// Role-DB source lanes (§54-§55): per-scope training corpora that the
/// self-learning collectors read. Stateful — external writers update
/// active/quality_score/paired, so these upsert with revision++.
pub const RT_ROLE_EXAMPLE: &str = "role_training_example";
pub const RT_ROLE_PAIR: &str = "role_preference_pair";

/// Every registered record type — unknown types fail closed.
pub const RECORD_TYPES: &[&str] = &[
    RT_DATASET, RT_DATASET_EXAMPLE, RT_TRAINING_JOB, RT_CANDIDATE,
    RT_EVALUATION, RT_RELEASE, RT_RUNTIME_STATE, RT_AUDIT,
    RT_CAP_EVIDENCE, RT_CAP_FLOOR, RT_CAP_DELTA, RT_CAP_BINDING,
    RT_CAP_MATURITY, RT_GENERATION, RT_LIFECYCLE, RT_SELFLEARN,
    RT_MATURATION, RT_TEACHER, RT_GRANT_REF, RT_RES_RECEIPT,
    RT_SCHEMA_META, RT_MIGRATION, RT_ROLE_EXAMPLE, RT_ROLE_PAIR,
    "rag_generation", "rag_chunk", "rag_resource", "rag_tombstone", "rag_index_state",
    "codex_row",
];

/// Append-only types: a committed record may never be mutated (§5 —
/// no silent overwrite). revision must stay 1.
pub const APPEND_ONLY: &[&str] = &[
    RT_DATASET_EXAMPLE, RT_EVALUATION, RT_RELEASE, RT_AUDIT,
    RT_CAP_EVIDENCE, RT_TEACHER, RT_RES_RECEIPT, RT_MIGRATION,
];

/// Singleton types: exactly one record id ("1") may exist.
pub const SINGLETONS: &[&str] = &[
    RT_RUNTIME_STATE, RT_LIFECYCLE, RT_SELFLEARN, RT_MATURATION,
];

pub fn is_append_only(rt: &str) -> bool {
    APPEND_ONLY.contains(&rt)
}
pub fn is_singleton(rt: &str) -> bool {
    SINGLETONS.contains(&rt)
}
pub fn is_known_type(rt: &str) -> bool {
    RECORD_TYPES.contains(&rt)
}

// ---- canonical helpers ----------------------------------------------------

/// Canonical byte encoding (§28) — the one true serialization.
pub fn canonical(v: &Value) -> String {
    serde_json::to_string(v).expect("serde_json::Value always serializes")
}

pub fn sha256_text(s: &str) -> String {
    hash::sha256_hex(s.as_bytes())
}

/// UTC timestamp in the same shape the C# lane emits
/// ("yyyy-MM-dd'T'HH:mm:ss.ffffff+00:00") so cross-lane reports compare
/// cleanly.
pub fn now_iso() -> String {
    let t = time::OffsetDateTime::now_utc();
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:06}+00:00",
        t.year(),
        t.month() as u8,
        t.day(),
        t.hour(),
        t.minute(),
        t.second(),
        t.nanosecond() / 1000
    )
}

pub fn new_id(prefix: &str) -> String {
    // Random-free deterministic uniqueness: pid + nanos + a per-process
    // counter keeps ids unique across rapid-fire CLI invocations.
    use std::sync::atomic::{AtomicU64, Ordering};
    static CTR: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!(
        "{prefix}-{:024x}",
        (nanos ^ ((std::process::id() as u128) << 64))
            .wrapping_add(CTR.fetch_add(1, Ordering::Relaxed) as u128)
    )
}

// ---- events ----------------------------------------------------------------

/// One intended state mutation, produced by domain validation
/// (meta_domain) and committed by meta_tx.
pub struct EventSpec {
    pub event_type: String,
    pub record_type: String,
    pub record_id: String,
    /// Full materialized post-state of the record.
    pub payload: Value,
    /// event_hash of this record's previous revision (GENESIS on create).
    pub previous_revision_hash: String,
    /// 1 + previous revision (1 on create).
    pub revision: u64,
}

/// event_hash = sha256(canonical(body minus the event_hash field))
/// where the body already carries previous_hash + payload — satisfies
/// §27 "previous_hash + canonical payload -> event_hash".
pub fn event_hash_of(v: &Value) -> Result<String, String> {
    let mut body = v.clone();
    body.as_object_mut()
        .ok_or("EVENT_NOT_OBJECT")?
        .remove("event_hash");
    Ok(sha256_text(&canonical(&body)))
}

/// Build the canonical event line for one spec. Returns (line_bytes,
/// event_hash). `previous_hash` chains the previous *raw line* (audit.rs
/// discipline: tampering with any byte breaks the next link).
pub fn make_event_line(
    seq: u64,
    transaction_id: &str,
    operation_id: &str,
    spec: &EventSpec,
    actor: &str,
    previous_hash: &str,
    writer_epoch: u64,
) -> Result<(String, String), String> {
    if !is_known_type(&spec.record_type) {
        return Err(format!("XSTORE_TYPE_UNKNOWN: {}", spec.record_type));
    }
    let payload_hash = sha256_text(&canonical(&spec.payload));
    let mut ev = json!({
        "format": EVENT_FORMAT,
        "seq": seq,
        "transaction_id": transaction_id,
        "operation_id": operation_id,
        "event_id": new_id("xme"),
        "event_type": spec.event_type,
        "record_type": spec.record_type,
        "record_id": spec.record_id,
        "revision": spec.revision,
        "actor": actor,
        "created_at": now_iso(),
        "previous_hash": previous_hash,
        "previous_revision_hash": spec.previous_revision_hash,
        "payload_hash": payload_hash,
        "payload": spec.payload,
        "writer_epoch": writer_epoch,
    });
    let eh = event_hash_of(&ev)?;
    ev["event_hash"] = Value::String(eh.clone());
    Ok((canonical(&ev), eh))
}

/// Re-derive the event_hash of a parsed event line.
pub fn verify_event_line(line: &str) -> Result<Value, String> {
    let v: Value = serde_json::from_str(line)
        .map_err(|e| format!("EVENT_CORRUPT: {e}"))?;
    if v.get("format").and_then(|x| x.as_str()) != Some(EVENT_FORMAT) {
        return Err("EVENT_CORRUPT: bad format tag".into());
    }
    let stored = v
        .get("event_hash")
        .and_then(|x| x.as_str())
        .ok_or("EVENT_CORRUPT: missing event_hash")?;
    if event_hash_of(&v)? != stored {
        return Err("EVENT_CORRUPT: event_hash mismatch".into());
    }
    let ph = sha256_text(&canonical(
        v.get("payload").ok_or("EVENT_CORRUPT: missing payload")?,
    ));
    if ph != v.get("payload_hash").and_then(|x| x.as_str()).unwrap_or("") {
        return Err("EVENT_CORRUPT: payload_hash mismatch".into());
    }
    Ok(v)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec() -> EventSpec {
        EventSpec {
            event_type: "test-created".into(),
            record_type: RT_AUDIT.into(),
            record_id: "r1".into(),
            payload: json!({"a": 1, "b": "x"}),
            previous_revision_hash: GENESIS.into(),
            revision: 1,
        }
    }

    #[test]
    fn event_line_roundtrip_and_tamper() {
        let (line, eh) =
            make_event_line(1, "tx1", "op1", &spec(), "tester", GENESIS, 1)
                .unwrap();
        let v = verify_event_line(&line).unwrap();
        assert_eq!(v["event_hash"], eh);
        let bad = line.replacen("\"a\":1", "\"a\":2", 1);
        assert!(verify_event_line(&bad).is_err());
    }

    #[test]
    fn unknown_record_type_denied() {
        let mut s = spec();
        s.record_type = "nope".into();
        assert!(make_event_line(1, "t", "o", &s, "a", GENESIS, 1).is_err());
    }
}

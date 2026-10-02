//! meta_state.rs — derived materialized state (§22-§23).
//!
//! State = deterministic fold over the canonical event log. Everything
//! here is rebuildable: the index files under metadata/index/ are caches
//! of exactly this fold (§23 — an index is never authority).
//!
//! Replay also re-enforces the invariants that PG enforced with
//! constraints/triggers (§8): a committed event that would violate an
//! invariant indicates corruption, not history — startup reports
//! RECOVERY_REQUIRED (§84) instead of silently adopting it (§85).

use crate::meta_log::{LogEvent, Scan};
use crate::meta_types as mt;
use serde_json::{json, Value};
use std::collections::BTreeMap;

/// Latest state of one record — the derived MetadataRecord (§4/§6).
#[derive(Clone)]
pub struct Record {
    pub record_type: String,
    pub record_id: String,
    pub revision: u64,
    pub event_hash: String,
    pub created_at: String,
    pub payload: Value,
}

#[derive(Default)]
pub struct State {
    /// key: "<record_type>/<record_id>"
    pub records: BTreeMap<String, Record>,
    /// operation_id -> committed result summary (idempotency, §79)
    pub operations: BTreeMap<String, Value>,
    pub event_count: u64,
    pub head_hash: String,
}

pub fn key(rt: &str, id: &str) -> String {
    format!("{rt}/{id}")
}

impl State {
    pub fn get(&self, rt: &str, id: &str) -> Option<&Record> {
        self.records.get(&key(rt, id))
    }
    pub fn list<'a>(&'a self, rt: &str) -> impl Iterator<Item = &'a Record> {
        let prefix = format!("{rt}/");
        self.records
            .range(prefix.clone()..)
            .take_while(move |(k, _)| k.starts_with(&prefix))
            .map(|(_, v)| v)
    }
    #[allow(dead_code)]
    pub fn find_by(&self, rt: &str, field: &str, val: &str) -> Option<&Record> {
        self.list(rt).find(|r| {
            r.payload.get(field).and_then(|v| v.as_str()) == Some(val)
        })
    }
}

/// Apply one verified event to the fold, re-checking the invariants a
/// compliant writer enforced at commit time. Returns Err on committed
/// corruption (callers surface RECOVERY_REQUIRED, §84).
fn apply(st: &mut State, ev: &LogEvent) -> Result<(), String> {
    let v = &ev.value;
    let rt = v["record_type"].as_str().unwrap_or("");
    let id = v["record_id"].as_str().unwrap_or("");
    let rev = v["revision"].as_u64().unwrap_or(0);
    let k = key(rt, id);
    match st.records.get(&k) {
        None => {
            if rev != 1 {
                return Err(format!(
                    "RECOVERY_REQUIRED: {k} first revision {rev}"
                ));
            }
        }
        Some(r) => {
            if mt::is_append_only(rt) {
                return Err(format!(
                    "RECOVERY_REQUIRED: append-only {k} mutated"
                ));
            }
            if rev != r.revision + 1 {
                return Err(format!(
                    "RECOVERY_REQUIRED: {k} revision gap {} -> {rev}",
                    r.revision
                ));
            }
            let expect_prev = r.event_hash.clone();
            if v["previous_revision_hash"].as_str().unwrap_or("") != expect_prev {
                return Err(format!(
                    "RECOVERY_REQUIRED: {k} revision chain break"
                ));
            }
        }
    }
    st.records.insert(
        k,
        Record {
            record_type: rt.into(),
            record_id: id.into(),
            revision: rev,
            event_hash: v["event_hash"].as_str().unwrap_or("").into(),
            created_at: v["created_at"].as_str().unwrap_or("").into(),
            payload: v["payload"].clone(),
        },
    );
    let op = v["operation_id"].as_str().unwrap_or("");
    if !op.is_empty() {
        st.operations.entry(op.into()).or_insert_with(|| json!({
            "operation_id": op,
            "transaction_id": v["transaction_id"].as_str().unwrap_or(""),
            "event_hash": v["event_hash"].as_str().unwrap_or(""),
        }));
    }
    Ok(())
}

/// Fold a canonical scan into state, then run the cross-record
/// invariants (§84): single active candidate, runtime singleton shape,
/// job statuses inside the legal set.
pub fn materialize(scan: &Scan) -> Result<State, String> {
    let mut st = State {
        event_count: scan.events.len() as u64,
        head_hash: scan.head_hash.clone(),
        ..Default::default()
    };
    for ev in &scan.events {
        apply(&mut st, ev)
            .map_err(|e| format!("XSTORE_RECOVERY_REQUIRED: {e}"))?;
    }
    check_invariants(&st)?;
    Ok(st)
}

pub fn check_invariants(st: &State) -> Result<(), String> {
    let active = st
        .list(mt::RT_CANDIDATE)
        .filter(|r| r.payload["status"].as_str() == Some("active"))
        .count();
    if active > 1 {
        return Err(format!(
            "XSTORE_RECOVERY_REQUIRED: {active} active candidates"
        ));
    }
    if st.list(mt::RT_RUNTIME_STATE).count() > 1 {
        return Err(
            "XSTORE_RECOVERY_REQUIRED: duplicate runtime state".into()
        );
    }
    if let Some(r) = st.get(mt::RT_RUNTIME_STATE, "1") {
        if r.payload["automatic_weight_replacement"].as_bool()
            != Some(false)
        {
            return Err(
                "XSTORE_RECOVERY_REQUIRED: automatic_weight_replacement"
                    .into(),
            );
        }
    }
    for r in st.list(mt::RT_TRAINING_JOB) {
        let s = r.payload["status"].as_str().unwrap_or("");
        if !crate::meta_domain::JOB_STATES.contains(&s) {
            return Err(format!(
                "XSTORE_RECOVERY_REQUIRED: job {} illegal status {s}",
                r.record_id
            ));
        }
    }
    for r in st.list(mt::RT_CANDIDATE) {
        let s = r.payload["status"].as_str().unwrap_or("");
        if !crate::meta_domain::CANDIDATE_STATES.contains(&s) {
            return Err(format!(
                "XSTORE_RECOVERY_REQUIRED: candidate {} illegal status {s}",
                r.record_id
            ));
        }
    }
    for r in st.list(mt::RT_DATASET) {
        let s = r.payload["state"].as_str().unwrap_or("");
        if !crate::meta_domain::DATASET_STATES.contains(&s) {
            return Err(format!(
                "XSTORE_RECOVERY_REQUIRED: dataset {} illegal state {s}",
                r.record_id
            ));
        }
    }
    Ok(())
}

/// Events for replay starting after `from_seq` (snapshot acceleration:
/// materialize a snapshot body then fold only the tail, §83).
pub fn replay_into(st: &mut State, scan: &Scan, from_seq: u64) -> Result<(), String> {
    for ev in &scan.events {
        if ev.seq > from_seq {
            apply(st, ev)
                .map_err(|e| format!("XSTORE_RECOVERY_REQUIRED: {e}"))?;
        }
    }
    st.event_count = scan.events.len() as u64;
    st.head_hash = scan.head_hash.clone();
    check_invariants(st)
}

//! meta_tx.rs — MetadataTransaction orchestration (§4,§7,§24).
//!
//! Every mutation is one governed transaction:
//!   acquire writer lease (single-writer, §71)
//!   -> load state (canonical; index fast path when in step, §25)
//!   -> idempotency check on operation_id (§79)
//!   -> domain validate + expand to EventSpecs
//!   -> append event lines + fsync   (commit point)
//!   -> append star-xstore-metadata-receipt/v1 to audit receipts (§7)
//!   -> update derived index          (never authority, §23)
//!   -> release lease
//!
//! §77: mutations that supply expected_revision are CAS-checked during
//! validate; a mismatch aborts before any byte is written.

use crate::meta_domain as dom;
use crate::meta_index as idx;
use crate::meta_lease as lease;
use crate::meta_log as log;
use crate::meta_release as rel;
use crate::meta_state::State;
use crate::meta_types as mt;
use serde_json::{json, Value};
use std::io::Write;
use std::path::{Path, PathBuf};

fn schema_path(store: &Path) -> PathBuf {
    store.join("metadata").join("schema.json")
}

/// Write the immutable schema identity once; refuse a store whose
/// schema file declares a different identity (§89 stable identity).
pub fn ensure_schema(store: &Path) -> Result<(), String> {
    let p = schema_path(store);
    if let Ok(text) = std::fs::read_to_string(&p) {
        let v: Value = serde_json::from_str(&text)
            .map_err(|e| format!("META_SCHEMA_CORRUPT: {e}"))?;
        if v.get("schema_identity").and_then(|x| x.as_str())
            != Some(mt::SCHEMA_IDENTITY)
        {
            return Err("META_SCHEMA_MISMATCH".into());
        }
        return Ok(());
    }
    let tmp = p.with_extension("tmp");
    if let Some(d) = p.parent() {
        std::fs::create_dir_all(d).map_err(|e| format!("META_SCHEMA_WRITE: {e}"))?;
    }
    {
        let mut f = std::fs::File::create(&tmp)
            .map_err(|e| format!("META_SCHEMA_WRITE: {e}"))?;
        let body = mt::canonical(&json!({
            "format": mt::SCHEMA_FORMAT,
            "schema_identity": mt::SCHEMA_IDENTITY,
            "created_at": mt::now_iso(),
        }));
        f.write_all(body.as_bytes())
            .and_then(|_| f.sync_all())
            .map_err(|e| format!("META_SCHEMA_WRITE: {e}"))?;
    }
    std::fs::rename(&tmp, &p).map_err(|e| format!("META_SCHEMA_WRITE: {e}"))
}

fn dispatch(
    st: &State,
    op: &str,
    p: &Value,
) -> Result<(Vec<mt::EventSpec>, Value), String> {
    match op {
        "create_dataset" => dom::create_dataset(st, p),
        "create_job" => dom::create_job(st, p),
        "transition_job" => dom::transition_job(st, p),
        "claim_job" => dom::claim_job(st, p),
        "register_candidate" => rel::register_candidate(st, p),
        "record_evaluation" => rel::record_evaluation(st, p),
        "release" => rel::release(st, p),
        "reject_candidate" => rel::reject_candidate(st, p),
        "init_runtime" => rel::init_runtime(st, p),
        "audit" => dom::audit_event(p),
        "put_many" => dom::put_many(st, p),
        "audit_many" => dom::audit_many(p),
        "put" => {
            let rt = p.get("record_type").and_then(|x| x.as_str())
                .ok_or("META_FIELD_REQUIRED: record_type")?;
            dom::generic_put(st, rt, p)
        }
        "transition" => {
            let rt = p.get("record_type").and_then(|x| x.as_str())
                .ok_or("META_FIELD_REQUIRED: record_type")?;
            dom::generic_transition(st, rt, p)
        }
        _ => Err(format!("XSTORE_OP_UNKNOWN: {op}")),
    }
}

/// Run one mutation. Returns the mutation report (result + receipt).
/// An empty spec list is a legal no-op outcome (idempotent hit, busy
/// lane) and still produces a receipt — every mutation is auditable.
pub fn mutate(
    store: &Path,
    op: &str,
    params: &Value,
    actor: &str,
) -> Result<Value, String> {
    ensure_schema(store)?;
    let operation_id = match params.get("operation_id").and_then(|x| x.as_str()) {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => mt::new_id("xmo"),
    };
    let lease = lease::acquire(store, actor, 60.0, 3000)?;
    let r = mutate_locked(store, op, params, actor, &operation_id, lease.epoch);
    lease::release(&lease);
    r
}

fn mutate_locked(
    store: &Path,
    op: &str,
    params: &Value,
    actor: &str,
    operation_id: &str,
    epoch: u64,
) -> Result<Value, String> {
    if let Some(e) = params.get("expected_epoch").and_then(|x| x.as_u64()) {
        lease::check_epoch(store, e)?;
    }
    let st = idx::load_state(store)?;
    if let Some(done) = st.operations.get(operation_id) {
        return Ok(json!({
            "format": "xstore-metadata-mutation/v1",
            "ok": true,
            "result": "duplicate",
            "operation_id": operation_id,
            "original": done,
        }));
    }
    let (mut specs, result) = dispatch(&st, op, params)?;
    // Backfill lane: domain ops carry suppress_audit so their implicit
    // audit records are not double-emitted — the migration replays the
    // authoritative audit history itself via audit_many.
    if params.get("suppress_audit").and_then(|x| x.as_bool())
        == Some(true)
    {
        specs.retain(|s| s.record_type != mt::RT_AUDIT);
    }
    let transaction_id = mt::new_id("xmt");
    let mut receipt_records = Vec::new();
    let mut head = st.head_hash.clone();
    let mut event_hashes: Vec<String> = Vec::new();
    if !specs.is_empty() {
        let (hashes, new_head) = log::commit_events(
            store, &specs, &transaction_id, operation_id, actor, epoch,
        )?;
        head = new_head;
        event_hashes = hashes;
        for (spec, eh) in specs.iter().zip(event_hashes.iter()) {
            let old_rev = st.get(&spec.record_type, &spec.record_id)
                .map(|r| r.revision)
                .unwrap_or(0);
            receipt_records.push(json!({
                "record_type": spec.record_type,
                "record_id": spec.record_id,
                "old_revision": old_rev,
                "new_revision": spec.revision,
                "event_hash": eh,
            }));
        }
    }
    // §7 receipt — one per mutation, chained in metadata/audit/.
    let receipt = json!({
        "format": mt::RECEIPT_FORMAT,
        "transaction_id": transaction_id,
        "operation_id": operation_id,
        "record_id": specs.first().map(|s| s.record_id.clone()).unwrap_or_default(),
        "records": receipt_records,
        "event_hash": event_hashes.last().cloned().unwrap_or_default(),
        "head_hash": head,
        "timestamp": mt::now_iso(),
        "actor": actor,
        "result": result,
    });
    let receipt_hash = log::append_receipt(store, receipt.clone())?;
    // §24 last step: refresh the derived index (rebuildable by design).
    if !specs.is_empty() {
        let fresh = idx::rebuild(store)?;
        let _ = fresh;
    } else {
        // no-op mutations still refresh the checkpoint lazily via head
        let _ = idx::load_state(store);
    }
    Ok(json!({
        "format": "xstore-metadata-mutation/v1",
        "ok": true,
        "result": result,
        "operation_id": operation_id,
        "transaction_id": transaction_id,
        "event_hashes": event_hashes,
        "receipt_hash": receipt_hash,
        "head_hash": head,
    }))
}

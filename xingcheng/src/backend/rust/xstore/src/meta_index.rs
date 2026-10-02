//! meta_index.rs — derived index + commit checkpoint (§22-§25).
//!
//!   <store>/metadata/index/head.json        commit checkpoint
//!   <store>/metadata/index/records.json     materialized records
//!   <store>/metadata/index/operations.json  idempotency map (§79)
//!
//! Everything here is *derived* (§23): it may be rebuilt at any time by
//! replaying the canonical event log, and is never a source of truth.
//! head.json is the last-write commit checkpoint — if a crash leaves
//! events committed but the index stale, open paths detect the drift
//! (tail hash != head.last_event_hash) and rebuild (§25).

use crate::meta_log::{self};
use crate::meta_state::{self, State};
use crate::meta_types as mt;
use serde_json::{json, Value};
use std::io::Write;
use std::path::{Path, PathBuf};

fn dir(store: &Path) -> PathBuf {
    store.join("metadata").join("index")
}
pub fn head_path(store: &Path) -> PathBuf {
    dir(store).join("head.json")
}
pub fn records_path(store: &Path) -> PathBuf {
    dir(store).join("records.json")
}
pub fn operations_path(store: &Path) -> PathBuf {
    dir(store).join("operations.json")
}

fn atomic_write(path: &Path, body: &str) -> Result<(), String> {
    let tmp = path.with_extension("tmp");
    {
        let mut f = std::fs::File::create(&tmp)
            .map_err(|e| format!("META_INDEX_WRITE: {e}"))?;
        f.write_all(body.as_bytes())
            .and_then(|_| f.sync_all())
            .map_err(|e| format!("META_INDEX_WRITE: {e}"))?;
    }
    std::fs::rename(&tmp, path).map_err(|e| format!("META_INDEX_WRITE: {e}"))
}

fn state_json(st: &State) -> Value {
    let mut records = serde_json::Map::new();
    for (k, r) in &st.records {
        records.insert(
            k.clone(),
            json!({
                "record_type": r.record_type,
                "record_id": r.record_id,
                "revision": r.revision,
                "event_hash": r.event_hash,
                "created_at": r.created_at,
                "payload": r.payload,
            }),
        );
    }
    json!({
        "event_count": st.event_count,
        "last_event_hash": st.head_hash,
        "records": records,
        "operations": st.operations,
    })
}

/// Persist the derived index + head checkpoint for `st`.
pub fn write_index(store: &Path, st: &State) -> Result<(), String> {
    std::fs::create_dir_all(dir(store))
        .map_err(|e| format!("META_INDEX_WRITE: {e}"))?;
    atomic_write(
        &head_path(store),
        &mt::canonical(&json!({
            "format": "star-xstore-metadata-head/v1",
            "event_count": st.event_count,
            "last_event_hash": st.head_hash,
            "updated_at": mt::now_iso(),
        })),
    )?;
    let sj = state_json(st);
    atomic_write(
        &records_path(store),
        &mt::canonical(&json!({
            "format": "star-xstore-metadata-index/v1",
            "event_count": sj["event_count"],
            "last_event_hash": sj["last_event_hash"],
            "records": sj["records"],
        })),
    )?;
    atomic_write(
        &operations_path(store),
        &mt::canonical(&json!({
            "format": "star-xstore-metadata-operations/v1",
            "event_count": sj["event_count"],
            "operations": sj["operations"],
        })),
    )
}

/// Load the derived index if it is still in step with the log tail
/// (§25 fast path). Returns None when stale/absent/corrupt — callers
/// rebuild from events; a stale index is a cache miss, never an error.
pub fn load_index(store: &Path) -> Option<State> {
    let head: Value =
        serde_json::from_str(&std::fs::read_to_string(head_path(store)).ok()?)
            .ok()?;
    let head_hash = head.get("last_event_hash")?.as_str()?.to_string();
    let head_count = head.get("event_count")?.as_u64()?;
    let (tail_seq, tail_hash) = meta_log::tail_hash(store).ok()?;
    if tail_hash != head_hash || tail_seq != head_count {
        return None; // committed tail moved past the index — rebuild
    }
    let recs: Value = serde_json::from_str(
        &std::fs::read_to_string(records_path(store)).ok()?,
    )
    .ok()?;
    let ops: Value = serde_json::from_str(
        &std::fs::read_to_string(operations_path(store)).ok()?,
    )
    .ok()?;
    let mut st = State {
        event_count: head_count,
        head_hash,
        ..Default::default()
    };
    for (k, r) in recs.get("records")?.as_object()? {
        st.records.insert(
            k.clone(),
            meta_state::Record {
                record_type: r["record_type"].as_str()?.to_string(),
                record_id: r["record_id"].as_str()?.to_string(),
                revision: r["revision"].as_u64()?,
                event_hash: r["event_hash"].as_str()?.to_string(),
                created_at: r["created_at"].as_str().unwrap_or("").into(),
                payload: r["payload"].clone(),
            },
        );
    }
    for (op, v) in ops.get("operations")?.as_object()? {
        st.operations.insert(op.clone(), v.clone());
    }
    Some(st)
}

/// Canonical reload: scan + materialize (the always-correct path; §83).
pub fn load_canonical(store: &Path) -> Result<State, String> {
    let scan = meta_log::scan(store)?;
    meta_state::materialize(&scan)
}

/// Load for a mutation: index fast path when in step, else snapshot-
/// accelerated replay over the verified canonical scan (§83).
pub fn load_state(store: &Path) -> Result<State, String> {
    if let Some(st) = load_index(store) {
        return Ok(st);
    }
    let scan = meta_log::scan(store)?;
    crate::meta_snap::load_accelerated(store, &scan)
}

/// Rebuild the derived index from the canonical log (§22/§96) and
/// return the fresh state. Emits nothing to the event log — the index
/// is a cache, not state.
pub fn rebuild(store: &Path) -> Result<State, String> {
    let st = load_canonical(store)?;
    write_index(store, &st)?;
    Ok(st)
}

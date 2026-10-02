//! meta_snap.rs — MetadataSnapshot (§80-§82).
//!
//! A snapshot is restart acceleration, never authority (§81): the body
//! is a content-addressed object under objects/, the manifest under
//! metadata/snapshots/<body_sha256>.json records the commit point it
//! captures (last_event_hash + event_count), the record count, the
//! index root and the schema identity (§82). Restore replays the event
//! tail past the snapshot and refuses a snapshot whose commit point is
//! not a real position on the canonical chain (§88).

use crate::meta_log::Scan;
use crate::meta_state::{self, State};
use crate::meta_types as mt;
use serde_json::{json, Value};
use std::io::Write;
use std::path::{Path, PathBuf};

fn snaps_dir(store: &Path) -> PathBuf {
    store.join("metadata").join("snapshots")
}
fn manifest_path(store: &Path, sha: &str) -> PathBuf {
    snaps_dir(store).join(format!("{sha}.json"))
}
fn body_path(store: &Path, sha: &str) -> PathBuf {
    store
        .join("objects")
        .join(&sha[..2])
        .join(format!("{sha}.bin"))
}

fn atomic_write(path: &Path, bytes: &[u8]) -> Result<(), String> {
    if let Some(p) = path.parent() {
        std::fs::create_dir_all(p).map_err(|e| format!("SNAP_WRITE: {e}"))?;
    }
    let tmp = path.with_extension("tmp");
    {
        let mut f = std::fs::File::create(&tmp)
            .map_err(|e| format!("SNAP_WRITE: {e}"))?;
        f.write_all(bytes)
            .and_then(|_| f.sync_all())
            .map_err(|e| format!("SNAP_WRITE: {e}"))?;
    }
    std::fs::rename(&tmp, path).map_err(|e| format!("SNAP_WRITE: {e}"))
}

/// Write a snapshot of the current materialized state. Caller holds
/// the writer lease so `st` cannot race a concurrent commit.
pub fn create(store: &Path, st: &State) -> Result<Value, String> {
    let mut recs: Vec<&crate::meta_state::Record> = st.records.values().collect();
    recs.sort_by(|a, b| {
        (a.record_type.as_str(), a.record_id.as_str())
            .cmp(&(b.record_type.as_str(), b.record_id.as_str()))
    });
    let body = mt::canonical(&json!({
        "format": mt::SNAPBODY_FORMAT,
        "event_count": st.event_count,
        "last_event_hash": st.head_hash,
        "records": recs.iter().map(|r| json!({
            "record_type": r.record_type,
            "record_id": r.record_id,
            "revision": r.revision,
            "event_hash": r.event_hash,
            "created_at": r.created_at,
            "payload": r.payload,
        })).collect::<Vec<_>>(),
    }));
    let sha = crate::hash::sha256_hex(body.as_bytes());
    atomic_write(&body_path(store, &sha), body.as_bytes())?;
    let manifest = json!({
        "format": mt::SNAPSHOT_FORMAT,
        "snapshot_sha256": sha,
        "last_event_hash": st.head_hash,
        "event_count": st.event_count,
        "record_count": st.records.len() as u64,
        "index_root": sha.clone(),
        "schema_identity": mt::SCHEMA_IDENTITY,
        "created_at": mt::now_iso(),
    });
    atomic_write(
        &manifest_path(store, &sha),
        mt::canonical(&manifest).as_bytes(),
    )?;
    Ok(json!({
        "format": "xstore-metadata-snapshot-created/v1",
        "ok": true,
        "snapshot_sha256": sha,
        "event_count": st.event_count,
        "record_count": st.records.len(),
    }))
}

/// Latest valid manifest (highest event_count whose body object exists
/// and hashes correctly). Invalid manifests are skipped — a snapshot is
/// a cache, its absence is not corruption.
fn latest_valid(store: &Path) -> Option<(Value, u64, String)> {
    let dir = snaps_dir(store);
    let mut best: Option<(Value, u64, String)> = None;
    for ent in std::fs::read_dir(&dir).ok()?.flatten() {
        let p = ent.path();
        if p.extension().and_then(|e| e.to_str()) != Some("json") {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(&p) else { continue };
        let Ok(m) = serde_json::from_str::<Value>(&text) else { continue };
        if m.get("format").and_then(|x| x.as_str()) != Some(mt::SNAPSHOT_FORMAT) {
            continue;
        }
        let sha = match m.get("snapshot_sha256").and_then(|x| x.as_str()) {
            Some(s) => s.to_string(),
            None => continue,
        };
        if sha.len() != 64 || !sha.bytes().all(|c| c.is_ascii_hexdigit()) { continue; }
        let Some(cnt) = m.get("event_count").and_then(|x| x.as_u64()) else {
            continue;
        };
        let Ok(body) = std::fs::read(body_path(store, &sha)) else { continue };
        if crate::hash::sha256_hex(&body) != sha {
            continue;
        }
        if best.as_ref().map(|(_, c, _)| cnt > *c).unwrap_or(true) {
            best = Some((m, cnt, sha));
        }
    }
    best
}

/// Snapshot-accelerated load (§83): pick the newest snapshot whose
/// commit point exists on the verified chain, materialize its records,
/// then replay the tail. Falls back to full materialize.
pub fn load_accelerated(store: &Path, scan: &Scan) -> Result<State, String> {
    let verification = verify_status(store, scan);
    if verification["invalid_count"].as_u64().unwrap_or(1) != 0 {
        return Err("XSTORE_RECOVERY_REQUIRED: invalid metadata snapshot".into());
    }
    let Some((manifest, snap_count, _sha)) = latest_valid(store) else {
        return meta_state::materialize(scan);
    };
    if snap_count == 0 || snap_count as usize > scan.events.len() {
        return meta_state::materialize(scan);
    }
    let snap_head = manifest["last_event_hash"].as_str().unwrap_or("");
    let at_pos = scan
        .events
        .get((snap_count - 1) as usize)
        .map(|e| e.line_hash.as_str());
    if at_pos != Some(snap_head) {
        return meta_state::materialize(scan);
    }
    let sha = manifest["snapshot_sha256"].as_str().unwrap_or("");
    let body_text = std::fs::read(body_path(store, sha))
        .map_err(|e| format!("SNAP_READ: {e}"))?;
    let body: Value = serde_json::from_slice(&body_text)
        .map_err(|e| format!("SNAP_CORRUPT: {e}"))?;
    if body["format"].as_str() != Some(mt::SNAPBODY_FORMAT) {
        return meta_state::materialize(scan);
    }
    let mut st = State {
        event_count: snap_count,
        head_hash: snap_head.to_string(),
        ..Default::default()
    };
    let Some(rows) = body.get("records").and_then(|x| x.as_array()) else {
        return meta_state::materialize(scan);
    };
    for r in rows {
        let rt = r["record_type"].as_str().unwrap_or("").to_string();
        let id = r["record_id"].as_str().unwrap_or("").to_string();
        st.records.insert(
            meta_state::key(&rt, &id),
            meta_state::Record {
                record_type: rt,
                record_id: id,
                revision: r["revision"].as_u64().unwrap_or(0),
                event_hash: r["event_hash"].as_str().unwrap_or("").into(),
                created_at: r["created_at"].as_str().unwrap_or("").into(),
                payload: r["payload"].clone(),
            },
        );
    }
    // operations map rebuilds from the tail only — idempotency checks
    // for pre-snapshot ops consult the persisted operations index.
    if let Some(idx) = crate::meta_index::load_index(store) {
        st.operations = idx.operations;
    }
    meta_state::replay_into(&mut st, scan, snap_count)?;
    Ok(st)
}

/// Verify manifests against both their content-addressed objects and the
/// canonical event prefix. A self-consistent forged snapshot is not evidence.
pub fn verify_status(store: &Path, scan: &Scan) -> Value {
    let mut checked = 0u64;
    let mut failures = Vec::new();
    let entries = match std::fs::read_dir(snaps_dir(store)) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound =>
            return json!({"verified": false, "snapshot_count": 0, "invalid_count": 0, "failures": []}),
        Err(error) => return json!({"verified": false, "snapshot_count": 0, "invalid_count": 1, "failures": [error.to_string()]}),
    };
    for entry in entries {
        let check = (|| -> Result<(), String> {
            let path = entry.map_err(|e| e.to_string())?.path();
            if path.extension().and_then(|e| e.to_str()) != Some("json") { return Ok(()); }
            checked += 1;
            let manifest: Value = serde_json::from_slice(&std::fs::read(&path).map_err(|e| e.to_string())?)
                .map_err(|e| e.to_string())?;
            let sha = manifest["snapshot_sha256"].as_str().ok_or("SNAP_HASH_MISSING")?;
            if sha.len() != 64 || !sha.bytes().all(|c| c.is_ascii_hexdigit()) { return Err("SNAP_HASH_INVALID".into()); }
            if manifest["format"] != mt::SNAPSHOT_FORMAT || manifest["schema_identity"] != mt::SCHEMA_IDENTITY
                || path.file_stem().and_then(|s| s.to_str()) != Some(sha) { return Err("SNAP_MANIFEST_INVALID".into()); }
            let bytes = std::fs::read(body_path(store, sha)).map_err(|e| e.to_string())?;
            if crate::hash::sha256_hex(&bytes) != sha { return Err("SNAP_HASH_MISMATCH".into()); }
            let body: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
            let count = manifest["event_count"].as_u64().ok_or("SNAP_COUNT_MISSING")?;
            if count > scan.events.len() as u64 { return Err("SNAP_PREFIX_MISSING".into()); }
            let head = if count == 0 { mt::GENESIS } else { &scan.events[count as usize - 1].line_hash };
            if manifest["last_event_hash"] != head || body["last_event_hash"] != head
                || body["event_count"] != count || body["format"] != mt::SNAPBODY_FORMAT { return Err("SNAP_PREFIX_MISMATCH".into()); }
            let prefix = Scan { events: scan.events[..count as usize].iter().map(|e| crate::meta_log::LogEvent {
                seq: e.seq, line_hash: e.line_hash.clone(), value: e.value.clone(),
            }).collect(), head_hash: head.into(), ignored_tail_bytes: 0, committed_len: 0 };
            let state = meta_state::materialize(&prefix)?;
            let records: Vec<Value> = state.records.values().map(|r| json!({
                "record_type": r.record_type, "record_id": r.record_id,
                "revision": r.revision, "event_hash": r.event_hash,
                "created_at": r.created_at, "payload": r.payload,
            })).collect();
            if body["records"] != json!(records) || manifest["record_count"] != records.len() as u64 {
                return Err("SNAP_STATE_MISMATCH".into());
            }
            Ok(())
        })();
        if let Err(error) = check { failures.push(error); }
    }
    json!({"verified": checked > 0 && failures.is_empty(), "snapshot_count": checked,
        "invalid_count": failures.len(), "failures": failures})
}

/// Report manifests for metadata-verify/status.
pub fn status(store: &Path) -> Value {
    let dir = snaps_dir(store);
    let mut manifests = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&dir) {
        for ent in rd.flatten() {
            if ent.path().extension().and_then(|e| e.to_str()) == Some("json") {
                if let Ok(m) = serde_json::from_str::<Value>(
                    &std::fs::read_to_string(ent.path()).unwrap_or_default(),
                ) {
                    manifests.push(json!({
                        "snapshot_sha256": m["snapshot_sha256"],
                        "event_count": m["event_count"],
                        "record_count": m["record_count"],
                    }));
                }
            }
        }
    }
    json!({"snapshot_count": manifests.len(), "snapshots": manifests})
}

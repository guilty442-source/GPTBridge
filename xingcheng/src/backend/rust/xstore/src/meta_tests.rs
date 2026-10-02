//! meta_tests.rs — metadata plane behavioural tests (crash matrix §94
//! subset + concurrency + parity semantics).
#![cfg(test)]

use crate::meta_api as api;
use crate::meta_index as idx;
use crate::meta_log as log;
use crate::meta_state as st;
use crate::meta_tx as tx;
use serde_json::json;
use std::collections::HashMap;
use std::io::Write;
use std::path::PathBuf;

fn scratch() -> PathBuf {
    let dir = std::env::temp_dir().join(crate::meta_types::new_id("xmeta-t"));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn snapshot_file(root: &std::path::Path) -> (PathBuf, String) {
    let f = root.join("snap.bin");
    std::fs::write(&f, b"snapshot-bytes").unwrap();
    let sha = crate::hash::sha256_hex(b"snapshot-bytes");
    (f, sha)
}

fn dataset_params(snap: &str, sha: &str) -> serde_json::Value {
    json!({
        "content_sha256": "a".repeat(64),
        "snapshot_path": snap,
        "snapshot_sha256": sha,
        "source_manifest_json": "{\"format\":\"star-transformer-sft/v1\"}",
        "examples": [
            {"split":"train","owner_model_id":"m","database_scope":"main",
             "source_example_id":"e1","source_revision":1,
             "content_sha256":"b".repeat(64),"source_type":"t","quality_score":0.9},
            {"split":"validation","owner_model_id":"m","database_scope":"main",
             "source_example_id":"e2","source_revision":1,
             "content_sha256":"c".repeat(64),"source_type":"t","quality_score":0.85}
        ]
    })
}

fn put(store: &std::path::Path, op: &str, p: serde_json::Value) -> Result<serde_json::Value, String> {
    tx::mutate(store, op, &p, "test")
}

fn dataset(store: &std::path::Path) -> String {
    let (f, sha) = snapshot_file(store);
    let r = put(store, "create_dataset", dataset_params(
        &f.to_string_lossy(), &sha,
    )).unwrap();
    r["result"]["dataset_id"].as_str().unwrap().to_string()
}

fn job(store: &std::path::Path, ds: &str) -> String {
    let r = put(store, "create_job", json!({
        "dataset_id": ds,
        "configuration_json": "{\"epochs\":1}",
    })).unwrap();
    r["result"]["job_id"].as_str().unwrap().to_string()
}

fn artifact(root: &std::path::Path, bytes: &[u8]) -> PathBuf {
    let f = root.join(crate::meta_types::new_id("art"));
    std::fs::write(&f, bytes).unwrap();
    f
}

#[test]
fn dataset_create_idempotent_and_immutable() {
    let root = scratch();
    let store = root.join("s");
    let ds = dataset(&store);
    let (f, sha) = snapshot_file(&store);
    // same unique key -> no new events
    let before = idx::load_canonical(&store).unwrap().event_count;
    let r = put(&store, "create_dataset", dataset_params(&f.to_string_lossy(), &sha)).unwrap();
    assert_eq!(r["result"]["result"], "existing");
    let after = idx::load_canonical(&store).unwrap().event_count;
    assert_eq!(before, after);
    // duplicate content hash inside examples -> denied
    let mut bad = dataset_params(&f.to_string_lossy(), &"d".repeat(64));
    bad["examples"][1]["content_sha256"] = json!("b".repeat(64));
    assert!(put(&store, "create_dataset", bad).is_err());
    let _ = ds;
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn job_state_machine_fail_closed() {
    let root = scratch();
    let store = root.join("s");
    let ds = dataset(&store);
    let j = job(&store, &ds);
    // illegal: queued -> training
    assert!(put(&store, "transition_job", json!({
        "job_id": j, "to": "training",
    })).unwrap_err().contains("XSTORE_INVALID_TRANSITION"));
    // legal chain
    for to in ["preflight", "training", "validating", "completed"] {
        put(&store, "transition_job", json!({"job_id": j, "to": to})).unwrap();
    }
    // terminal -> no further transition
    assert!(put(&store, "transition_job", json!({
        "job_id": j, "to": "failed",
    })).is_err());
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn claim_serial_lane_and_revision_conflict() {
    let root = scratch();
    let store = root.join("s");
    let ds = dataset(&store);
    let j1 = job(&store, &ds);
    let j2 = job(&store, &ds);
    let r = put(&store, "claim_job", json!({"job_id": j1})).unwrap();
    assert_eq!(r["result"]["result"], "claimed");
    // second job can't claim while j1 in flight
    let r2 = put(&store, "claim_job", json!({"job_id": j2})).unwrap();
    assert_eq!(r2["result"]["result"], "busy");
    // stale expected_revision rejected
    assert!(put(&store, "transition_job", json!({
        "job_id": j1, "to": "training", "expected_revision": 99,
    })).unwrap_err().contains("XSTORE_REVISION_CONFLICT"));
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn candidate_single_active_and_release_cycle() {
    let root = scratch();
    let store = root.join("s");
    let ds = dataset(&store);
    let j1 = job(&store, &ds);
    let j2 = job(&store, &ds);
    for j in [&j1, &j2] {
        put(&store, "claim_job", json!({"job_id": j})).unwrap();
        // free the lane for j2's claim
        let rec = idx::load_canonical(&store).unwrap();
        let _ = rec;
        put(&store, "transition_job", json!({"job_id": j, "to": "training"})).unwrap();
        put(&store, "transition_job", json!({"job_id": j, "to": "validating"})).unwrap();
        put(&store, "transition_job", json!({"job_id": j, "to": "completed"})).unwrap();
    }
    put(&store, "init_runtime", json!({})).unwrap();
    let a1 = artifact(&store, b"weights-one");
    let a2 = artifact(&store, b"weights-two");
    let c1 = put(&store, "register_candidate", json!({
        "job_id": j1, "artifact_path": a1.to_string_lossy(),
        "metrics_json": "{}",
    })).unwrap()["result"]["adapter_id"].as_str().unwrap().to_string();
    // artifact hash unique
    assert!(put(&store, "register_candidate", json!({
        "job_id": j2, "artifact_path": a1.to_string_lossy(),
        "metrics_json": "{}",
    })).is_err());
    let c2 = put(&store, "register_candidate", json!({
        "job_id": j2, "artifact_path": a2.to_string_lossy(),
        "metrics_json": "{}",
    })).unwrap()["result"]["adapter_id"].as_str().unwrap().to_string();
    for c in [&c1, &c2] {
        put(&store, "record_evaluation", json!({
            "adapter_id": c, "suite_id": "s1",
            "baseline_metrics_json": "{}", "adapter_metrics_json": "{}",
            "comparison_json": "{}", "quality_gates_json": "{}",
            "passed": true,
        })).unwrap();
        put(&store, "release", json!({
            "adapter_id": c, "action": "stage",
            "governed_by": "t", "reason": "t",
        })).unwrap();
        put(&store, "release", json!({
            "adapter_id": c, "action": "activate",
            "governed_by": "t", "reason": "t",
        })).unwrap();
    }
    // exactly one active
    let stt = idx::load_canonical(&store).unwrap();
    let act = stt.list(crate::meta_types::RT_CANDIDATE)
        .filter(|r| r.payload["status"] == "active").count();
    assert_eq!(act, 1);
    let rt = stt.get(crate::meta_types::RT_RUNTIME_STATE, "1").unwrap();
    assert_eq!(rt.payload["active_adapter_id"], c2);
    assert_eq!(rt.payload["previous_adapter_id"], c1);
    assert_eq!(rt.payload["automatic_weight_replacement"], false);
    // rollback flips back
    put(&store, "release", json!({
        "adapter_id": c2, "action": "rollback",
        "governed_by": "t", "reason": "t",
    })).unwrap();
    let stt = idx::load_canonical(&store).unwrap();
    assert_eq!(
        stt.get(crate::meta_types::RT_RUNTIME_STATE, "1").unwrap()
            .payload["active_adapter_id"], c1);
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn idempotent_operation_and_torn_tail() {
    let root = scratch();
    let store = root.join("s");
    let p = json!({"operation_id":"op-1","event_type":"x","entity_type":"t",
                   "entity_id":"e","payload":{}});
    put(&store, "audit", p.clone()).unwrap();
    let n1 = idx::load_canonical(&store).unwrap().event_count;
    // resend same operation_id -> duplicate, no new events
    let r = put(&store, "audit", p).unwrap();
    assert_eq!(r["result"], "duplicate");
    let n2 = idx::load_canonical(&store).unwrap().event_count;
    assert_eq!(n1, n2);
    // torn tail: partial line appended by a crashed writer
    let mut f = std::fs::OpenOptions::new().append(true)
        .open(log::events_path(&store)).unwrap();
    f.write_all(b"{\"format\":\"star-xstore-metadata").unwrap();
    drop(f);
    let sc = log::scan(&store).unwrap();
    assert!(sc.ignored_tail_bytes > 0);
    // next commit trims the uncommitted residue and proceeds
    put(&store, "audit", json!({"operation_id":"op-2","event_type":"y",
        "entity_type":"t","entity_id":"e","payload":{}})).unwrap();
    let sc2 = log::scan(&store).unwrap();
    assert_eq!(sc2.ignored_tail_bytes, 0);
    assert_eq!(sc2.events.len() as u64, n2 + 1);
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn midfile_corruption_requires_recovery() {
    let root = scratch();
    let store = root.join("s");
    put(&store, "audit", json!({"event_type":"a","entity_type":"t",
        "entity_id":"1","payload":{}})).unwrap();
    put(&store, "audit", json!({"event_type":"b","entity_type":"t",
        "entity_id":"2","payload":{}})).unwrap();
    // corrupt the FIRST line, keep a valid tail -> mid-file break
    let p = log::events_path(&store);
    let data = std::fs::read(&p).unwrap();
    let nl = data.iter().position(|b| *b == b'\n').unwrap();
    let mut d2 = data.clone();
    d2[20] = b'X'; // flip a byte in line 1 (not in a hash-critical spot? any byte flips hash)
    let _ = nl;
    std::fs::write(&p, &d2).unwrap();
    assert!(log::scan(&store).unwrap_err().contains("RECOVERY_REQUIRED"));
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn snapshot_roundtrip_and_index_rebuild() {
    let root = scratch();
    let store = root.join("s");
    let ds = dataset(&store);
    let _j = job(&store, &ds);
    let snap = api::cmd_snapshot(&store).unwrap();
    assert_eq!(snap["ok"], true);
    // wipe the derived index -> reads still correct, rebuild restores
    let idir = store.join("metadata").join("index");
    let _ = std::fs::remove_dir_all(&idir);
    let st1 = idx::load_state(&store).unwrap();
    assert!(st1.get(crate::meta_types::RT_DATASET, &ds).is_some());
    let rb = api::cmd_rebuild_index(&store).unwrap();
    assert_eq!(rb["ok"], true);
    assert!(idx::load_index(&store).is_some());
    let v = api::cmd_verify(&store).unwrap();
    assert_eq!(v["ok"], true);
    assert!(v["receipts"]["receipt_count"].as_u64().unwrap() > 0);
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn append_only_and_generic_records() {
    let root = scratch();
    let store = root.join("s");
    put(&store, "put", json!({
        "record_type": "capability_evidence",
        "record": {"record_id": "ce-1", "capability_id": "dialogue"},
    })).unwrap();
    // append-only -> second write denied
    assert!(put(&store, "put", json!({
        "record_type": "capability_evidence",
        "record": {"record_id": "ce-1", "capability_id": "x"},
    })).unwrap_err().contains("IMMUTABLE"));
    // stateful generic -> revision++
    put(&store, "put", json!({
        "record_type": "self_learning_state",
        "record": {"record_id": "sl", "state": "idle"},
    })).unwrap();
    put(&store, "transition", json!({
        "record_type": "self_learning_state", "record_id": "sl",
        "to": "cycling", "expected_revision": 1,
    })).unwrap();
    let st1 = idx::load_canonical(&store).unwrap();
    assert_eq!(st1.get("self_learning_state", "sl").unwrap().revision, 2);
    let _ = std::fs::remove_dir_all(&root);
}

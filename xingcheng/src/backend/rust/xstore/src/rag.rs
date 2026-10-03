//! RAG authority on the existing canonical metadata event engine.
//! Migration compares every source row with the replayed target before sealing.
use crate::{meta_log, meta_state, meta_tx, meta_types as mt};
use serde_json::{json, Value};
use std::{collections::BTreeMap, path::Path};

pub const TYPES: &[&str] = &[
    "rag_generation",
    "rag_chunk",
    "rag_resource",
    "rag_tombstone",
    "rag_index_state",
];
const MARKER: &str = "rag-postgresql-retirement";

fn clean(mut value: Value) -> Value {
    if let Some(obj) = value.as_object_mut() {
        obj.remove("created_at");
        obj.remove("updated_at");
    }
    value
}

fn rows(st: &meta_state::State) -> BTreeMap<String, Value> {
    st.records
        .iter()
        .filter(|(_, r)| TYPES.contains(&r.record_type.as_str()))
        .map(|(key, r)| (key.clone(), clean(r.payload.clone())))
        .collect()
}

fn digest(rows: &BTreeMap<String, Value>) -> String {
    mt::sha256_text(&mt::canonical(&serde_json::to_value(rows).unwrap()))
}

/// Explicit exported source tables, never a silent empty-store initialization.
/// Every type must be present (an empty table is explicit); duplicate keys fail.
pub fn migrate(store: &Path, source: &Value) -> Result<Value, String> {
    let mut expected = BTreeMap::new();
    for rt in TYPES {
        let table = source
            .get(*rt)
            .and_then(Value::as_array)
            .ok_or("RAG_SOURCE_TABLE_MISSING")?;
        for record in table {
            let id = record
                .get("record_id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .ok_or("RAG_SOURCE_ID_MISSING")?;
            if expected
                .insert(format!("{rt}/{id}"), clean(record.clone()))
                .is_some()
            {
                return Err("RAG_SOURCE_DUPLICATE".into());
            }
        }
    }
    let scan = meta_log::scan(store)?;
    let initial = meta_state::materialize(&scan)?;
    if initial.get(mt::RT_MIGRATION, MARKER).is_some() {
        return Err("RAG_MIGRATION_ALREADY_SEALED".into());
    }
    // A failed import may resume only when every existing target row matches
    // this exact source. Foreign or overwritten rows are never adopted.
    if rows(&initial)
        .iter()
        .any(|(key, row)| expected.get(key) != Some(row))
    {
        return Err("RAG_MIGRATION_TARGET_CONFLICT".into());
    }
    for rt in TYPES {
        meta_tx::mutate(
            store,
            "put_many",
            &json!({"operation_id":format!("rag-import-{}-{}",digest(&expected),rt), "record_type":rt, "records":source[*rt]}),
            "rag-native-migration",
        )?;
    }
    let current = meta_state::materialize(&meta_log::scan(store)?)?;
    let actual = rows(&current);
    if actual != expected {
        return Err("RAG_MIGRATION_PARITY_FAILED".into());
    }
    let marker = json!({"record_id":MARKER, "status":"VERIFIED", "row_count":actual.len(),
        "source_sha256":digest(&expected), "target_sha256":digest(&actual), "source":"explicit-postgresql-export"});
    meta_tx::mutate(
        store,
        "put",
        &json!({"record_type":mt::RT_MIGRATION,"record":marker}),
        "rag-native-migration",
    )?;
    Ok(marker)
}

/// One replay supplies all joins and generation reads in one query. No cache trust.
/// Until a governed successor migration is implemented, changed rows fail closed.
pub fn read(store: &Path) -> Result<BTreeMap<String, Value>, String> {
    let st = meta_state::materialize(&meta_log::scan(store)?)?;
    let marker = st
        .get(mt::RT_MIGRATION, MARKER)
        .ok_or("RAG_MIGRATION_REQUIRED")?;
    let receipt = meta_log::verify_receipts(store)?;
    if receipt["ok"] != true {
        return Err("RAG_AUDIT_INVALID".into());
    }
    let rows = rows(&st);
    let hash = digest(&rows);
    if marker.payload["status"] != "VERIFIED"
        || marker.payload["source_sha256"] != hash
        || marker.payload["target_sha256"] != hash
        || marker.payload["row_count"] != rows.len()
    {
        return Err("RAG_MIGRATION_PARITY_DRIFT".into());
    }
    Ok(rows)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn migration_requires_complete_source_and_replay_parity() {
        let root = std::env::temp_dir().join(mt::new_id("rag-migration-test"));
        assert!(read(&root).unwrap_err().contains("MIGRATION_REQUIRED"));
        assert!(migrate(&root, &json!({})).is_err());
        let mut source = json!({});
        for rt in TYPES {
            source[*rt] = json!([]);
        }
        source["rag_chunk"] = json!([{"record_id":"c", "content":"verified"}]);
        migrate(&root, &source).unwrap();
        assert_eq!(read(&root).unwrap().len(), 1);
        assert!(migrate(&root, &source)
            .unwrap_err()
            .contains("ALREADY_SEALED"));
        meta_tx::mutate(
            &root,
            "put",
            &json!({"record_type":"rag_chunk","record":{"record_id":"c","content":"changed"}}),
            "test",
        )
        .unwrap();
        assert!(read(&root).unwrap_err().contains("PARITY_DRIFT"));
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn duplicate_source_and_foreign_target_are_rejected_before_sealing() {
        let root = std::env::temp_dir().join(mt::new_id("rag-conflict-test"));
        let mut source = json!({});
        for rt in TYPES {
            source[*rt] = json!([]);
        }
        source["rag_chunk"] = json!([{"record_id":"a"},{"record_id":"a"}]);
        assert!(migrate(&root, &source).unwrap_err().contains("DUPLICATE"));
        source["rag_chunk"] = json!([{"record_id":"a","content":"source"}]);
        meta_tx::mutate(
            &root,
            "put",
            &json!({"record_type":"rag_chunk","record":{"record_id":"a","content":"foreign"}}),
            "test",
        )
        .unwrap();
        assert!(migrate(&root, &source)
            .unwrap_err()
            .contains("TARGET_CONFLICT"));
        assert!(read(&root).unwrap_err().contains("MIGRATION_REQUIRED"));
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn matching_partial_import_resumes_and_corrupt_events_fail_closed() {
        let root = std::env::temp_dir().join(mt::new_id("rag-resume-test"));
        let mut source = json!({});
        for rt in TYPES {
            source[*rt] = json!([]);
        }
        source["rag_chunk"] = json!([{"record_id":"a","content":"source"}]);
        meta_tx::mutate(
            &root,
            "put",
            &json!({"record_type":"rag_chunk","record":source["rag_chunk"][0]}),
            "test",
        )
        .unwrap();
        migrate(&root, &source).unwrap();
        assert_eq!(read(&root).unwrap().len(), 1);
        let path = meta_log::events_path(&root);
        let contents = std::fs::read_to_string(&path).unwrap();
        std::fs::write(path, contents.replace("source", "tampered")).unwrap();
        assert!(read(&root).is_err());
        std::fs::remove_dir_all(root).unwrap();
    }
}

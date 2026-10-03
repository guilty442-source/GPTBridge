//! Codex authority on the existing canonical metadata event engine —
//! the PostgreSQL-retirement store domain. A pinned
//! `gptbridge-codex-native-snapshot/v1` export is replayed into `codex_row`
//! records and sealed behind a VERIFIED migration marker; `read` re-verifies
//! marker, row digests and audit receipts before yielding any row.
use crate::{meta_log, meta_state, meta_tx, meta_types as mt};
use serde_json::{json, Value};
use std::{collections::BTreeMap, path::Path};

pub const RT_CODEX_ROW: &str = "codex_row";
pub const SNAPSHOT_FORMAT: &str = "gptbridge-codex-native-snapshot/v1";
const MARKER: &str = "codex-postgresql-retirement";

fn clean(mut value: Value) -> Value {
    if let Some(obj) = value.as_object_mut() {
        obj.remove("created_at");
        obj.remove("updated_at");
    }
    value
}

/// Content-addressed row identity — PostgreSQL rows carry no uniform key
/// and bag semantics permit identical rows, so the occurrence ordinal
/// within the table makes each id unique while staying deterministic
/// (the source export pins row order).
fn row_id(table: &str, row: &Value, occurrence: usize) -> String {
    format!(
        "{table}/{}-{occurrence}",
        mt::sha256_text(&mt::canonical(row))
    )
}

fn record(row_id: &str, table: &str, row: &Value) -> Value {
    json!({"record_id": row_id, "table": table, "row": row})
}

fn rows(st: &meta_state::State) -> BTreeMap<String, Value> {
    st.records
        .iter()
        .filter(|(_, r)| r.record_type == RT_CODEX_ROW)
        .map(|(key, r)| (key.clone(), clean(r.payload.clone())))
        .collect()
}

fn digest(rows: &BTreeMap<String, Value>) -> String {
    mt::sha256_text(&mt::canonical(&serde_json::to_value(rows).unwrap()))
}

fn table_name(name: &str) -> Result<&str, String> {
    if name.is_empty()
        || !name.bytes().all(|c| {
            c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_'
        })
        || !name.bytes().next().unwrap().is_ascii_lowercase()
    {
        return Err("CODEX_SOURCE_TABLE_INVALID".into());
    }
    Ok(name)
}

/// Flatten the pinned snapshot into state keys
/// `codex_row/{table}/{row-sha256}` -> codex_row payloads.
/// payloads. Count pins, format, table names and duplicate rows are
/// checked before any write reaches the log.
fn flatten(source: &Value) -> Result<(String, BTreeMap<String, Value>), String> {
    if source.get("artifact").and_then(Value::as_str) != Some(SNAPSHOT_FORMAT) {
        return Err("CODEX_SNAPSHOT_FORMAT".into());
    }
    let generation = source
        .get("generation")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or("CODEX_SNAPSHOT_GENERATION")?
        .to_string();
    let tables = source
        .get("tables")
        .and_then(Value::as_object)
        .ok_or("CODEX_SNAPSHOT_TABLES")?;
    if source.get("table_count").and_then(Value::as_u64) != Some(tables.len() as u64)
    {
        return Err("CODEX_SNAPSHOT_TABLE_COUNT".into());
    }
    let mut expected = BTreeMap::new();
    for (name, rows) in tables {
        let table = table_name(name)?;
        let list = rows
            .as_array()
            .ok_or("CODEX_SOURCE_TABLE_SHAPE")?;
        let mut occurrences: std::collections::HashMap<String, usize> =
            std::collections::HashMap::new();
        for row in list {
            if !row.is_object() {
                return Err("CODEX_SOURCE_ROW_SHAPE".into());
            }
            let ordinal = {
                let seen = occurrences
                    .entry(mt::sha256_text(&mt::canonical(row)))
                    .or_insert(0);
                let n = *seen;
                *seen += 1;
                n
            };
            let id = row_id(table, row, ordinal);
            if expected
                .insert(
                    format!("{RT_CODEX_ROW}/{id}"),
                    record(&id, table, row),
                )
                .is_some()
            {
                return Err("CODEX_SOURCE_DUPLICATE".into());
            }
        }
    }
    if source.get("row_count").and_then(Value::as_u64)
        != Some(expected.len() as u64)
    {
        return Err("CODEX_SNAPSHOT_ROW_COUNT".into());
    }
    Ok((generation, expected))
}

/// One-time sealed import of a pinned codex snapshot. Every existing
/// target row must match this exact source or the import fails closed;
/// the marker records generation, table registry and both digests.
pub fn migrate(store: &Path, source: &Value) -> Result<Value, String> {
    let (generation, expected) = flatten(source)?;
    let scan = meta_log::scan(store)?;
    let initial = meta_state::materialize(&scan)?;
    if initial.get(mt::RT_MIGRATION, MARKER).is_some() {
        return Err("CODEX_MIGRATION_ALREADY_SEALED".into());
    }
    // A failed import may resume only when every existing target row
    // matches this exact source. Foreign rows are never adopted.
    if rows(&initial)
        .iter()
        .any(|(key, row)| expected.get(key) != Some(row))
    {
        return Err("CODEX_MIGRATION_TARGET_CONFLICT".into());
    }
    let mut tables: Vec<String> = Vec::new();
    for record in expected.values() {
        let table = record["table"].as_str().unwrap_or("").to_string();
        if tables.last() != Some(&table) {
            tables.push(table);
        }
    }
    for table in &tables {
        let records: Vec<Value> = expected
            .values()
            .filter(|record| record["table"].as_str() == Some(table))
            .cloned()
            .collect();
        meta_tx::mutate(
            store,
            "put_many",
            &json!({"operation_id":format!("codex-import-{}-{}",digest(&expected),table),
                    "record_type":RT_CODEX_ROW, "records":records}),
            "codex-native-migration",
        )?;
    }
    let current = meta_state::materialize(&meta_log::scan(store)?)?;
    let actual = rows(&current);
    if actual != expected {
        return Err("CODEX_MIGRATION_PARITY_FAILED".into());
    }
    let marker = json!({"record_id":MARKER, "status":"VERIFIED", "domain":"codex",
        "generation":generation, "row_count":actual.len(), "table_count":tables.len(),
        "tables":tables,
        "source_sha256":digest(&expected), "target_sha256":digest(&actual),
        "source":"explicit-postgresql-export"});
    meta_tx::mutate(
        store,
        "put",
        &json!({"record_type":mt::RT_MIGRATION,"record":marker}),
        "codex-native-migration",
    )?;
    Ok(marker)
}

/// Sealed codex replay: marker, receipts, digests and the table registry
/// all re-verified before one row is trusted. Post-seal mutation drifts
/// the digest and fails closed — no successor migration path exists yet.
pub fn read(store: &Path) -> Result<BTreeMap<String, Value>, String> {
    let st = meta_state::materialize(&meta_log::scan(store)?)?;
    let marker = st
        .get(mt::RT_MIGRATION, MARKER)
        .ok_or("CODEX_MIGRATION_REQUIRED")?;
    let receipt = meta_log::verify_receipts(store)?;
    if receipt["ok"] != true {
        return Err("CODEX_AUDIT_INVALID".into());
    }
    let rows = rows(&st);
    let hash = digest(&rows);
    let declared: Vec<String> = marker.payload["tables"]
        .as_array()
        .map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect())
        .unwrap_or_default();
    let table_set: std::collections::BTreeSet<&str> =
        rows.values().filter_map(|r| r["table"].as_str()).collect();
    if marker.payload["status"] != "VERIFIED"
        || marker.payload["source_sha256"] != hash
        || marker.payload["target_sha256"] != hash
        || marker.payload["row_count"] != rows.len()
        || declared.len() != table_set.len()
        || declared.iter().any(|t| !table_set.contains(t.as_str()))
    {
        return Err("CODEX_MIGRATION_PARITY_DRIFT".into());
    }
    Ok(rows)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn snapshot(tables: Value) -> Value {
        let tables = tables.as_object().unwrap().clone();
        let count: u64 = tables
            .values()
            .map(|rows| rows.as_array().unwrap().len() as u64)
            .sum();
        json!({"artifact":SNAPSHOT_FORMAT,"generation":"2026-10-03T00:00:00Z",
               "row_count":count,"table_count":tables.len(),"tables":tables})
    }
    #[test]
    fn migration_requires_complete_snapshot_and_replay_parity() {
        let root = std::env::temp_dir().join(mt::new_id("codex-migration-test"));
        assert!(read(&root).unwrap_err().contains("MIGRATION_REQUIRED"));
        assert!(migrate(&root, &json!({})).is_err());
        let source = snapshot(json!({"articles":[{"provision_id":"A1","rule":"r"}],
                                     "sovereigns":[]}));
        migrate(&root, &source).unwrap();
        assert_eq!(read(&root).unwrap().len(), 1);
        assert!(migrate(&root, &source)
            .unwrap_err()
            .contains("ALREADY_SEALED"));
        meta_tx::mutate(
            &root,
            "put",
            &json!({"record_type":RT_CODEX_ROW,
                    "record":{"record_id":"articles/foreign","table":"articles",
                              "row":{"provision_id":"A2"}}}),
            "test",
        )
        .unwrap();
        assert!(read(&root).unwrap_err().contains("PARITY_DRIFT"));
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn bag_rows_conflicts_and_shape_fail_closed() {
        let root = std::env::temp_dir().join(mt::new_id("codex-conflict-test"));
        // PostgreSQL bag semantics: identical rows are distinct records —
        // occurrence ordinals keep their ids unique and deterministic.
        let bag = snapshot(
            json!({"articles":[{"provision_id":"A1"},{"provision_id":"A1"}]}),
        );
        migrate(&root, &bag).unwrap();
        assert_eq!(read(&root).unwrap().len(), 2);
        std::fs::remove_dir_all(&root).unwrap();
        assert!(migrate(&root, &snapshot(json!({"Bad Table":[]}))).is_err());
        let source = snapshot(json!({"articles":[{"provision_id":"A1"}]}));
        meta_tx::mutate(
            &root,
            "put",
            &json!({"record_type":RT_CODEX_ROW,
                    "record":{"record_id":"articles/foreign","table":"articles",
                              "row":{"provision_id":"OTHER"}}}),
            "test",
        )
        .unwrap();
        assert!(migrate(&root, &source)
            .unwrap_err()
            .contains("TARGET_CONFLICT"));
        std::fs::remove_dir_all(root).unwrap();
    }
}

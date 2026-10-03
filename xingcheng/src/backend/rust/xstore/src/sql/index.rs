//! index.rs — derived column index for native SQL reads
//! (`xstore-sql-index/v1`).
//!
//! DERIVED ONLY (same convention as `meta_index`): the index is built
//! from the caller's row snapshot at `Session` construction, may be
//! rebuilt at any time from the same rows, and is never a source of
//! truth or an authority claim. Every query re-applies the full
//! predicate filter on returned candidates, so an index hit can only
//! narrow the scan — the result is identical to the canonical linear
//! scan by construction.
//!
//! Layout: `table -> column -> value-key -> sorted row keys`.
//! `record_id` is indexed as a pseudo-column (it is registered for
//! every table). Null field values are never indexed — `equal()`
//! rejects null on both sides, so an absent key is a correct miss.
//!
//! Value keys (the `value_key` function) are deliberately
//! over-inclusive, never under-inclusive: every pair of rows/params
//! that `equal()` would accept lands under one key; a superset is
//! safe because the planner re-filters candidates with all
//! predicates.

use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

pub struct Index {
    tables: BTreeMap<String, BTreeMap<String, BTreeMap<String, Vec<String>>>>,
}

/// Normalized equality key. Numbers normalize through `f64` so
/// `-0.0`/`0`/`0.0` share one key (serde_json `Number` equality is
/// variant-exact while `equal()` semantics must stay a superset);
/// other values key on their canonical serialization.
fn value_key(v: &Value) -> Option<String> {
    match v {
        Value::Null => None,
        Value::Number(n) => Some(match n.as_f64() {
            Some(f) if f == 0.0 => "n:0".to_string(),
            Some(f) => format!("n:{f:?}"),
            None => format!("j:{}", crate::meta_types::canonical(v)),
        }),
        _ => Some(format!("j:{}", crate::meta_types::canonical(v))),
    }
}

impl Index {
    /// Derive the column index over a row snapshot (`table/record_id`
    /// keys). Table-agnostic: every top-level field is indexed so new
    /// registered tables/columns need no index migration.
    pub fn build(rows: &BTreeMap<String, Value>) -> Self {
        let mut tables: BTreeMap<String, BTreeMap<String, BTreeMap<String, Vec<String>>>> =
            BTreeMap::new();
        for (key, row) in rows {
            let Some((table, record_id)) = key.split_once('/') else {
                continue;
            };
            let columns = tables.entry(table.to_string()).or_default();
            columns
                .entry("record_id".to_string())
                .or_default()
                .entry(value_key(&Value::from(record_id)).unwrap())
                .or_default()
                .push(key.clone());
            let Some(map) = row.as_object() else {
                continue;
            };
            for (field, value) in map {
                if let Some(k) = value_key(value) {
                    // The record_id pseudo-column already keyed this row;
                    // a same-valued field must not duplicate the key.
                    let bucket = columns
                        .entry(field.clone())
                        .or_default()
                        .entry(k)
                        .or_default();
                    if bucket.last() != Some(key) {
                        bucket.push(key.clone());
                    }
                }
            }
        }
        Self { tables }
    }

    /// Candidate row keys for `table.field = param`.
    /// `Some(&[])` = indexed, provably no match; `None` = the column is
    /// not indexed (no row carries it) and the caller must fall back
    /// to the canonical scan for that predicate.
    pub fn candidates(&self, table: &str, field: &str, param: &Value) -> Option<&[String]> {
        let Some(k) = value_key(param) else {
            return Some(&[]);
        };
        match self
            .tables
            .get(table)?
            .get(field)
            .and_then(|by_value| by_value.get(&k))
        {
            Some(keys) => Some(keys.as_slice()),
            None if self.tables.get(table)?.contains_key(field) => Some(&[]),
            None => None,
        }
    }

    /// Candidate union for `table.field = ANY(params)`; `None` when the
    /// column is not indexed for every element.
    pub fn any_candidates(
        &self,
        table: &str,
        field: &str,
        params: &[Value],
    ) -> Option<BTreeSet<String>> {
        let mut acc = BTreeSet::new();
        for param in params {
            acc.extend(self.candidates(table, field, param)?.iter().cloned());
        }
        Some(acc)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn rows() -> BTreeMap<String, Value> {
        BTreeMap::from([
            (
                "rag_chunk/a".into(),
                json!({"record_id":"a","module_id":"m","sequence":2}),
            ),
            (
                "rag_chunk/b".into(),
                json!({"record_id":"b","module_id":"m","sequence":1}),
            ),
            (
                "rag_chunk/c".into(),
                json!({"record_id":"c","module_id":null,"sequence":0}),
            ),
            ("rag_resource/r".into(), json!({"resource_id":"r"})),
        ])
    }

    #[test]
    fn index_finds_keys_and_reports_absence() {
        let index = Index::build(&rows());
        assert_eq!(
            index.candidates("rag_chunk", "module_id", &json!("m")),
            Some(["rag_chunk/a".to_string(), "rag_chunk/b".to_string()].as_slice())
        );
        assert_eq!(
            index.candidates("rag_chunk", "module_id", &json!("absent")),
            Some([].as_slice())
        );
        assert_eq!(
            index.candidates("rag_chunk", "module_id", &Value::Null),
            Some([].as_slice())
        );
        assert_eq!(
            index.candidates("rag_chunk", "no_such_field", &json!("x")),
            None
        );
        assert_eq!(
            index.candidates("rag_chunk", "record_id", &json!("a")),
            Some(["rag_chunk/a".to_string()].as_slice())
        );
    }

    #[test]
    fn number_keys_unify_representations() {
        let index = Index::build(&BTreeMap::from([(
            "t/x".into(),
            json!({"v": -0.0, "w": 5}),
        )]));
        assert_eq!(
            index.candidates("t", "v", &json!(0.0)).map(|s| s.len()),
            Some(1)
        );
        // 5 vs 5.0 share a normalized key — superset is intentional;
        // the caller's full predicate filter decides equality.
        assert_eq!(
            index.candidates("t", "w", &json!(5.0)).map(|s| s.len()),
            Some(1)
        );
    }

    #[test]
    fn any_unions_and_unindexed_columns_defer() {
        let index = Index::build(&rows());
        let union = index
            .any_candidates("rag_chunk", "module_id", &[json!("m"), json!("z")])
            .unwrap();
        assert_eq!(union.len(), 2);
        assert!(index
            .any_candidates("rag_chunk", "missing", &[json!("m")])
            .is_none());
    }
}

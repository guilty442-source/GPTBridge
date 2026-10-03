//! Native canonical RAG authority. Each request replays one verified snapshot.
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap},
    path::Path,
};

pub struct Authority {
    rows: BTreeMap<String, Value>,
}
fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
impl Authority {
    pub fn open(path: &Path) -> Result<Self, String> {
        Ok(Self {
            rows: xstore::rag::read(path)?,
        })
    }
    fn table<'a>(&'a self, kind: &'a str) -> impl Iterator<Item = &'a Value> {
        self.rows
            .iter()
            .filter(move |(k, _)| k.starts_with(&format!("{kind}/")))
            .map(|(_, v)| v)
    }
    pub fn active_generation(&mut self, alias: &str) -> Result<Option<String>, String> {
        let rows = xstore::sql::Session::query_rows(
            &self.rows,
            "SELECT generation_id FROM rag_generation WHERE alias_name = $1 AND state = $2",
            &[json!(alias), json!("ACTIVE")],
        )?;
        if rows.len() > 1 {
            return Err("RAG_ACTIVE_GENERATION_AMBIGUOUS".into());
        }
        Ok(rows.first().map(|r| text(r, "generation_id").to_owned()))
    }
    fn barrier_row(&self, chunk: &Value) -> Option<Value> {
        let rid = text(chunk, "resource_id");
        let module = text(chunk, "module_id");
        let mut resources = self
            .table("rag_resource")
            .filter(|r| text(r, "resource_id") == rid);
        let resource = resources.next()?;
        if resources.next().is_some()
            || text(resource, "index_status").is_empty()
            || ["tombstoned", "deleted", "purged"].contains(&text(resource, "index_status"))
        {
            return None;
        }
        if self.table("rag_tombstone").any(|t| {
            text(t, "module_id") == module && text(t, "resource_id") == rid && t["purged"] == false
        }) {
            return None;
        }
        let cm = &chunk["metadata"];
        let rm = &resource["metadata"];
        let title = if text(cm, "title").is_empty() {
            text(rm, "title")
        } else {
            text(cm, "title")
        };
        let source = if text(rm, "source").is_empty() {
            text(cm, "source")
        } else {
            text(rm, "source")
        };
        Some(
            json!({"point_id":text(chunk,"vector_point_id"),"chunk_id":text(chunk,"chunk_id"),
            "resource_id":rid,"document_resource_id":rid,"document_id":text(rm,"document_id"),
            "module_id":module,"sequence":chunk["sequence"],"character_start":chunk["character_start"],
            "character_end":chunk["character_end"],"title":title,"source":source,
            "content":text(cm,"content"),"resource_status":text(resource,"index_status")}),
        )
    }
    pub fn chunks_for_points(
        &mut self,
        modules: &[String],
        points: &[String],
    ) -> Result<HashMap<String, Value>, String> {
        let mut result = HashMap::new();
        for chunk in xstore::sql::Session::query_rows(
            &self.rows,
            "SELECT * FROM rag_chunk WHERE module_id = ANY($1) AND vector_point_id = ANY($2)",
            &[json!(modules), json!(points)],
        )? {
            if let Some(row) = self.barrier_row(&chunk) {
                if result.insert(text(&row, "point_id").into(), row).is_some() {
                    return Err("RAG_POINT_AMBIGUOUS".into());
                }
            }
        }
        Ok(result)
    }
    pub fn index_states(
        &mut self,
        module: &str,
        ids: &[String],
    ) -> Result<HashMap<String, String>, String> {
        let mut result = HashMap::new();
        for row in xstore::sql::Session::query_rows(&self.rows,
            "SELECT resource_id, status FROM rag_index_state WHERE module_id = $1 AND resource_id = ANY($2)",
            &[json!(module),json!(ids)])? {
            if result
                .insert(text(&row, "resource_id").into(), text(&row, "status").into())
                .is_some()
            {
                return Err("RAG_INDEX_STATE_AMBIGUOUS".into());
            }
        }
        Ok(result)
    }
    pub fn keyword_search(
        &mut self,
        modules: &[String],
        query: &str,
        limit: usize,
    ) -> Result<Vec<(Value, f64)>, String> {
        // Native lexical ranking v1: exact Unicode alphanumeric terms, conjunctive
        // matching and frequency/length rank. This intentionally replaces PG ts_rank.
        let tokens = |input: &str| -> Vec<String> {
            input
                .split(|c: char| !c.is_alphanumeric())
                .filter(|s| !s.is_empty())
                .map(str::to_lowercase)
                .collect()
        };
        let terms = tokens(query);
        if terms.is_empty() || limit == 0 {
            return Ok(Vec::new());
        }
        let mut result = Vec::new();
        for chunk in self
            .table("rag_chunk")
            .filter(|r| modules.iter().any(|m| m == text(r, "module_id")))
        {
            if let Some(row) = self.barrier_row(chunk) {
                let words = tokens(text(&row, "content"));
                if terms.iter().all(|term| words.contains(term)) {
                    let hits = words.iter().filter(|word| terms.contains(word)).count();
                    let rank = hits as f64 / words.len().max(1) as f64;
                    result.push((row, rank));
                }
            }
        }
        result.sort_by(|a, b| {
            b.1.total_cmp(&a.1)
                .then_with(|| text(&a.0, "chunk_id").cmp(text(&b.0, "chunk_id")))
        });
        result.truncate(limit);
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn deleted_resources_and_tombstones_do_not_become_evidence() {
        let mut rows = BTreeMap::new();
        rows.insert("rag_chunk/c".into(),json!({"module_id":"m","resource_id":"r","vector_point_id":"p","metadata":{"content":"proof"}}));
        rows.insert(
            "rag_resource/r".into(),
            json!({"resource_id":"r","index_status":"indexed","metadata":{}}),
        );
        let mut authority = Authority { rows };
        assert_eq!(
            authority
                .chunks_for_points(&["m".into()], &["p".into()])
                .unwrap()
                .len(),
            1
        );
        authority.rows.insert(
            "rag_tombstone/t".into(),
            json!({"module_id":"m","resource_id":"r","purged":false}),
        );
        assert!(authority
            .chunks_for_points(&["m".into()], &["p".into()])
            .unwrap()
            .is_empty());
        assert!(authority
            .keyword_search(&["m".into()], "proof", 10)
            .unwrap()
            .is_empty());
    }
    #[test]
    fn native_sparse_terms_are_conjunctive_and_rank_is_deterministic() {
        let mut rows = BTreeMap::new();
        rows.insert(
            "rag_resource/r".into(),
            json!({"resource_id":"r","index_status":"indexed"}),
        );
        for (id, content) in [
            ("a", "ALPHA beta"),
            ("b", "alpha beta other other"),
            ("c", "alpha only"),
        ] {
            rows.insert(format!("rag_chunk/{id}"),json!({"chunk_id":id,"module_id":"m","resource_id":"r","metadata":{"content":content}}));
        }
        let mut authority = Authority { rows };
        let matches = authority
            .keyword_search(&["m".into()], "alpha beta", 10)
            .unwrap();
        assert_eq!(matches.len(), 2);
        assert_eq!(matches[0].0["chunk_id"], "a");
        assert!(matches[0].1 > matches[1].1);
    }
}

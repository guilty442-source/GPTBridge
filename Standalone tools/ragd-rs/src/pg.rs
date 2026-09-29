//! PostgreSQL authority queries — ports of
//! `PostgreSQLMetadataAuthority.fetch_chunks_for_points` /
//! `get_index_states` / `get_active_generation`.
//! Fail-closed: any error means zero authority rows, never silent pass.

use std::collections::HashMap;

use postgres::{Client, NoTls};
use serde_json::{Map, Value};

/// Read barrier row — mirrors `_chunk_barrier_row` in
/// `rag_metadata_documents.py` (field names preserved verbatim).
fn chunk_row_to_record(row: &postgres::Row) -> Value {
    let chunk_meta: Option<Value> = row.get(7);
    let resource_meta: Option<Value> = row.get(8);
    let get_str = |meta: &Option<Value>, key: &str| -> String {
        meta.as_ref()
            .and_then(|m| m.get(key))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    };
    let title = {
        let c = get_str(&chunk_meta, "title");
        if c.is_empty() {
            get_str(&resource_meta, "title")
        } else {
            c
        }
    };
    let source = {
        let r = get_str(&resource_meta, "source");
        if r.is_empty() {
            get_str(&chunk_meta, "source")
        } else {
            r
        }
    };
    let resource_id: String = row.get::<_, String>(2);
    let mut map = Map::new();
    map.insert("point_id".into(), json_str(&row.get::<_, String>(0)));
    map.insert("chunk_id".into(), json_str(&row.get::<_, String>(1)));
    map.insert("resource_id".into(), json_str(&resource_id));
    map.insert("document_resource_id".into(), json_str(&resource_id));
    map.insert(
        "document_id".into(),
        json_str(&get_str(&resource_meta, "document_id")),
    );
    map.insert("module_id".into(), json_str(&row.get::<_, String>(3)));
    map.insert("sequence".into(), json_num(row.get::<_, i32>(4)));
    map.insert("character_start".into(), json_num(row.get::<_, i32>(5)));
    map.insert("character_end".into(), json_num(row.get::<_, i32>(6)));
    map.insert("title".into(), json_str(&title));
    map.insert("source".into(), json_str(&source));
    map.insert(
        "content".into(),
        json_str(&get_str(&chunk_meta, "content")),
    );
    map.insert(
        "resource_status".into(),
        json_str(row.get::<_, Option<String>>(9).as_deref().unwrap_or("")),
    );
    Value::Object(map)
}

fn json_str(s: &str) -> Value {
    Value::String(s.to_string())
}
fn json_num(n: i32) -> Value {
    Value::Number(n.into())
}

pub struct Authority {
    client: Client,
}

impl Authority {
    pub fn connect(dsn: &str) -> Result<Self, String> {
        Client::connect(dsn, NoTls)
            .map(|client| Self { client })
            .map_err(|e| e.to_string())
    }

    /// `gptbridge_rag.generation` ACTIVE row for `alias` → generation_id.
    pub fn active_generation(&mut self, alias: &str) -> Result<Option<String>, String> {
        let row = self
            .client
            .query_opt(
                "SELECT generation_id FROM gptbridge_rag.generation \
                 WHERE alias_name = $1 AND state = 'ACTIVE'",
                &[&alias],
            )
            .map_err(|e| e.to_string())?;
        Ok(row.map(|r| r.get::<_, String>(0)))
    }

    /// Canonical read barrier — one batch query proving every candidate
    /// hit: chunk metadata exists, resource not tombstoned/deleted/
    /// purged, no authoritative tombstone row. Keyed by point id.
    pub fn chunks_for_points(
        &mut self,
        module_ids: &[String],
        point_ids: &[String],
    ) -> Result<HashMap<String, Value>, String> {
        let rows = self
            .client
            .query(
                "SELECT chunk.vector_point_id::text, chunk.chunk_id, \
                        chunk.resource_id, chunk.module_id, chunk.sequence, \
                        chunk.character_start, chunk.character_end, \
                        chunk.metadata AS chunk_metadata, \
                        resource.metadata AS resource_metadata, \
                        resource.index_status \
                 FROM gptbridge_rag.chunk AS chunk \
                 JOIN gptbridge_index.resource AS resource \
                   ON resource.resource_id = chunk.resource_id \
                 WHERE chunk.module_id = ANY($1) \
                   AND chunk.vector_point_id::text = ANY($2) \
                   AND resource.index_status NOT IN \
                       ('tombstoned', 'deleted', 'purged') \
                   AND NOT EXISTS ( \
                       SELECT 1 FROM gptbridge_rag.tombstone AS t \
                       WHERE t.module_id = chunk.module_id \
                         AND t.resource_id = chunk.resource_id \
                         AND t.purged = false)",
                &[&module_ids, &point_ids],
            )
            .map_err(|e| e.to_string())?;
        Ok(rows
            .iter()
            .map(|r| (r.get::<_, String>(0), chunk_row_to_record(r)))
            .collect())
    }

    /// Sparse channel — PostgreSQL FTS over canonical chunk content
    /// (`pipeline.py::_keyword_search` counterpart). Returns the same
    /// barrier-row shape plus a `ts_rank` score; callers still apply
    /// the `index_state` proof before a row becomes evidence.
    pub fn keyword_search(
        &mut self,
        module_ids: &[String],
        query: &str,
        limit: usize,
    ) -> Result<Vec<(Value, f64)>, String> {
        let rows = self
            .client
            .query(
                "SELECT chunk.vector_point_id::text, chunk.chunk_id, \
                        chunk.resource_id, chunk.module_id, chunk.sequence, \
                        chunk.character_start, chunk.character_end, \
                        chunk.metadata AS chunk_metadata, \
                        resource.metadata AS resource_metadata, \
                        resource.index_status, \
                        ts_rank( \
                            to_tsvector('simple', COALESCE(chunk.metadata->>'content', '')), \
                            plainto_tsquery('simple', $3)) AS rank \
                 FROM gptbridge_rag.chunk AS chunk \
                 JOIN gptbridge_index.resource AS resource \
                   ON resource.resource_id = chunk.resource_id \
                 WHERE chunk.module_id = ANY($1) \
                   AND resource.index_status NOT IN \
                       ('tombstoned', 'deleted', 'purged') \
                   AND NOT EXISTS ( \
                       SELECT 1 FROM gptbridge_rag.tombstone AS t \
                       WHERE t.module_id = chunk.module_id \
                         AND t.resource_id = chunk.resource_id \
                         AND t.purged = false) \
                   AND to_tsvector('simple', COALESCE(chunk.metadata->>'content', '')) \
                       @@ plainto_tsquery('simple', $3) \
                 ORDER BY rank DESC \
                 LIMIT $2",
                &[&module_ids, &(limit as i64), &query],
            )
            .map_err(|e| e.to_string())?;
        Ok(rows
            .iter()
            .map(|r| {
                let rank: f32 = r.get::<_, f32>(10);
                (chunk_row_to_record(r), rank as f64)
            })
            .collect())
    }

    /// `gptbridge_rag.index_state` status per resource — one round trip
    /// per module (P15 batch shape preserved).
    pub fn index_states(
        &mut self,
        module_id: &str,
        resource_ids: &[String],
    ) -> Result<HashMap<String, String>, String> {
        let rows = self
            .client
            .query(
                "SELECT resource_id, status FROM gptbridge_rag.index_state \
                 WHERE module_id = $1 AND resource_id = ANY($2)",
                &[&module_id, &resource_ids],
            )
            .map_err(|e| e.to_string())?;
        Ok(rows
            .iter()
            .map(|r| (r.get::<_, String>(0), r.get::<_, String>(1)))
            .collect())
    }
}

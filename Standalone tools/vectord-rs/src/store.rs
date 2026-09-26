//! vectord store — derived, rebuildable vector index (codex A610
//! DATA-SAFETY: this engine stores only rebuildable derived data and is
//! never the formal data authority; PostgreSQL owns canonical source data,
//! scope/revision/tombstone resolution happens there after candidate IDs
//! come back from ANN retrieval).

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, RwLock};

use hnsw_rs::prelude::*;
use serde::{Deserialize, Serialize};
use serde_json::Value;

const HNSW_MAX_CONNECTION: usize = 16;
const HNSW_MAX_LAYER: usize = 16;
const HNSW_EF_CONSTRUCTION: usize = 200;
const HNSW_EF_SEARCH: usize = 64;

/// Below this live-point count, queries use an exact brute-force scan.
/// HNSW graph connectivity is probabilistic; for small collections an
/// exact pass is both faster and immune to entry-point/topology misses.
const EXACT_SCAN_THRESHOLD: usize = 4096;

fn cosine_score(a: &[f32], b: &[f32]) -> f32 {
    let mut dot = 0.0_f64;
    let mut na = 0.0_f64;
    let mut nb = 0.0_f64;
    for (x, y) in a.iter().zip(b.iter()) {
        dot += (*x as f64) * (*y as f64);
        na += (*x as f64) * (*x as f64);
        nb += (*y as f64) * (*y as f64);
    }
    if na <= 0.0 || nb <= 0.0 {
        return 0.0;
    }
    (dot / (na.sqrt() * nb.sqrt())) as f32
}

pub const STORE_SCHEMA: &str = "vectord-store/v1";

#[derive(Debug, Serialize, Deserialize)]
pub struct SnapshotPoint {
    pub id: String,
    pub vector: Vec<f32>,
    /// Payload serialized as canonical JSON text — bincode cannot drive
    /// `serde_json::Value` (it requires `deserialize_any`).
    pub payload_json: String,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct CollectionDump {
    pub name: String,
    pub dimension: usize,
    pub points: Vec<SnapshotPoint>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct SnapshotFile {
    pub schema: String,
    pub collections: Vec<CollectionDump>,
    #[serde(default)]
    pub aliases: Vec<(String, String)>,
}

#[derive(Debug, Deserialize, Default, Clone)]
pub struct MatchRule {
    #[serde(default)]
    pub value: Option<Value>,
    #[serde(default)]
    pub any: Option<Vec<Value>>,
}

#[derive(Debug, Deserialize, Clone)]
pub struct Condition {
    pub key: String,
    #[serde(rename = "match")]
    pub rule: MatchRule,
}

#[derive(Debug, Deserialize, Default, Clone)]
pub struct Filter {
    #[serde(default)]
    pub must: Vec<Condition>,
    #[serde(default)]
    pub should: Vec<Condition>,
    #[serde(default)]
    pub must_not: Vec<Condition>,
}

fn json_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        // Cross-compare numeric representations (1 == 1.0).
        (Value::Number(x), Value::Number(y)) => {
            match (x.as_f64(), y.as_f64()) {
                (Some(xf), Some(yf)) => xf == yf,
                _ => x == y,
            }
        }
        _ => a == b,
    }
}

fn condition_ok(payload: &Value, cond: &Condition) -> bool {
    let field = payload.get(&cond.key);
    match (field, &cond.rule.value, &cond.rule.any) {
        (_, Some(want), _) => field.map(|f| json_eq(f, want)).unwrap_or(false),
        (_, _, Some(list)) => field
            .map(|f| list.iter().any(|w| json_eq(f, w)))
            .unwrap_or(false),
        _ => false,
    }
}

pub fn filter_ok(payload: &Value, filter: &Filter) -> bool {
    if filter.must.iter().any(|c| !condition_ok(payload, c)) {
        return false;
    }
    if !filter.should.is_empty()
        && !filter.should.iter().any(|c| condition_ok(payload, c))
    {
        return false;
    }
    if filter.must_not.iter().any(|c| condition_ok(payload, c)) {
        return false;
    }
    true
}

pub struct Collection {
    pub dimension: usize,
    hnsw: RwLock<Hnsw<'static, f32, DistCosine>>,
    // Point ids were stored as three owned String copies (forward key,
    // internal_to_id value, payloads/tombstoned key) — Arc<str> shares one
    // allocation across every index.
    forward: HashMap<Arc<str>, usize>,
    internal_to_id: HashMap<usize, Arc<str>>,
    vectors: HashMap<usize, Vec<f32>>,
    pub payloads: HashMap<Arc<str>, Value>,
    tombstoned: HashSet<Arc<str>>,
    next_internal: usize,
}

impl Collection {
    fn new(dimension: usize, capacity_hint: usize) -> Self {
        Collection {
            dimension,
            hnsw: RwLock::new(Hnsw::new(
                HNSW_MAX_CONNECTION,
                capacity_hint.max(1024),
                HNSW_MAX_LAYER,
                HNSW_EF_CONSTRUCTION,
                DistCosine,
            )),
            forward: HashMap::new(),
            internal_to_id: HashMap::new(),
            vectors: HashMap::new(),
            payloads: HashMap::new(),
            tombstoned: HashSet::new(),
            next_internal: 0,
        }
    }

    pub fn upsert(&mut self, id: &str, vector: Vec<f32>, payload: Value) -> Result<(), String> {
        if vector.len() != self.dimension {
            return Err(format!(
                "DIMENSION_MISMATCH:point={} got={} want={}",
                id,
                vector.len(),
                self.dimension
            ));
        }
        let (arc_id, internal) = match self.forward.get_key_value(id) {
            Some((key, &internal)) => {
                self.tombstoned.remove(id);
                (key.clone(), internal)
            }
            None => {
                let internal = self.next_internal;
                self.next_internal += 1;
                let arc_id: Arc<str> = Arc::from(id);
                self.forward.insert(arc_id.clone(), internal);
                self.internal_to_id.insert(internal, arc_id.clone());
                (arc_id, internal)
            }
        };
        // One allocation per vector: the stored copy is also the HNSW
        // insert operand — previously every upsert paid a second
        // vector.to_vec() just to borrow it.
        self.vectors.insert(internal, vector);
        self.payloads.insert(arc_id, payload);
        let stored = self.vectors.get(&internal).unwrap();
        let hnsw = self.hnsw.read().unwrap();
        hnsw.insert((stored, internal));
        Ok(())
    }

    /// Tombstone the point — the derived index never hard-deletes in place;
    /// a snapshot rebuild drops tombstoned points permanently.
    pub fn delete(&mut self, id: &str) -> bool {
        match self.forward.get_key_value(id) {
            Some((key, _)) => {
                self.tombstoned.insert(key.clone());
                true
            }
            None => false,
        }
    }

    pub fn live_count(&self) -> usize {
        self.forward.len() - self.tombstoned.len()
    }

    pub fn search(
        &self,
        query: &[f32],
        top_k: usize,
        score_threshold: f32,
        filter: &Filter,
    ) -> Result<Vec<(String, f32, Value)>, String> {
        if query.len() != self.dimension {
            return Err(format!(
                "DIMENSION_MISMATCH:query got={} want={}",
                query.len(),
                self.dimension
            ));
        }
        if self.forward.is_empty() {
            return Ok(Vec::new());
        }
        let live = self.live_count().max(1);
        if live <= EXACT_SCAN_THRESHOLD {
            let mut scored: Vec<(String, f32, Value)> = Vec::with_capacity(top_k);
            for (id, internal) in &self.forward {
                if self.tombstoned.contains(&**id) {
                    continue;
                }
                let payload = self.payloads.get(id).cloned().unwrap_or(Value::Null);
                if !filter_ok(&payload, filter) {
                    continue;
                }
                let vector = match self.vectors.get(internal) {
                    Some(v) => v,
                    None => continue,
                };
                let score = cosine_score(query, vector);
                if score < score_threshold {
                    continue;
                }
                scored.push((id.to_string(), score, payload));
            }
            scored.sort_by(|x, y| y.1.total_cmp(&x.1));
            scored.truncate(top_k);
            return Ok(scored);
        }
        // Over-fetch so tombstoned / filtered-out hits cannot starve the
        // caller-visible top_k window.
        let fetch = (top_k.saturating_mul(4) + 64).min(live);
        let hnsw = self.hnsw.read().unwrap();
        let neighbours = hnsw.search(query, fetch, HNSW_EF_SEARCH);
        let mut hits: Vec<(String, f32, Value)> = Vec::with_capacity(top_k);
        for n in neighbours {
            let id = match self.internal_to_id.get(&n.d_id) {
                Some(id) => id.clone(),
                None => continue,
            };
            if self.tombstoned.contains(&*id) {
                continue;
            }
            let score = 1.0 - n.distance;
            if score < score_threshold {
                continue;
            }
            let payload = self.payloads.get(&id).cloned().unwrap_or(Value::Null);
            if !filter_ok(&payload, filter) {
                continue;
            }
            hits.push((id.to_string(), score, payload));
            if hits.len() >= top_k {
                break;
            }
        }
        Ok(hits)
    }

    pub fn count_where(&self, filter: &Filter) -> usize {
        self.forward
            .keys()
            .filter(|id| !self.tombstoned.contains(&**id))
            .filter(|id| {
                self.payloads
                    .get(*id)
                    .map(|p| filter_ok(p, filter))
                    .unwrap_or(false)
            })
            .count()
    }

    pub fn dump(&self) -> Vec<SnapshotPoint> {
        let mut out = Vec::with_capacity(self.live_count());
        for (id, internal) in &self.forward {
            if self.tombstoned.contains(&**id) {
                continue;
            }
            let vector = match self.vectors.get(internal) {
                Some(v) => v.clone(),
                None => continue,
            };
            let payload = self
                .payloads
                .get(id)
                .map(|p| p.to_string())
                .unwrap_or_else(|| "null".to_string());
            out.push(SnapshotPoint {
                id: id.to_string(),
                vector,
                payload_json: payload,
            });
        }
        out
    }

    fn load(dimension: usize, points: Vec<SnapshotPoint>) -> Result<Self, String> {
        let mut collection = Collection::new(dimension, points.len() + 1024);
        for point in points {
            let payload: Value =
                serde_json::from_str(&point.payload_json).unwrap_or(Value::Null);
            collection.upsert(&point.id, point.vector, payload)?;
        }
        Ok(collection)
    }
}

pub struct Store {
    collections: RwLock<HashMap<String, Collection>>,
    aliases: RwLock<HashMap<String, String>>,
    capacity_hint: usize,
}

impl Store {
    pub fn new(capacity_hint: usize) -> Self {
        Store {
            collections: RwLock::new(HashMap::new()),
            aliases: RwLock::new(HashMap::new()),
            capacity_hint,
        }
    }

    fn resolve(&self, name: &str) -> String {
        let aliases = self.aliases.read().unwrap();
        let mut current = name.to_string();
        for _ in 0..8 {
            match aliases.get(&current) {
                Some(next) => current = next.clone(),
                None => break,
            }
        }
        current
    }

    pub fn ensure_collection(&self, name: &str, dimension: usize) -> Result<(), String> {
        let resolved = self.resolve(name);
        let mut collections = self.collections.write().unwrap();
        match collections.get(&resolved) {
            Some(existing) => {
                if existing.dimension != dimension {
                    return Err(format!(
                        "INDEX_MISMATCH:collection={} dimension={} expected={}",
                        resolved, existing.dimension, dimension
                    ));
                }
                Ok(())
            }
            None => {
                if dimension == 0 {
                    return Err("DIMENSION_REQUIRED".to_string());
                }
                collections.insert(
                    resolved.clone(),
                    Collection::new(dimension, self.capacity_hint),
                );
                Ok(())
            }
        }
    }

    pub fn set_alias(&self, alias: &str, collection: &str) {
        self.aliases
            .write()
            .unwrap()
            .insert(alias.to_string(), collection.to_string());
    }

    pub fn get_alias(&self, alias: &str) -> Option<String> {
        self.aliases.read().unwrap().get(alias).cloned()
    }

    pub fn delete_alias(&self, alias: &str) -> bool {
        self.aliases.write().unwrap().remove(alias).is_some()
    }

    pub fn alias_pairs(&self) -> Vec<(String, String)> {
        self.aliases
            .read()
            .unwrap()
            .iter()
            .map(|(a, c)| (a.clone(), c.clone()))
            .collect()
    }

    pub fn collection_names(&self) -> Vec<String> {
        self.collections.read().unwrap().keys().cloned().collect()
    }

    pub fn drop_collection(&self, name: &str) -> bool {
        let resolved = self.resolve(name);
        self.collections.write().unwrap().remove(&resolved).is_some()
    }

    pub fn upsert_points(
        &self,
        collection: &str,
        points: Vec<(String, Vec<f32>, Value)>,
    ) -> Result<usize, String> {
        let resolved = self.resolve(collection);
        let mut collections = self.collections.write().unwrap();
        let coll = collections
            .get_mut(&resolved)
            .ok_or_else(|| format!("COLLECTION_MISSING:{}", resolved))?;
        let count = points.len();
        for (id, vector, payload) in points {
            coll.upsert(&id, vector, payload)?;
        }
        Ok(count)
    }

    pub fn search(
        &self,
        collection: &str,
        query: &[f32],
        top_k: usize,
        score_threshold: f32,
        filter: &Filter,
    ) -> Result<Vec<(String, f32, Value)>, String> {
        let resolved = self.resolve(collection);
        let collections = self.collections.read().unwrap();
        match collections.get(&resolved) {
            Some(coll) => coll.search(query, top_k, score_threshold, filter),
            None => Ok(Vec::new()),
        }
    }

    pub fn delete_where(&self, collection: &str, filter: &Filter) -> Result<usize, String> {
        let resolved = self.resolve(collection);
        let mut collections = self.collections.write().unwrap();
        let coll = collections
            .get_mut(&resolved)
            .ok_or_else(|| format!("COLLECTION_MISSING:{}", resolved))?;
        let doomed: Vec<Arc<str>> = coll
            .payloads
            .iter()
            .filter(|(id, p)| {
                !coll.tombstoned.contains(*id) && filter_ok(p, filter)
            })
            .map(|(id, _)| id.clone())
            .collect();
        let count = doomed.len();
        for id in doomed {
            coll.delete(&id);
        }
        Ok(count)
    }

    pub fn count_where(&self, collection: &str, filter: &Filter) -> Result<usize, String> {
        let resolved = self.resolve(collection);
        let collections = self.collections.read().unwrap();
        match collections.get(&resolved) {
            Some(coll) => Ok(coll.count_where(filter)),
            None => Ok(0),
        }
    }

    pub fn collection_info(&self, name: &str) -> Option<(usize, usize)> {
        let resolved = self.resolve(name);
        let collections = self.collections.read().unwrap();
        collections
            .get(&resolved)
            .map(|c| (c.live_count(), c.dimension))
    }

    pub fn stats(&self) -> (usize, usize) {
        let collections = self.collections.read().unwrap();
        let points = collections.values().map(|c| c.live_count()).sum();
        (collections.len(), points)
    }

    pub fn dump(&self) -> SnapshotFile {
        let collections = self.collections.read().unwrap();
        let aliases = self.aliases.read().unwrap();
        SnapshotFile {
            schema: STORE_SCHEMA.to_string(),
            collections: collections
                .iter()
                .map(|(name, coll)| CollectionDump {
                    name: name.clone(),
                    dimension: coll.dimension,
                    points: coll.dump(),
                })
                .collect(),
            aliases: aliases.iter().map(|(a, c)| (a.clone(), c.clone())).collect(),
        }
    }

    pub fn load(dump: SnapshotFile, capacity_hint: usize) -> Result<Self, String> {
        if dump.schema != STORE_SCHEMA {
            return Err(format!("SNAPSHOT_SCHEMA_MISMATCH:{}", dump.schema));
        }
        let store = Store::new(capacity_hint);
        {
            let mut collections = store.collections.write().unwrap();
            for coll_dump in dump.collections {
                collections.insert(
                    coll_dump.name.clone(),
                    Collection::load(coll_dump.dimension, coll_dump.points)?,
                );
            }
            let mut aliases = store.aliases.write().unwrap();
            for (alias, target) in dump.aliases {
                aliases.insert(alias, target);
            }
        }
        Ok(store)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn store_with_points() -> Store {
        let store = Store::new(1024);
        store.ensure_collection("coll", 4).unwrap();
        let points = vec![
            ("a".to_string(), vec![1.0, 0.0, 0.0, 0.0], json!({"module_id": "m1", "resource_id": "r1"})),
            ("b".to_string(), vec![0.0, 1.0, 0.0, 0.0], json!({"module_id": "m2", "resource_id": "r2"})),
            ("c".to_string(), vec![0.9, 0.1, 0.0, 0.0], json!({"module_id": "m1", "resource_id": "r3"})),
        ];
        store.upsert_points("coll", points).unwrap();
        store
    }

    #[test]
    fn scoped_search_only_returns_scoped_module() {
        let store = store_with_points();
        let filter: Filter = serde_json::from_value(json!({
            "must": [{"key": "module_id", "match": {"any": ["m1"]}}]
        }))
        .unwrap();
        let hits = store.search("coll", &[1.0, 0.0, 0.0, 0.0], 10, 0.0, &filter).unwrap();
        assert_eq!(hits.len(), 2);
        assert!(hits.iter().all(|(id, _, _)| id != "b"));
    }

    #[test]
    fn delete_tombstones_and_verify_zero() {
        let store = store_with_points();
        let filter: Filter = serde_json::from_value(json!({
            "must": [{"key": "module_id", "match": {"value": "m1"}}],
            "should": [
                {"key": "resource_id", "match": {"value": "r1"}},
                {"key": "document_resource_id", "match": {"value": "r1"}}
            ]
        }))
        .unwrap();
        assert_eq!(store.delete_where("coll", &filter).unwrap(), 1);
        assert_eq!(store.count_where("coll", &filter).unwrap(), 0);
        let scope: Filter = serde_json::from_value(json!({
            "must": [{"key": "module_id", "match": {"any": ["m1"]}}]
        }))
        .unwrap();
        let hits = store.search("coll", &[1.0, 0.0, 0.0, 0.0], 10, 0.0, &scope).unwrap();
        assert_eq!(hits.len(), 1);
    }

    #[test]
    fn snapshot_round_trip() {
        let store = store_with_points();
        let dump = store.dump();
        let bytes = bincode::serialize(&dump).unwrap();
        let loaded: SnapshotFile = bincode::deserialize(&bytes).unwrap();
        let store2 = Store::load(loaded, 1024).unwrap();
        let filter = Filter::default();
        let hits = store2.search("coll", &[0.0, 1.0, 0.0, 0.0], 1, 0.0, &filter).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].0, "b");
    }

    #[test]
    fn dimension_mismatch_rejected() {
        let store = Store::new(1024);
        store.ensure_collection("coll", 4).unwrap();
        assert!(store.ensure_collection("coll", 8).is_err());
        let err = store
            .upsert_points("coll", vec![("x".into(), vec![1.0; 8], json!({}))])
            .unwrap_err();
        assert!(err.contains("DIMENSION_MISMATCH"));
    }

    #[test]
    fn exact_match_query_still_returns_other_neighbours() {
        // Reproduce: query identical to a stored vector must not starve the
        // remaining live points out of the top_k window.
        let store = Store::new(1024);
        store.ensure_collection("coll", 8).unwrap();
        store
            .upsert_points(
                "coll",
                vec![
                    ("a".into(), [1.0_f32].into_iter().chain([0.0; 7]).collect::<Vec<f32>>(), json!({"module_id": "m1"})),
                    ("b".into(), [0.9_f32, 0.1].into_iter().chain([0.0; 6]).collect::<Vec<f32>>(), json!({"module_id": "m2"})),
                ],
            )
            .unwrap();
        let hits = store
            .search("coll", &[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], 10, -1.0, &Filter::default())
            .unwrap();
        assert_eq!(hits.len(), 2, "hits: {:?}", hits.iter().map(|h| &h.0).collect::<Vec<_>>());
    }

    #[test]
    fn alias_resolves_to_collection() {
        let store = store_with_points();
        store.set_alias("alias", "coll");
        assert_eq!(store.get_alias("alias").as_deref(), Some("coll"));
        let hits = store
            .search("alias", &[1.0, 0.0, 0.0, 0.0], 5, 0.0, &Filter::default())
            .unwrap();
        assert_eq!(hits.len(), 3);
    }
}

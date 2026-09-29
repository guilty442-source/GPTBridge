//! Vector collection — HNSW index + payload/tombstone maps (B94 split of
//! store.rs).

use super::*;

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
    pub(super) tombstoned: HashSet<Arc<str>>,
    next_internal: usize,
}

impl Collection {
    pub(super) fn new(dimension: usize, capacity_hint: usize) -> Self {
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

    pub(super) fn load(dimension: usize, points: Vec<SnapshotPoint>) -> Result<Self, String> {
        let mut collection = Collection::new(dimension, points.len() + 1024);
        for point in points {
            let payload: Value =
                serde_json::from_str(&point.payload_json).unwrap_or(Value::Null);
            collection.upsert(&point.id, point.vector, payload)?;
        }
        Ok(collection)
    }
}

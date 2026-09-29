//! Store — collection registry + aliases + snapshot IO (B94 split of
//! store.rs).

use super::*;

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

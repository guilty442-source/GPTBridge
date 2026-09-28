//! Canonical read barrier — pure port of
//! `pipeline.py::_apply_read_barrier`.  Every vectord hit must be proved
//! against the PostgreSQL authority before it enters the evidence pool:
//! module scope, generation match, canonical chunk metadata existence +
//! resource_id consistency, index_state in (indexed, active).

use std::collections::{HashMap, HashSet};

use serde_json::{Map, Value};

use crate::vectord::Hit;

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Drops {
    pub unauthorized: usize,
    pub generation_mismatch: usize,
    pub missing_metadata: usize,
    pub tombstoned: usize,
}

fn payload_str(hit: &Hit, key: &str) -> String {
    hit.payload
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

fn round6(score: f64) -> f64 {
    (score * 1_000_000.0).round() / 1_000_000.0
}

pub fn apply(
    hits: &[Hit],
    chunk_rows: &HashMap<String, Value>,
    index_states: &HashMap<(String, String), String>,
    scope: &HashSet<String>,
    active_generation: Option<&str>,
) -> (Vec<Value>, Drops) {
    let mut proved = Vec::with_capacity(hits.len());
    let mut drops = Drops::default();
    for hit in hits {
        let module_id = payload_str(hit, "module_id");
        let document_resource_id = {
            let d = payload_str(hit, "document_resource_id");
            if d.is_empty() {
                payload_str(hit, "resource_id")
            } else {
                d
            }
        };
        if module_id.is_empty() || !scope.contains(&module_id) || document_resource_id.is_empty() {
            drops.unauthorized += 1;
            continue;
        }
        if let Some(gen) = active_generation {
            let hit_gen = payload_str(hit, "generation_id");
            if !hit_gen.is_empty() && hit_gen != gen {
                drops.generation_mismatch += 1;
                continue;
            }
        }
        let row = match chunk_rows.get(&hit.id) {
            Some(r) => r,
            None => {
                drops.missing_metadata += 1;
                continue;
            }
        };
        if row
            .get("resource_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            != document_resource_id
        {
            drops.missing_metadata += 1;
            continue;
        }
        let status = index_states
            .get(&(module_id.clone(), document_resource_id.clone()))
            .map(|s| s.to_ascii_lowercase());
        match status.as_deref() {
            Some("indexed") | Some("active") => {}
            _ => {
                drops.tombstoned += 1;
                continue;
            }
        }
        let mut record = Map::new();
        if let Value::Object(p) = &hit.payload {
            for (k, v) in p {
                record.insert(k.clone(), v.clone());
            }
        }
        if let Value::Object(r) = row {
            for (k, v) in r {
                if k != "point_id" {
                    record.insert(k.clone(), v.clone());
                }
            }
        }
        record.insert("id".into(), Value::String(hit.id.clone()));
        record.insert("point_id".into(), Value::String(hit.id.clone()));
        record.insert("module_id".into(), Value::String(module_id.clone()));
        record.insert("vector_score".into(), json_f64(round6(hit.score)));
        record.insert("score".into(), json_f64(round6(hit.score)));
        record.insert(
            "index_state".into(),
            json_obj(&[(
                "status",
                Value::String(status.unwrap_or_default()),
            )]),
        );
        proved.push(Value::Object(record));
    }
    (proved, drops)
}

fn json_f64(v: f64) -> Value {
    serde_json::Number::from_f64(v)
        .map(Value::Number)
        .unwrap_or(Value::Null)
}
fn json_obj(pairs: &[(&str, Value)]) -> Value {
    Value::Object(
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.clone()))
            .collect(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn hit(id: &str, module: &str, rid: &str, gen: &str, score: f64) -> Hit {
        Hit {
            id: id.into(),
            score,
            payload: json!({
                "module_id": module,
                "document_resource_id": rid,
                "generation_id": gen,
            }),
        }
    }
    fn chunk_row(point: &str, rid: &str) -> Value {
        json!({
            "point_id": point, "chunk_id": "c1", "resource_id": rid,
            "document_resource_id": rid, "document_id": "d1",
            "module_id": "m1", "sequence": 0, "character_start": 0,
            "character_end": 5, "title": "t", "source": "s",
            "content": "hello", "resource_status": "indexed",
        })
    }

    #[test]
    fn proves_scoped_indexed_hit() {
        let hits = vec![hit("p1", "m1", "r1", "g1", 0.52)];
        let chunks = HashMap::from([("p1".to_string(), chunk_row("p1", "r1"))]);
        let states = HashMap::from([(("m1".into(), "r1".into()), "indexed".to_string())]);
        let scope = HashSet::from(["m1".to_string()]);
        let (proved, drops) = apply(&hits, &chunks, &states, &scope, Some("g1"));
        assert_eq!(drops, Drops::default());
        assert_eq!(proved.len(), 1);
        assert_eq!(proved[0]["id"], "p1");
        assert_eq!(proved[0]["score"], 0.52);
        assert_eq!(proved[0]["content"], "hello");
    }

    #[test]
    fn drops_unauthorized_and_tombstoned_and_missing() {
        let hits = vec![
            hit("p1", "m2", "r1", "g1", 0.9),  // out of scope
            hit("p2", "m1", "r1", "g9", 0.8),  // generation mismatch
            hit("p3", "m1", "r1", "g1", 0.7),  // no chunk row
            hit("p4", "m1", "r1", "g1", 0.6),  // chunk row, wrong rid
            hit("p5", "m1", "r5", "g1", 0.5),  // tombstoned state
            hit("p6", "m1", "r6", "", 0.4),    // no generation_id: kept
        ];
        let chunks = HashMap::from([
            ("p2".into(), chunk_row("p2", "r1")),
            ("p4".into(), chunk_row("p4", "rX")),
            ("p5".into(), chunk_row("p5", "r5")),
            ("p6".into(), chunk_row("p6", "r6")),
        ]);
        let states = HashMap::from([
            (("m1".into(), "r5".into()), "tombstoned".to_string()),
            (("m1".into(), "r6".into()), "active".to_string()),
        ]);
        let scope = HashSet::from(["m1".to_string()]);
        let (proved, drops) = apply(&hits, &chunks, &states, &scope, Some("g1"));
        assert_eq!(
            drops,
            Drops {
                unauthorized: 1,
                generation_mismatch: 1,
                missing_metadata: 2,
                tombstoned: 1,
            }
        );
        assert_eq!(proved.len(), 1);
        assert_eq!(proved[0]["id"], "p6");
    }
}

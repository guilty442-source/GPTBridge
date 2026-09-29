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


mod collection;
mod registry;
#[cfg(test)]
mod tests;

pub use collection::Collection;
pub use registry::Store;

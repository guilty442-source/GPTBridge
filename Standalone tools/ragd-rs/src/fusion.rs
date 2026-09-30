//! Two-level evidence fusion — native port of
//! `core_system/rag/orchestration/fusion.py` (A549).
//!
//! Level 1 (channel fusion, inside one architecture):
//!   hybrid : dense + sparse           -> RRF
//!   code   : semantic + symbol + graph -> code composite
//!   memory : semantic + recency + importance + confidence
//!
//! Level 2 (architecture fusion, across lanes):
//!   channel score + authority_bonus * authority rank — canonical
//!   source never loses to a high-similarity memory hit.

use std::collections::{BTreeMap, HashMap};

use crate::evidence::{authority_of, RagArchitecture, RagEvidence};

const RRF_K: f64 = 60.0;

fn by_id(evidence: &[RagEvidence]) -> HashMap<String, RagEvidence> {
    evidence
        .iter()
        .map(|e| (e.evidence_id.clone(), e.clone()))
        .collect()
}

/// `reciprocal_rank_fusion` — pipeline-level RRF for dense+keyword
/// channels inside one lane.
pub fn reciprocal_rank_fusion(lists: &[Vec<RagEvidence>]) -> Vec<RagEvidence> {
    let mut merged: HashMap<String, RagEvidence> = HashMap::new();
    let mut rrf: HashMap<String, f64> = HashMap::new();
    for list in lists {
        for e in list {
            merged.entry(e.evidence_id.clone()).or_insert_with(|| e.clone());
        }
        for (rank, e) in list.iter().enumerate() {
            *rrf.entry(e.evidence_id.clone()).or_insert(0.0) += 1.0 / (RRF_K + rank as f64);
        }
    }
    let mut out: Vec<RagEvidence> = merged
        .into_iter()
        .map(|(eid, mut e)| {
            let score = rrf.get(&eid).copied().unwrap_or(0.0);
            e.provenance.insert(
                "rrf_score".to_string(),
                serde_json::json!(score),
            );
            e
        })
        .collect();
    out.sort_by(|a, b| {
        let sa = a.provenance.get("rrf_score").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let sb = b.provenance.get("rrf_score").and_then(|v| v.as_f64()).unwrap_or(0.0);
        sb.partial_cmp(&sa).unwrap_or(std::cmp::Ordering::Equal)
    });
    out
}

/// `channel_fusion_hybrid` — dense + sparse -> RRF composite stored in
/// `dense_score`.
pub fn channel_fusion_hybrid(dense: &[RagEvidence], sparse: &[RagEvidence]) -> Vec<RagEvidence> {
    let mut merged = by_id(dense);
    for e in sparse {
        merged
            .entry(e.evidence_id.clone())
            .or_insert_with(|| e.clone());
    }
    let mut rrf: HashMap<String, f64> = HashMap::new();
    for (rank, e) in dense.iter().enumerate() {
        *rrf.entry(e.evidence_id.clone()).or_insert(0.0) += 1.0 / (RRF_K + rank as f64);
    }
    for (rank, e) in sparse.iter().enumerate() {
        *rrf.entry(e.evidence_id.clone()).or_insert(0.0) += 1.0 / (RRF_K + rank as f64);
    }
    let mut out: Vec<RagEvidence> = merged
        .into_iter()
        .map(|(eid, mut e)| {
            e.dense_score = rrf.get(&eid).copied().unwrap_or(0.0);
            e
        })
        .collect();
    out.sort_by(|a, b| {
        b.dense_score
            .partial_cmp(&a.dense_score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    out
}

/// `channel_fusion_code` — semantic + symbol + graph -> code composite
/// in `graph_score`. Graph relations get a full rank-based share.
/// Contract surface: the code lane currently fuses dense+sparse with a
/// symbol boost (retriever parity); graph channels join when a native
/// symbol/graph source registers.
#[allow(dead_code)]
pub fn channel_fusion_code(
    semantic: &[RagEvidence],
    symbol: &[RagEvidence],
    graph: &[RagEvidence],
) -> Vec<RagEvidence> {
    let mut merged = by_id(semantic);
    for channel in [symbol, graph] {
        for e in channel {
            merged
                .entry(e.evidence_id.clone())
                .or_insert_with(|| e.clone());
        }
    }
    let mut rrf: HashMap<String, f64> = HashMap::new();
    for channel in [semantic, symbol, graph] {
        for (rank, e) in channel.iter().enumerate() {
            *rrf.entry(e.evidence_id.clone()).or_insert(0.0) += 1.0 / (RRF_K + rank as f64);
        }
    }
    let mut out: Vec<RagEvidence> = merged
        .into_iter()
        .map(|(eid, mut e)| {
            e.graph_score = rrf.get(&eid).copied().unwrap_or(0.0);
            e
        })
        .collect();
    out.sort_by(|a, b| {
        b.graph_score
            .partial_cmp(&a.graph_score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    out
}

/// `memory_score` — semantic + recency + importance + confidence
/// composite; memory's own score before it enters fusion.
pub fn memory_score(semantic: f64, recency: f64, importance: f64, confidence: f64) -> f64 {
    0.40 * semantic + 0.20 * recency + 0.20 * importance + 0.20 * confidence
}

/// `channel_fusion_memory`.
pub fn channel_fusion_memory(semantic: &[RagEvidence]) -> Vec<RagEvidence> {
    let mut out: Vec<RagEvidence> = semantic
        .iter()
        .map(|e| {
            let mut e = e.clone();
            let importance = e
                .provenance
                .get("importance")
                .and_then(|v| v.as_f64())
                .unwrap_or(0.5);
            let confidence = e
                .provenance
                .get("confidence")
                .and_then(|v| v.as_f64())
                .unwrap_or(0.5);
            e.memory_score = memory_score(e.dense_score, e.freshness, importance, confidence);
            e
        })
        .collect();
    out.sort_by(|a, b| {
        b.memory_score
            .partial_cmp(&a.memory_score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    out
}

/// `_channel_score` — best available per-arch composite score.
fn channel_score(e: &RagEvidence) -> f64 {
    match e.rag_type {
        RagArchitecture::Memory => {
            if e.memory_score != 0.0 {
                e.memory_score
            } else {
                e.dense_score
            }
        }
        RagArchitecture::Code => {
            if e.graph_score != 0.0 {
                e.graph_score
            } else {
                e.dense_score
            }
        }
        _ => {
            if e.dense_score != 0.0 {
                e.dense_score
            } else {
                e.reranker_score
            }
        }
    }
}

/// `architecture_fusion` — merge per-architecture pools into one ordered
/// list: channel score + authority_bonus * rank.
pub fn architecture_fusion(
    pools: &BTreeMap<RagArchitecture, Vec<RagEvidence>>,
    authority_bonus: f64,
) -> Vec<RagEvidence> {
    let mut scored: Vec<(f64, RagEvidence)> = Vec::new();
    for pool in pools.values() {
        for e in pool {
            scored.push((
                channel_score(e) + authority_bonus * authority_of(e).rank() as f64,
                e.clone(),
            ));
        }
    }
    scored.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    scored.into_iter().map(|(_, e)| e).collect()
}

/// `mark_conflicts` — flag contradicting evidence instead of dropping
/// it: same `provenance.fact_key`, different `fact_value`.
pub fn mark_conflicts(evidence: &[RagEvidence]) -> Vec<RagEvidence> {
    let mut by_fact: HashMap<String, Vec<usize>> = HashMap::new();
    for (i, e) in evidence.iter().enumerate() {
        if let Some(fk) = e.provenance.get("fact_key").and_then(|v| v.as_str()) {
            if !fk.is_empty() {
                by_fact.entry(fk.to_string()).or_default().push(i);
            }
        }
    }
    let mut conflict: Vec<bool> = vec![false; evidence.len()];
    for indices in by_fact.values() {
        let values: std::collections::HashSet<String> = indices
            .iter()
            .map(|&i| {
                evidence[i]
                    .provenance
                    .get("fact_value")
                    .map(|v| v.to_string())
                    .unwrap_or_default()
            })
            .collect();
        if values.len() > 1 {
            for &i in indices {
                conflict[i] = true;
            }
        }
    }
    evidence
        .iter()
        .enumerate()
        .map(|(i, e)| {
            let mut e = e.clone();
            e.evidence_conflict = conflict[i];
            e
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::evidence::{authority_of, EvidenceKind, SourceAuthority};

    fn ev(id: &str, arch: RagArchitecture, dense: f64) -> RagEvidence {
        RagEvidence {
            evidence_id: id.to_string(),
            rag_type: arch,
            dense_score: dense,
            evidence_kind: EvidenceKind::SourceText,
            ..Default::default()
        }
    }

    #[test]
    fn hybrid_rrf_prefers_items_in_both_channels() {
        let dense = vec![ev("a", RagArchitecture::Hybrid, 0.9), ev("b", RagArchitecture::Hybrid, 0.8)];
        let sparse = vec![ev("b", RagArchitecture::Hybrid, 0.9), ev("c", RagArchitecture::Hybrid, 0.7)];
        let fused = channel_fusion_hybrid(&dense, &sparse);
        assert_eq!(fused[0].evidence_id, "b");
        assert_eq!(fused.len(), 3);
    }

    #[test]
    fn architecture_fusion_honours_authority_rank() {
        let mut canon = ev("canon", RagArchitecture::Hybrid, 1.0);
        canon.provenance.insert("authority".into(), serde_json::json!("canonical-source"));
        let mut mem = ev("mem", RagArchitecture::Memory, 1.0);
        mem.provenance.insert("authority".into(), serde_json::json!("contextual-memory"));
        let mut pools = BTreeMap::new();
        pools.insert(RagArchitecture::Hybrid, vec![canon]);
        pools.insert(RagArchitecture::Memory, vec![mem]);
        let fused = architecture_fusion(&pools, 0.05);
        assert_eq!(fused[0].evidence_id, "canon");
        assert_eq!(authority_of(&fused[0]), SourceAuthority::CanonicalSource);
    }

    #[test]
    fn conflicts_are_flagged_not_dropped() {
        let mut a = ev("a", RagArchitecture::Hybrid, 0.5);
        a.provenance.insert("fact_key".into(), serde_json::json!("k"));
        a.provenance.insert("fact_value".into(), serde_json::json!("v1"));
        let mut b = ev("b", RagArchitecture::Hybrid, 0.4);
        b.provenance.insert("fact_key".into(), serde_json::json!("k"));
        b.provenance.insert("fact_value".into(), serde_json::json!("v2"));
        let c = ev("c", RagArchitecture::Hybrid, 0.3);
        let marked = mark_conflicts(&[a, b, c]);
        assert!(marked[0].evidence_conflict);
        assert!(marked[1].evidence_conflict);
        assert!(!marked[2].evidence_conflict);
    }
}

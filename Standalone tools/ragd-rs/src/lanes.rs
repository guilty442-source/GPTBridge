//! Specialized and autonomous retrieval lanes — the C106
//! retrieval-plane roles beyond the canonical hybrid core.
//!
//!   agentic-rag      : single-agent autonomous retrieval — a bounded
//!                      sufficiency-driven loop (retrieve -> evaluate
//!                      -> reformulate) over the canonical hybrid core;
//!                      rounds are hard-capped by the DAG budget
//!                      envelope (max_rounds <= 3).
//!   multi-agent-rag  : collaborative retrieval — the same query is
//!                      answered by the specialized peer lanes
//!                      concurrently and merged under the same bounded
//!                      DAG.
//!   graph-rag        : specialized governed data path — relation-aware
//!                      rerank over the in-pool candidate graph
//!                      (shared-resource adjacency, chunk-sequence
//!                      neighbours, metadata-declared links).
//!   tag-rag          : specialized governed data path — structured /
//!                      table-shaped canonical content under
//!                      structured-authority.
//!   multimodal-rag   : specialized governed data path — media-bearing
//!                      sources (declared modality or media-file
//!                      source).
//!
//! Every lane consumes only barrier-proved / canonical rows through the
//! existing governed plumbing; no lane creates authority,
//! orchestration or a cache (C106: specialized paths are never
//! independent orchestration or authority planes).

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use serde_json::{json, Value};

use crate::dag;
use crate::evidence::{EvidenceKind, RagArchitecture, RagEvidence, SourceAuthority};
use crate::fusion;
use crate::retrieve::{
    dense_lane, evaluate_sufficiency, extract_symbols, lane_code, lane_hybrid, lane_memory,
    sparse_lane, RetrieveRequest, SufficiencyPolicy,
};
use crate::App;

fn merge_evidence(pool: &mut Vec<RagEvidence>, batch: Vec<RagEvidence>) {
    for e in batch {
        if !pool.iter().any(|x| x.evidence_id == e.evidence_id) {
            pool.push(e);
        }
    }
}

fn aspect_covered(pool: &[RagEvidence], aspect: &str) -> bool {
    pool.iter().any(|e| {
        e.authorized
            && e.provenance
                .get("aspects")
                .and_then(|v| v.as_array())
                .map(|a| a.iter().any(|x| x.as_str() == Some(aspect)))
                .unwrap_or(false)
    })
}

/// Reformulation for the next agentic round: uncovered required
/// aspects are appended to the query; on the first empty coverage the
/// symbol-shaped terms tighten the search instead. An identical query
/// signals no progress and ends the loop.
fn reformulate(
    base: &str,
    pool: &[RagEvidence],
    aspects: &[String],
    round: usize,
) -> String {
    let uncovered: Vec<&str> = aspects
        .iter()
        .filter(|a| !aspect_covered(pool, a))
        .map(|a| a.as_str())
        .collect();
    if !uncovered.is_empty() {
        return format!("{} {}", base, uncovered.join(" ")).trim().to_string();
    }
    if round == 0 {
        let symbols = extract_symbols(base);
        if !symbols.is_empty() {
            return format!("{} {}", base, symbols.join(" "));
        }
    }
    base.to_string()
}

/// Agentic-RAG — single-agent autonomous retrieval. Each round runs
/// the canonical hybrid core, merges into the pool and evaluates
/// sufficiency; RETRIEVE/FIND_AUTHORITY verdicts reformulate and loop,
/// bounded by `budgets.max_rounds` (envelope cap HARD_MAX_ROUNDS).
pub(crate) fn lane_agentic(
    app: &Arc<App>,
    req: &RetrieveRequest,
    active_generation: Option<&str>,
) -> Result<Vec<RagEvidence>, String> {
    let max_rounds = req.budgets.max_rounds.clamp(1, dag::HARD_MAX_ROUNDS);
    let policy = SufficiencyPolicy::default();
    let mut pool: Vec<RagEvidence> = Vec::new();
    let mut query = req.query.clone();
    let mut rounds: Vec<Value> = Vec::new();
    let mut final_verdict = "STOP_INSUFFICIENT".to_string();
    for round in 0..max_rounds {
        let mut sub = req.clone();
        sub.query = query.clone();
        let batch = lane_hybrid(app, &sub, active_generation)?;
        merge_evidence(&mut pool, batch);
        // Remaining-rounds budget: this round counts, so verdicts see
        // the true left-over exactly like the Python loop.
        let report = evaluate_sufficiency(
            &pool,
            &policy,
            &req.required_aspects,
            max_rounds - round,
        );
        let verdict = report
            .get("verdict")
            .and_then(Value::as_str)
            .unwrap_or("STOP_INSUFFICIENT")
            .to_string();
        final_verdict = verdict.clone();
        rounds.push(json!({
            "round": round + 1,
            "query": query,
            "pool": pool.len(),
            "verdict": verdict,
        }));
        if matches!(verdict.as_str(), "SUFFICIENT" | "STOP_INSUFFICIENT") {
            break;
        }
        let next = reformulate(&req.query, &pool, &req.required_aspects, round);
        if next == query {
            break;
        }
        query = next;
    }
    let round_count = rounds.len();
    for e in pool.iter_mut() {
        e.rag_type = RagArchitecture::Agentic;
        e.provenance
            .insert("agentic_rounds".to_string(), json!(round_count));
        e.provenance.insert(
            "agentic_verdict".to_string(),
            json!(final_verdict),
        );
        e.provenance
            .insert("agentic_trace".to_string(), json!(rounds));
    }
    pool.truncate(req.top_k);
    Ok(pool)
}

/// Multi-Agent-RAG — multiple-agent collaborative retrieval under the
/// same bounded DAG: the specialized peer lanes answer the same query
/// concurrently and their pools merge via RRF. Peer evidence keeps its
/// own architecture tag so fusion/diversity stays honest; a peer
/// failure fails the lane closed exactly like dispatch.
pub(crate) fn lane_multi_agent(
    app: &Arc<App>,
    req: &RetrieveRequest,
    active_generation: Option<&str>,
) -> Result<Vec<RagEvidence>, String> {
    const AGENTS: [RagArchitecture; 4] = [
        RagArchitecture::Hybrid,
        RagArchitecture::Code,
        RagArchitecture::Memory,
        RagArchitecture::Graph,
    ];
    fn peer(
        arch: RagArchitecture,
        app: &Arc<App>,
        req: &RetrieveRequest,
        gen: Option<&str>,
    ) -> Result<Vec<RagEvidence>, String> {
        match arch {
            RagArchitecture::Hybrid => lane_hybrid(app, req, gen),
            RagArchitecture::Code => lane_code(app, req, gen),
            RagArchitecture::Memory => lane_memory(app, req, gen),
            _ => lane_graph(app, req, gen),
        }
    }
    let results: Vec<(RagArchitecture, Result<Vec<RagEvidence>, String>)> =
        std::thread::scope(|scope| {
            let handles: Vec<_> = AGENTS
                .iter()
                .map(|a| scope.spawn(move || (*a, peer(*a, app, req, active_generation))))
                .collect();
            handles
                .into_iter()
                .map(|h| {
                    h.join().unwrap_or_else(|_| {
                        (RagArchitecture::Hybrid, Err("agent-panicked".to_string()))
                    })
                })
                .collect()
        });
    let mut lists: Vec<Vec<RagEvidence>> = Vec::new();
    let mut agents: Vec<&str> = Vec::new();
    for (a, r) in results {
        lists.push(r?);
        agents.push(a.as_str());
    }
    let mut fused = fusion::reciprocal_rank_fusion(&lists);
    for e in fused.iter_mut() {
        e.provenance
            .insert("multi_agent_agents".to_string(), json!(agents));
    }
    fused.truncate(req.top_k);
    Ok(fused)
}

/// GraphRAG — specialized governed data path. The candidate pool is a
/// small evidence graph: edges come only from governed rows — shared
/// `resource_id` (the chunk-adjacency graph inside one resource),
/// consecutive `sequence` neighbours, and metadata-declared links
/// (`resource_metadata.relations|links|references`) that resolve to
/// another in-pool resource. `graph_score` is the weighted degree; the
/// composite reranks the pool.
pub(crate) fn lane_graph(
    app: &Arc<App>,
    req: &RetrieveRequest,
    active_generation: Option<&str>,
) -> Result<Vec<RagEvidence>, String> {
    let dense = dense_lane(
        app,
        &req.collection,
        &req.query,
        &req.module_ids,
        req.candidate_limit,
        active_generation,
        RagArchitecture::Graph,
    )?;
    let sparse = sparse_lane(
        app,
        &req.query,
        &req.module_ids,
        req.candidate_limit,
        RagArchitecture::Graph,
    )?;
    let mut fused = fusion::reciprocal_rank_fusion(&[dense, sparse]);

    // In-pool resource set for link resolution + per-resource counts.
    let resources: HashSet<String> = fused
        .iter()
        .map(|e| e.resource_id.clone())
        .filter(|r| !r.is_empty())
        .collect();
    let mut res_count: HashMap<String, usize> = HashMap::new();
    let mut res_seqs: HashMap<String, Vec<i64>> = HashMap::new();
    for e in &fused {
        if e.resource_id.is_empty() {
            continue;
        }
        *res_count.entry(e.resource_id.clone()).or_insert(0) += 1;
        if let Some(s) = e.provenance.get("sequence").and_then(|v| v.as_i64()) {
            res_seqs
                .entry(e.resource_id.clone())
                .or_default()
                .push(s);
        }
    }

    for e in fused.iter_mut() {
        let mut degree = 0usize;
        // Shared-resource edges (siblings in one resource's chunk graph).
        degree += res_count
            .get(e.resource_id.as_str())
            .map(|n| n.saturating_sub(1))
            .unwrap_or(0);
        // Consecutive-sequence neighbours weigh double (true adjacency).
        if let Some(s) = e.provenance.get("sequence").and_then(|v| v.as_i64()) {
            if let Some(seqs) = res_seqs.get(e.resource_id.as_str()) {
                degree += 2 * seqs.iter().filter(|&&t| (t - s).abs() == 1).count();
            }
        }
        // Metadata-declared links resolving to another in-pool resource.
        for key in ["relations", "links", "references"] {
            let linked = e
                .provenance
                .get("resource_metadata")
                .and_then(|m| m.get(key))
                .and_then(|v| v.as_array())
                .map(|a| {
                    a.iter()
                        .filter_map(|x| x.as_str())
                        .any(|t| resources.contains(t) && t != e.resource_id.as_str())
                })
                .unwrap_or(false);
            if linked {
                degree += 2;
            }
        }
        let rrf = e
            .provenance
            .get("rrf_score")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.0);
        e.graph_score = degree as f64;
        e.dense_score = rrf + 0.05 * (degree as f64).min(8.0);
        e.provenance
            .insert("graph_edges".to_string(), json!(degree));
        e.rag_type = RagArchitecture::Graph;
    }
    fused.sort_by(|a, b| {
        b.dense_score
            .partial_cmp(&a.dense_score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    fused.truncate(req.top_k);
    Ok(fused)
}

/// Structured/table content detection for the TAG lane: metadata-declared
/// structured kinds win, then JSON payloads, then delimiter-consistent
/// pipe/tab tables.
fn looks_structured(e: &RagEvidence) -> bool {
    const STRUCTURED_KINDS: [&str; 8] = [
        "table", "structured", "record", "csv", "json", "sql", "row", "grid",
    ];
    for key in ["data_category", "content_type", "kind", "format"] {
        if let Some(v) = e
            .provenance
            .get("resource_metadata")
            .and_then(|m| m.get(key))
            .and_then(|v| v.as_str())
        {
            if STRUCTURED_KINDS.contains(&v.to_lowercase().as_str()) {
                return true;
            }
        }
    }
    let content = e.content.trim();
    if (content.starts_with('{') || content.starts_with('['))
        && serde_json::from_str::<Value>(content).is_ok()
    {
        return true;
    }
    for delimiter in ['|', '\t'] {
        let min_marks = if delimiter == '|' { 2 } else { 1 };
        let arities: Vec<usize> = content
            .lines()
            .take(8)
            .map(|l| l.matches(delimiter).count())
            .filter(|&n| n > 0)
            .collect();
        if arities.len() >= 3 && arities.iter().all(|&n| n >= min_marks) {
            let first = arities[0];
            if arities.iter().filter(|&&n| n == first).count() >= 3 {
                return true;
            }
        }
    }
    false
}

/// TAG — specialized governed data path over structured canonical
/// content. Structured rows are marked `STRUCTURED_DATA` under
/// structured-authority and surfaced first; when no structured row
/// exists the fused pool passes through (same unfiltered-fallback
/// precedent as the memory lane).
pub(crate) fn lane_tag(
    app: &Arc<App>,
    req: &RetrieveRequest,
    active_generation: Option<&str>,
) -> Result<Vec<RagEvidence>, String> {
    let dense = dense_lane(
        app,
        &req.collection,
        &req.query,
        &req.module_ids,
        req.candidate_limit,
        active_generation,
        RagArchitecture::Tag,
    )?;
    let sparse = sparse_lane(
        app,
        &req.query,
        &req.module_ids,
        req.candidate_limit,
        RagArchitecture::Tag,
    )?;
    let fused = fusion::reciprocal_rank_fusion(&[dense, sparse]);
    let mut out: Vec<RagEvidence> = Vec::with_capacity(fused.len());
    let mut rest: Vec<RagEvidence> = Vec::new();
    for mut e in fused {
        e.rag_type = RagArchitecture::Tag;
        if looks_structured(&e) {
            e.evidence_kind = EvidenceKind::StructuredData;
            e.authority = Some(SourceAuthority::StructuredAuthority);
            e.provenance
                .insert("tag_structured".to_string(), Value::Bool(true));
            out.push(e);
        } else {
            rest.push(e);
        }
    }
    if out.is_empty() {
        out = rest;
    }
    out.truncate(req.top_k);
    Ok(out)
}

const MEDIA_EXTENSIONS: [&str; 16] = [
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff", ".mp3", ".wav",
    ".ogg", ".flac", ".mp4", ".webm", ".mov",
];

/// Modality detection: a declared `resource_metadata.modality` /
/// `media_type` wins; otherwise a media-file extension on `source` or
/// `title` identifies the modality.
fn modality_of(e: &RagEvidence) -> Option<String> {
    if let Some(m) = e
        .provenance
        .get("resource_metadata")
        .and_then(|m| m.get("modality").or_else(|| m.get("media_type")))
        .and_then(|v| v.as_str())
    {
        if !m.is_empty() && !m.eq_ignore_ascii_case("text") {
            return Some(m.to_string());
        }
    }
    for key in ["source", "title"] {
        if let Some(s) = e.provenance.get(key).and_then(|v| v.as_str()) {
            let lower = s.to_lowercase();
            if let Some(ext) = MEDIA_EXTENSIONS.iter().find(|x| lower.ends_with(*x)) {
                return Some(ext.trim_start_matches('.').to_string());
            }
        }
    }
    None
}

/// Multimodal-RAG — specialized governed data path over media-bearing
/// sources. Media evidence is marked with its modality; when nothing
/// media-bearing exists the fused pool passes through (same fallback
/// precedent as memory/tag).
pub(crate) fn lane_multimodal(
    app: &Arc<App>,
    req: &RetrieveRequest,
    active_generation: Option<&str>,
) -> Result<Vec<RagEvidence>, String> {
    let dense = dense_lane(
        app,
        &req.collection,
        &req.query,
        &req.module_ids,
        req.candidate_limit,
        active_generation,
        RagArchitecture::Multimodal,
    )?;
    let sparse = sparse_lane(
        app,
        &req.query,
        &req.module_ids,
        req.candidate_limit,
        RagArchitecture::Multimodal,
    )?;
    let fused = fusion::reciprocal_rank_fusion(&[dense, sparse]);
    let mut out: Vec<RagEvidence> = Vec::with_capacity(fused.len());
    let mut rest: Vec<RagEvidence> = Vec::new();
    for mut e in fused {
        e.rag_type = RagArchitecture::Multimodal;
        match modality_of(&e) {
            Some(m) => {
                e.provenance.insert("modality".to_string(), json!(m));
                out.push(e);
            }
            None => rest.push(e),
        }
    }
    if out.is_empty() {
        out = rest;
    }
    out.truncate(req.top_k);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::evidence::EvidenceKind;

    fn ev(id: &str, content: &str) -> RagEvidence {
        RagEvidence {
            evidence_id: id.into(),
            content: content.into(),
            ..Default::default()
        }
    }

    #[test]
    fn structured_detection_covers_json_and_tables() {
        let json_ev = ev("j", "{\"rows\": [1, 2]}");
        assert!(looks_structured(&json_ev));
        let table_ev = ev("t", "a | b | c\n1 | 2 | 3\n4 | 5 | 6");
        assert!(looks_structured(&table_ev));
        let prose = ev("p", "plain prose with no table shape at all");
        assert!(!looks_structured(&prose));
        assert_eq!(EvidenceKind::SourceText, prose.evidence_kind);
    }

    #[test]
    fn modality_detection_uses_source_extension() {
        let mut e = ev("m", "caption");
        e.provenance
            .insert("source".to_string(), json!("diagrams/flow.png"));
        assert_eq!(modality_of(&e).as_deref(), Some("png"));
        let mut t = ev("t", "text");
        t.provenance
            .insert("source".to_string(), json!("docs/readme.md"));
        assert!(modality_of(&t).is_none());
        let mut d = ev("d", "clip");
        d.provenance.insert(
            "resource_metadata".to_string(),
            json!({"modality": "audio"}),
        );
        assert_eq!(modality_of(&d).as_deref(), Some("audio"));
    }

    #[test]
    fn reformulate_appends_uncovered_aspects() {
        let pool: Vec<RagEvidence> = Vec::new();
        let aspects = vec!["latency".to_string(), "cost".to_string()];
        let next = reformulate("system overview", &pool, &aspects, 0);
        assert!(next.contains("latency") && next.contains("cost"));
        let covered = vec![{
            let mut e = ev("c", "x");
            e.provenance
                .insert("aspects".to_string(), json!(["latency", "cost"]));
            e
        }];
        let stable = reformulate("system overview", &covered, &aspects, 1);
        assert_eq!(stable, "system overview");
    }
}

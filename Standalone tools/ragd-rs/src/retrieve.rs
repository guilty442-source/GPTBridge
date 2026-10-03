//! Full retrieval chain — native port of the governed orchestration
//! path (`orchestration/orchestrator.py` + `retrievers/` + DAG
//! `handlers.py` + `service_api.py` CAG facade).
//!
//! `POST /v1/retrieve` runs the bounded DAG RETRIEVAL_CHAIN kind:
//!   retrieval (bounded lane fan-out) -> fusion -> rerank -> context-build
//!
//! Retrieval lanes mirror the Python retrievers:
//!   hybrid : vectord dense + native lexical retrieval -> channel_fusion_hybrid (RRF)
//!   code   : vectord dense + native lexical retrieval -> RRF + symbol boost
//!   memory : vectord dense + native lexical retrieval -> RRF -> scope filter
//!            -> memory_score composite
//!
//! Every dense candidate is proved through the canonical PG read
//! barrier; every FTS row comes straight from canonical tables and is
//! re-checked against `index_state` — vectord never decides authority.
//!
//! The CAG plane wraps the run: a validated L1-L3 cache hit returns the
//! stored payload without executing the DAG; a successful run is
//! cached. The cache gate decides — caches are never canonical.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use crate::barrier;
use crate::cag::{CacheLevel, CacheRequest};
use crate::context::{build_context, validate_citations};
use crate::dag::{
    self, NodeOutcome, RagDagBudgets, RagDagExecutionContext, RagDagExecutor, RagDagKind,
    RagDagNode, RagDagNodeType, RagDagPlanRequest,
};
use crate::evidence::{
    authority_of, evidence_list_from_json, EvidenceKind, RagArchitecture, RagEvidence,
    SourceAuthority,
};
use crate::fusion;
use crate::vectord;
use crate::App;

static REQUEST_SEQ: AtomicU64 = AtomicU64::new(0);

fn request_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("rq-{:x}-{:x}", nanos, REQUEST_SEQ.fetch_add(1, Ordering::SeqCst))
}

// ---------------------------------------------------------------------
// Lane plumbing — dense (vectord + barrier) and sparse (PG FTS)
// ---------------------------------------------------------------------

/// Dense lane: vectord candidates -> canonical barrier proof -> evidence.
fn dense_lane(
    app: &Arc<App>,
    collection: &str,
    query: &str,
    module_ids: &[String],
    limit: usize,
    active_generation: Option<&str>,
    rag_type: RagArchitecture,
) -> Result<Vec<RagEvidence>, String> {
    let hits = vectord::search(
        &app.vectord_base,
        collection,
        Some(query),
        None,
        limit,
        module_ids,
    )?;
    if hits.is_empty() {
        return Ok(Vec::new());
    }
    let scope: HashSet<String> = module_ids.iter().cloned().collect();
    let point_ids: Vec<String> = hits.iter().map(|h| h.id.clone()).collect();
    let mut rids_by_module: HashMap<String, Vec<String>> = HashMap::new();
    for hit in &hits {
        let mid = hit
            .payload
            .get("module_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let rid = hit
            .payload
            .get("document_resource_id")
            .or_else(|| hit.payload.get("resource_id"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if !mid.is_empty() && !rid.is_empty() {
            rids_by_module
                .entry(mid.to_string())
                .or_default()
                .push(rid.to_string());
        }
    }
    let (chunk_rows, index_states) = app.with_authority(|pg| {
        let chunks = pg.chunks_for_points(module_ids, &point_ids)?;
        let mut states = HashMap::new();
        for (mid, rids) in &rids_by_module {
            for (rid, status) in pg.index_states(mid, rids)? {
                states.insert((mid.clone(), rid), status);
            }
        }
        Ok((chunks, states))
    })?;
    let (proved, _drops) =
        barrier::apply(&hits, &chunk_rows, &index_states, &scope, active_generation);
    Ok(proved
        .iter()
        .map(|r| RagEvidence::from_proved_record(r, rag_type))
        .collect())
}

/// Sparse lane: PG full-text search over canonical chunk content.
/// Rows are canonical table reads; `index_state` proof is re-applied
/// before a row becomes evidence.
fn sparse_lane(
    app: &Arc<App>,
    query: &str,
    module_ids: &[String],
    limit: usize,
    rag_type: RagArchitecture,
) -> Result<Vec<RagEvidence>, String> {
    let rows = app.with_authority(|pg| pg.keyword_search(module_ids, query, limit))?;
    if rows.is_empty() {
        return Ok(Vec::new());
    }
    let mut rids_by_module: HashMap<String, Vec<String>> = HashMap::new();
    for (row, _rank) in &rows {
        let mid = row.get("module_id").and_then(Value::as_str).unwrap_or("");
        let rid = row.get("resource_id").and_then(Value::as_str).unwrap_or("");
        if !mid.is_empty() && !rid.is_empty() {
            rids_by_module
                .entry(mid.to_string())
                .or_default()
                .push(rid.to_string());
        }
    }
    let index_states = app.with_authority(|pg| {
        let mut states = HashMap::new();
        for (mid, rids) in &rids_by_module {
            for (rid, status) in pg.index_states(mid, rids)? {
                states.insert((mid.clone(), rid), status);
            }
        }
        Ok(states)
    })?;
    let mut out = Vec::new();
    for (row, rank) in rows {
        let mid = row.get("module_id").and_then(Value::as_str).unwrap_or("");
        let rid = row.get("resource_id").and_then(Value::as_str).unwrap_or("");
        let status = index_states
            .get(&(mid.to_string(), rid.to_string()))
            .map(|s| s.to_ascii_lowercase());
        match status.as_deref() {
            Some("indexed") | Some("active") => {}
            _ => continue,
        }
        let mut e = RagEvidence::from_proved_record(&row, rag_type);
        e.sparse_score = rank;
        e.dense_score = 0.0;
        out.push(e);
    }
    Ok(out)
}

// ---------------------------------------------------------------------
// Retriever lanes (retrievers/hybrid.py, code.py, memory.py)
// ---------------------------------------------------------------------

fn lane_hybrid(
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
        RagArchitecture::Hybrid,
    )?;
    let sparse = sparse_lane(app, &req.query, &req.module_ids, req.candidate_limit, RagArchitecture::Hybrid)?;
    Ok(fusion::channel_fusion_hybrid(&dense, &sparse)
        .into_iter()
        .take(req.top_k)
        .collect())
}

/// `_SYMBOL_PATTERN`/`_IMPORT_PATTERN` — query-symbol extraction for the
/// code lane boost (code.py). Lightweight port: identifier-ish tokens
/// plus import/require/include/using subjects.
fn extract_symbols(query: &str) -> Vec<String> {
    const KEYWORDS: [&str; 8] = [
        "def", "class", "function", "func", "method", "import", "from", "async",
    ];
    let mut symbols: Vec<String> = Vec::new();
    let mut chars = query.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        if c.is_alphabetic() || c == '_' {
            let start = i;
            let mut end = i + c.len_utf8();
            while let Some(&(j, c2)) = chars.peek() {
                if c2.is_alphanumeric() || c2 == '_' || c2 == '.' {
                    end = j + c2.len_utf8();
                    chars.next();
                } else {
                    break;
                }
            }
            let token = &query[start..end];
            let lower = token.to_lowercase();
            if KEYWORDS.contains(&lower.as_str()) {
                continue;
            }
            // Keep identifier-ish tokens only (must contain a letter).
            if token.chars().any(|ch| ch.is_alphabetic())
                && !symbols.iter().any(|s| s == token)
            {
                symbols.push(token.to_string());
            }
        }
    }
    symbols.truncate(16);
    symbols
}

fn lane_code(
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
        RagArchitecture::Code,
    )?;
    let sparse = sparse_lane(app, &req.query, &req.module_ids, req.candidate_limit, RagArchitecture::Code)?;
    let mut fused = fusion::reciprocal_rank_fusion(&[dense, sparse]);
    // Symbol-level boost: candidates mentioning extracted symbols gain
    // +0.1 rrf_score per symbol (code.py).
    let symbols = extract_symbols(&req.query);
    if !symbols.is_empty() {
        for e in fused.iter_mut() {
            let content = e.content.to_lowercase();
            let boost = symbols
                .iter()
                .filter(|s| content.contains(&s.to_lowercase()))
                .count() as f64
                * 0.1;
            if boost > 0.0 {
                let cur = e
                    .provenance
                    .get("rrf_score")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(0.0);
                e.provenance
                    .insert("rrf_score".to_string(), json!(cur + boost));
            }
        }
        fused.sort_by(|a, b| {
            let sa = a.provenance.get("rrf_score").and_then(|v| v.as_f64()).unwrap_or(0.0);
            let sb = b.provenance.get("rrf_score").and_then(|v| v.as_f64()).unwrap_or(0.0);
            sb.partial_cmp(&sa).unwrap_or(std::cmp::Ordering::Equal)
        });
    }
    Ok(fused
        .into_iter()
        .take(req.top_k)
        .map(|mut e| {
            e.evidence_kind = EvidenceKind::CodeSnippet;
            e
        })
        .collect())
}

fn lane_memory(
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
        RagArchitecture::Memory,
    )?;
    let sparse = sparse_lane(app, &req.query, &req.module_ids, req.candidate_limit, RagArchitecture::Memory)?;
    let fused = fusion::reciprocal_rank_fusion(&[dense, sparse]);
    // Scope filter (memory.py): memory_scope/scope tags + session match;
    // unfiltered fallback when no scope tags exist.
    let scopes: HashSet<String> = if req.memory_scopes.is_empty() {
        ["session", "episodic", "long_term"]
            .iter()
            .map(|s| s.to_string())
            .collect()
    } else {
        req.memory_scopes.iter().cloned().collect()
    };
    let mut scoped: Vec<RagEvidence> = fused
        .iter()
        .filter(|e| {
            let scope = e
                .provenance
                .get("memory_scope")
                .or_else(|| e.provenance.get("scope"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if !scope.is_empty() && !scopes.contains(scope) {
                return false;
            }
            if scope == "session" && !req.session_id.is_empty() {
                let cand = e
                    .provenance
                    .get("session_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if !cand.is_empty() && cand != req.session_id {
                    return false;
                }
            }
            true
        })
        .cloned()
        .collect();
    if scoped.is_empty() {
        scoped = fused;
    }
    // Session boost: +0.05 rrf_score for session matches.
    if !req.session_id.is_empty() {
        for e in scoped.iter_mut() {
            if e.provenance.get("session_id").and_then(|v| v.as_str())
                == Some(req.session_id.as_str())
            {
                let cur = e
                    .provenance
                    .get("rrf_score")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(0.0);
                e.provenance
                    .insert("rrf_score".to_string(), json!(cur + 0.05));
            }
        }
        scoped.sort_by(|a, b| {
            let sa = a.provenance.get("rrf_score").and_then(|v| v.as_f64()).unwrap_or(0.0);
            let sb = b.provenance.get("rrf_score").and_then(|v| v.as_f64()).unwrap_or(0.0);
            sb.partial_cmp(&sa).unwrap_or(std::cmp::Ordering::Equal)
        });
    }
    let scored = fusion::channel_fusion_memory(&scoped);
    Ok(scored
        .into_iter()
        .take(req.top_k)
        .map(|mut e| {
            e.evidence_kind = EvidenceKind::Memory;
            e
        })
        .collect())
}

/// `RagOrchestrator.dispatch` — bounded per-architecture fan-out; a
/// single lane stays on the caller thread. Lane failures propagate —
/// the retrieval node fails closed.
fn dispatch(
    app: &Arc<App>,
    archs: &[RagArchitecture],
    req: &RetrieveRequest,
    active_generation: Option<&str>,
) -> Result<BTreeMap<RagArchitecture, Vec<RagEvidence>>, String> {
    fn lane(
        arch: RagArchitecture,
        app: &Arc<App>,
        req: &RetrieveRequest,
        gen: Option<&str>,
    ) -> Result<Vec<RagEvidence>, String> {
        match arch {
            RagArchitecture::Hybrid => lane_hybrid(app, req, gen),
            RagArchitecture::Code => lane_code(app, req, gen),
            RagArchitecture::Memory => lane_memory(app, req, gen),
            RagArchitecture::Agentic => Ok(Vec::new()),
        }
    }
    if archs.len() <= 1 {
        let mut pools = BTreeMap::new();
        for a in archs {
            pools.insert(*a, lane(*a, app, req, active_generation)?);
        }
        return Ok(pools);
    }
    // Bounded fan-out: scoped threads, one per lane.
    let results: Vec<(RagArchitecture, Result<Vec<RagEvidence>, String>)> =
        std::thread::scope(|scope| {
            let handles: Vec<_> = archs
                .iter()
                .map(|a| {
                    scope.spawn(move || (*a, lane(*a, app, req, active_generation)))
                })
                .collect();
            handles.into_iter().map(|h| h.join().unwrap_or_else(|_| {
                (RagArchitecture::Agentic, Err("lane-panicked".to_string()))
            })).collect()
        });
    let mut pools = BTreeMap::new();
    for (a, r) in results {
        pools.insert(a, r?);
    }
    Ok(pools)
}

// ---------------------------------------------------------------------
// Sufficiency (orchestration/sufficiency.py)
// ---------------------------------------------------------------------

struct SufficiencyPolicy {
    min_evidence: usize,
    min_coverage: f64,
    min_diversity: usize,
    min_freshness: f64,
}

impl Default for SufficiencyPolicy {
    fn default() -> Self {
        Self {
            min_evidence: 3,
            min_coverage: 0.5,
            min_diversity: 1,
            min_freshness: 0.3,
        }
    }
}

fn evaluate_sufficiency(
    evidence: &[RagEvidence],
    policy: &SufficiencyPolicy,
    required_aspects: &[String],
    max_rounds: usize,
) -> Value {
    let authorized: Vec<&RagEvidence> = evidence.iter().filter(|e| e.authorized).collect();
    let diversity = authorized
        .iter()
        .map(|e| e.rag_type)
        .collect::<HashSet<_>>()
        .len();
    let contradiction = authorized.iter().any(|e| e.evidence_conflict);
    let best_auth = authorized
        .iter()
        .map(|e| authority_of(e))
        .max_by_key(|a| a.rank())
        .unwrap_or(SourceAuthority::ContextualMemory);
    let freshness = authorized
        .iter()
        .map(|e| e.freshness)
        .fold(1.0f64, f64::min);
    let freshness = if authorized.is_empty() { 0.0 } else { freshness };
    let coverage = if required_aspects.is_empty() {
        if authorized.is_empty() {
            0.0
        } else {
            1.0
        }
    } else {
        let covered = required_aspects
            .iter()
            .filter(|aspect| {
                authorized.iter().any(|e| {
                    e.provenance
                        .get("aspects")
                        .and_then(|v| v.as_array())
                        .map(|a| a.iter().any(|x| x.as_str() == Some(aspect.as_str())))
                        .unwrap_or(false)
                })
            })
            .count();
        covered as f64 / required_aspects.len() as f64
    };
    let mut reasons: Vec<String> = Vec::new();
    if authorized.len() < policy.min_evidence {
        reasons.push(format!("evidence<{}", policy.min_evidence));
    }
    if coverage < policy.min_coverage {
        reasons.push(format!("coverage<{}", policy.min_coverage));
    }
    if diversity < policy.min_diversity {
        reasons.push(format!("diversity<{}", policy.min_diversity));
    }
    if freshness < policy.min_freshness && !authorized.is_empty() {
        reasons.push("stale-evidence".into());
    }
    let budget_left = 1 < max_rounds;
    let needs_authority =
        contradiction && best_auth.rank() < SourceAuthority::CanonicalSource.rank();
    let verdict = if needs_authority {
        reasons.push("contradiction-needs-canonical".into());
        if budget_left {
            "FIND_AUTHORITY"
        } else {
            "STOP_INSUFFICIENT"
        }
    } else if reasons.is_empty() {
        "SUFFICIENT"
    } else if budget_left {
        "RETRIEVE"
    } else {
        "STOP_INSUFFICIENT"
    };
    json!({
        "verdict": verdict,
        "coverage": coverage,
        "source_diversity": diversity,
        "best_authority": best_auth.as_str(),
        "min_freshness": freshness,
        "contradiction": contradiction,
        "evidence_count": authorized.len(),
        "reasons": reasons,
    })
}

// ---------------------------------------------------------------------
// DAG node handlers (handlers.py ports)
// ---------------------------------------------------------------------

fn resolve<'a>(v: &Value, upstream: &'a HashMap<String, Value>) -> Option<Value> {
    if let Some(s) = v.as_str() {
        if let Some((node_id, key)) = s.split_once('.') {
            return upstream
                .get(node_id)
                .and_then(|m| m.get(key))
                .cloned();
        }
    }
    Some(v.clone())
}

fn resolve_many(v: &Value, upstream: &HashMap<String, Value>) -> Vec<Value> {
    let refs = match v {
        Value::Array(a) => a.clone(),
        other => vec![other.clone()],
    };
    let mut out = Vec::new();
    for r in refs {
        match resolve(&r, upstream) {
            Some(Value::Array(items)) => out.extend(items),
            Some(other) if !other.is_null() => out.push(other),
            _ => {}
        }
    }
    out
}

fn evidence_json_list(items: &[RagEvidence]) -> Value {
    Value::Array(
        items
            .iter()
            .map(|e| serde_json::to_value(e).unwrap_or(Value::Null))
            .collect(),
    )
}

fn evidence_map(pairs: &[(&str, Value)]) -> Map<String, Value> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.clone()))
        .collect()
}

fn build_handlers(
    app: &Arc<App>,
    req: &RetrieveRequest,
    active_generation: Option<String>,
) -> HashMap<RagDagNodeType, dag::NodeHandler> {
    let mut handlers: HashMap<RagDagNodeType, dag::NodeHandler> = HashMap::new();

    // RETRIEVAL — bounded lane fan-out via the governed dispatch.
    {
        let app = app.clone();
        let req = req.clone();
        let gen = active_generation.clone();
        handlers.insert(
            RagDagNodeType::Retrieval,
            Arc::new(
                move |node: &RagDagNode,
                      context: &RagDagExecutionContext,
                      _upstream: &HashMap<String, Value>|
                      -> NodeOutcome {
                    if context.cancelled() {
                        return NodeOutcome::cancelled();
                    }
                    let names: Vec<String> = node
                        .inputs
                        .get("rag_types")
                        .or_else(|| node.inputs.get("rag_type"))
                        .and_then(|v| match v {
                            Value::Array(a) => Some(
                                a.iter().filter_map(|x| x.as_str().map(String::from)).collect(),
                            ),
                            Value::String(s) => Some(vec![s.clone()]),
                            _ => None,
                        })
                        .unwrap_or_else(|| vec!["hybrid".to_string()]);
                    let archs: Vec<RagArchitecture> = names
                        .iter()
                        .filter_map(|n| RagArchitecture::from_name(n))
                        .collect();
                    let archs = if archs.is_empty() {
                        vec![RagArchitecture::Hybrid]
                    } else {
                        archs
                    };
                    match dispatch(&app, &archs, &req, gen.as_deref()) {
                        Ok(pools) => {
                            if context.cancelled() {
                                return NodeOutcome::cancelled();
                            }
                            let candidates: Vec<RagEvidence> = pools
                                .values()
                                .flat_map(|p| p.iter().cloned())
                                .collect();
                            NodeOutcome::ok(evidence_map(&[
                                ("candidates", evidence_json_list(&candidates)),
                                (
                                    "provenance",
                                    json!({
                                        "architectures": pools.keys().map(|a| a.as_str()).collect::<Vec<_>>(),
                                        "module_ids": context.module_ids.clone(),
                                    }),
                                ),
                            ]))
                        }
                        Err(e) => {
                            let mut ev = evidence_map(&[
                                ("candidates", Value::Array(vec![])),
                                ("provenance", json!({"architectures": [], "module_ids": context.module_ids.clone()})),
                            ]);
                            ev.insert("error".into(), json!(format!("retrieval:{}", e)));
                            NodeOutcome {
                                ok: false,
                                error: format!("retrieval:{}", e),
                                evidence: ev,
                            }
                        }
                    }
                },
            ),
        );
    }

    // FUSION — architecture_fusion + mark_conflicts.
    handlers.insert(
        RagDagNodeType::Fusion,
        Arc::new(
            move |node: &RagDagNode,
                  context: &RagDagExecutionContext,
                  upstream: &HashMap<String, Value>|
                  -> NodeOutcome {
                if context.cancelled() {
                    return NodeOutcome::cancelled();
                }
                let raw = resolve_many(
                    node.inputs.get("candidate_sets").unwrap_or(&Value::Null),
                    upstream,
                );
                let mut pools: BTreeMap<RagArchitecture, Vec<RagEvidence>> = BTreeMap::new();
                for v in raw {
                    if let Ok(e) = serde_json::from_value::<RagEvidence>(v) {
                        pools.entry(e.rag_type).or_default().push(e);
                    }
                }
                let fused = fusion::mark_conflicts(&fusion::architecture_fusion(&pools, 0.05));
                NodeOutcome::ok(evidence_map(&[
                    ("fused_candidates", evidence_json_list(&fused)),
                    ("fusion_method", json!("architecture_fusion")),
                ]))
            },
        ),
    );

    // RERANK — no external reranker is configured in ragd; the fused
    // order passes through with model "none" (fail-open to fused order,
    // exactly like the Python handler when no reranker is bound).
    handlers.insert(
        RagDagNodeType::Rerank,
        Arc::new(
            move |node: &RagDagNode,
                  context: &RagDagExecutionContext,
                  upstream: &HashMap<String, Value>|
                  -> NodeOutcome {
                if context.cancelled() {
                    return NodeOutcome::cancelled();
                }
                let fused = resolve(
                    node.inputs.get("fused_candidates").unwrap_or(&Value::Null),
                    upstream,
                )
                .unwrap_or(Value::Array(vec![]));
                let limit = node
                    .inputs
                    .get("reranker_limit")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(20)
                    .max(0) as usize;
                let mut ranked = evidence_list_from_json(&fused);
                if limit > 0 {
                    ranked.truncate(limit);
                }
                NodeOutcome::ok(evidence_map(&[
                    ("reranked_candidates", evidence_json_list(&ranked)),
                    ("reranker_model", json!("none")),
                ]))
            },
        ),
    );

    // CONTEXT_BUILD — four-layer context within the token budget.
    {
        let task_instruction = req.task_instruction.clone();
        let query = req.query.clone();
        let max_chars = req.max_context_chars;
        handlers.insert(
            RagDagNodeType::ContextBuild,
            Arc::new(
                move |node: &RagDagNode,
                      context: &RagDagExecutionContext,
                      upstream: &HashMap<String, Value>|
                      -> NodeOutcome {
                    if context.cancelled() {
                        return NodeOutcome::cancelled();
                    }
                    let ranked = resolve(
                        node.inputs
                            .get("reranked_candidates")
                            .unwrap_or(&Value::Null),
                        upstream,
                    )
                    .unwrap_or(Value::Array(vec![]));
                    let ranked = evidence_list_from_json(&ranked);
                    let instruction = if task_instruction.is_empty() {
                        &query
                    } else {
                        &task_instruction
                    };
                    let built = build_context(&ranked, "", instruction, max_chars);
                    let citations: Vec<Value> = built
                        .citations
                        .iter()
                        .map(|c| serde_json::to_value(c).unwrap_or(Value::Null))
                        .collect();
                    let budget = node
                        .inputs
                        .get("max_context_tokens")
                        .and_then(|v| v.as_i64())
                        .unwrap_or(0);
                    NodeOutcome::ok(evidence_map(&[
                        ("context_text", json!(built.text)),
                        ("token_budget", json!(budget)),
                        ("citations", Value::Array(citations)),
                        ("evidence_used", json!(built.evidence_used)),
                    ]))
                },
            ),
        );
    }

    // CACHE_LOOKUP — the store's gated `get`; a miss is a valid
    // negative, never an error.
    {
        let app = app.clone();
        let cache_request = req.cache_request.clone();
        handlers.insert(
            RagDagNodeType::CacheLookup,
            Arc::new(
                move |_node: &RagDagNode,
                      context: &RagDagExecutionContext,
                      _upstream: &HashMap<String, Value>|
                      -> NodeOutcome {
                    if context.cancelled() {
                        return NodeOutcome::cancelled();
                    }
                    let Some(cache_request) = cache_request.clone() else {
                        return NodeOutcome::fail("cache-lookup:no-store");
                    };
                    let mut guard = match app.cache.lock() {
                        Ok(g) => g,
                        Err(_) => return NodeOutcome::fail("cache-lookup:lock"),
                    };
                    let (entry, decision) = guard.get(&cache_request);
                    NodeOutcome::ok(evidence_map(&[
                        ("cache_hit", json!(entry.is_some() && decision.allowed)),
                        ("cache_metadata", decision.to_record()),
                        (
                            "cache_entry",
                            entry
                                .as_ref()
                                .map(|e| e.to_record())
                                .unwrap_or(Value::Null),
                        ),
                        ("cache_decision", decision.to_record()),
                    ]))
                },
            ),
        );
    }

    // CACHE_VALIDATE — map gate checks to the declared evidence keys.
    handlers.insert(
        RagDagNodeType::CacheValidate,
        Arc::new(
            move |_node: &RagDagNode,
                  context: &RagDagExecutionContext,
                  upstream: &HashMap<String, Value>|
                  -> NodeOutcome {
                if context.cancelled() {
                    return NodeOutcome::cancelled();
                }
                let decision = resolve(
                    &Value::String("cache-lookup.cache_decision".into()),
                    upstream,
                )
                .unwrap_or(Value::Null);
                let checks = decision
                    .get("checks")
                    .and_then(|c| c.as_object())
                    .cloned()
                    .unwrap_or_default();
                let allowed = decision
                    .get("allowed")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                let reason = decision
                    .get("reason")
                    .and_then(|v| v.as_str())
                    .unwrap_or("no-decision");
                let mut ev = Map::new();
                for (gate_key, evidence_key) in [
                    ("scope", "scope_check"),
                    ("permission", "permission_check"),
                    ("revision", "revision_check"),
                    ("expiry", "expiry_check"),
                    ("authority", "authority_check"),
                ] {
                    ev.insert(
                        evidence_key.to_string(),
                        Value::Bool(
                            checks.get(gate_key).and_then(|v| v.as_bool()).unwrap_or(false),
                        ),
                    );
                }
                ev.insert(
                    "cache_verdict".into(),
                    json!(if allowed {
                        "allowed".to_string()
                    } else {
                        format!("denied:{}", reason)
                    }),
                );
                NodeOutcome::ok(ev)
            },
        ),
    );

    // MODEL_INFERENCE — ragd owns the retrieval plane only; no
    // `generate` callable is ever wired, so the node fails closed and
    // no answer_text evidence can be fabricated.
    handlers.insert(
        RagDagNodeType::ModelInference,
        Arc::new(
            |_node: &RagDagNode,
             _context: &RagDagExecutionContext,
             _upstream: &HashMap<String, Value>|
             -> NodeOutcome { NodeOutcome::fail("model-inference:no-generate-callable") },
        ),
    );

    // CITATION_VALIDATION — real validation against the authorized
    // candidate pool; fail-closed verdict.
    handlers.insert(
        RagDagNodeType::CitationValidation,
        Arc::new(
            move |node: &RagDagNode,
                  context: &RagDagExecutionContext,
                  upstream: &HashMap<String, Value>|
                  -> NodeOutcome {
                if context.cancelled() {
                    return NodeOutcome::cancelled();
                }
                let answer = resolve(
                    node.inputs.get("answer_text").unwrap_or(&Value::Null),
                    upstream,
                )
                .and_then(|v| v.as_str().map(String::from))
                .unwrap_or_default();
                let mut citations = Vec::new();
                for m in upstream.values() {
                    if let Some(arr) = m.get("citations").and_then(|c| c.as_array()) {
                        for c in arr {
                            if let Ok(cit) =
                                serde_json::from_value::<crate::evidence::Citation>(c.clone())
                            {
                                citations.push(cit);
                            }
                        }
                    }
                }
                let mut candidates: Vec<RagEvidence> = Vec::new();
                for key in ["reranked_candidates", "fused_candidates", "candidates"] {
                    for m in upstream.values() {
                        if let Some(arr) = m.get(key).and_then(|c| c.as_array()) {
                            candidates.extend(evidence_list_from_json(&Value::Array(
                                arr.clone(),
                            )));
                        }
                    }
                }
                let result = validate_citations(&answer, &citations, &candidates);
                let verdict = result
                    .get("citation_verdict")
                    .and_then(|v| v.as_str())
                    .unwrap_or("rejected");
                let mut ev = result.as_object().cloned().unwrap_or_default();
                if verdict != "validated" {
                    ev.insert(
                        "error".into(),
                        json!(format!("citation-validation:{}", verdict)),
                    );
                    return NodeOutcome {
                        ok: false,
                        error: format!("citation-validation:{}", verdict),
                        evidence: ev,
                    };
                }
                NodeOutcome::ok(ev)
            },
        ),
    );

    // Side-effecting write path — ragd owns reads only; every write
    // node fails closed (`write_path_handlers` with no callables).
    for (t, name) in [
        (RagDagNodeType::Index, "index"),
        (RagDagNodeType::Verification, "verification"),
        (RagDagNodeType::PublishBarrier, "publish-barrier"),
        (RagDagNodeType::Repair, "repair"),
    ] {
        let err = format!("{}:no-callable", name);
        handlers.insert(
            t,
            Arc::new(
                move |_node: &RagDagNode,
                      _context: &RagDagExecutionContext,
                      _upstream: &HashMap<String, Value>|
                      -> NodeOutcome { NodeOutcome::fail(&err.clone()) },
            ),
        );
    }

    handlers
}

// ---------------------------------------------------------------------
// Request / route
// ---------------------------------------------------------------------

#[derive(Clone)]
struct RetrieveRequest {
    query: String,
    collection: String,
    module_ids: Vec<String>,
    rag_types: Vec<String>,
    session_id: String,
    task_instruction: String,
    top_k: usize,
    candidate_limit: usize,
    max_context_chars: usize,
    memory_scopes: Vec<String>,
    cache_request: Option<CacheRequest>,
    kind: RagDagKind,
    budgets: RagDagBudgets,
    required_aspects: Vec<String>,
    permission_scope: String,
    data_categories: Vec<String>,
    identity_id: String,
    generation_id: String,
}

fn str_field(req: &Value, key: &str) -> String {
    req.get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

fn str_list(req: &Value, key: &str) -> Vec<String> {
    req.get(key)
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn parse_request(body: &[u8]) -> Result<RetrieveRequest, Value> {
    let req: Value = serde_json::from_slice(body).map_err(|_| crate::err("INVALID_JSON"))?;
    let query = str_field(&req, "query");
    let module_ids = str_list(&req, "module_ids");
    if query.trim().is_empty() || module_ids.is_empty() {
        return Err(crate::err("INVALID_REQUEST"));
    }
    let kind = RagDagKind::from_name(&str_field(&req, "kind"))
        .unwrap_or(RagDagKind::RetrievalChain);
    // Side-effecting kinds belong to the governed write path, which
    // ragd does not own — refuse rather than plan them.
    if matches!(kind, RagDagKind::Index | RagDagKind::Rebuild | RagDagKind::Repair) {
        return Err(crate::err("INVALID_REQUEST"));
    }
    let top_k = req
        .get("top_k")
        .and_then(Value::as_u64)
        .map(|n| (n as usize).clamp(1, 256))
        .unwrap_or(10);
    let candidate_limit = req
        .get("candidate_limit")
        .and_then(Value::as_u64)
        .map(|n| (n as usize).clamp(1, 256))
        .unwrap_or(24);
    let budgets = {
        let b = req.get("budgets");
        let steps = b
            .and_then(|v| v.get("max_steps"))
            .and_then(Value::as_u64)
            .map(|n| n as usize)
            .unwrap_or(16);
        let seconds = b
            .and_then(|v| v.get("max_seconds"))
            .and_then(Value::as_f64)
            .unwrap_or(60.0);
        let mut budgets = dag::default_budgets(steps, seconds);
        if let Some(c) = b.and_then(|v| v.get("max_cost")).and_then(Value::as_f64) {
            budgets.max_cost = c;
        }
        if let Some(r) = b
            .and_then(|v| v.get("max_rounds"))
            .and_then(Value::as_u64)
        {
            budgets.max_rounds = (r as usize).clamp(1, dag::HARD_MAX_ROUNDS);
        }
        budgets
    };
    let rag_types = str_list(&req, "rag_types");
    let use_cache = req
        .get("use_cache")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let level = CacheLevel::from_name(&str_field(&req, "cache_level"))
        .unwrap_or(CacheLevel::L1);
    let permission_scope = str_field(&req, "permission_scope");
    let cache_request = if use_cache {
        Some(CacheRequest {
            tenant_id: str_field(&req, "tenant_id"),
            module_ids: module_ids.clone(),
            identity_id: str_field(&req, "identity_id"),
            permission_scope: permission_scope.clone(),
            data_classification: str_field(&req, "data_classification"),
            query: query.clone(),
            model_version: str_field(&req, "model_version"),
            policy_version: str_field(&req, "policy_version"),
            source_revision: str_field(&req, "source_revision"),
            level,
            level4_approved: req
                .get("level4_approved")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            rag_architectures: if rag_types.is_empty() {
                vec!["hybrid".to_string()]
            } else {
                rag_types.clone()
            },
            generation_mode: if str_field(&req, "generation_mode").is_empty() {
                "canonical".to_string()
            } else {
                str_field(&req, "generation_mode")
            },
            active_generation: str_field(&req, "generation_id"),
            embedding_model: str_field(&req, "embedding_model"),
            embedding_dimension: req
                .get("embedding_dimension")
                .and_then(Value::as_i64)
                .unwrap_or(0),
            reranker_version: str_field(&req, "reranker_version"),
            chunk_policy_version: str_field(&req, "chunk_policy_version"),
            context_builder_version: str_field(&req, "context_builder_version"),
        })
    } else {
        None
    };
    Ok(RetrieveRequest {
        query,
        collection: {
            let c = str_field(&req, "collection");
            if c.is_empty() {
                "gptbridge_rag".to_string()
            } else {
                c
            }
        },
        module_ids,
        rag_types,
        session_id: str_field(&req, "session_id"),
        task_instruction: str_field(&req, "task_instruction"),
        top_k,
        candidate_limit,
        max_context_chars: req
            .get("max_context_chars")
            .and_then(Value::as_u64)
            .map(|n| (n as usize).min(crate::context::MAX_CONTEXT_CHARS * 4))
            .unwrap_or(crate::context::MAX_CONTEXT_CHARS),
        memory_scopes: str_list(&req, "memory_scopes"),
        cache_request,
        kind,
        budgets,
        required_aspects: str_list(&req, "required_aspects"),
        permission_scope,
        data_categories: str_list(&req, "data_categories"),
        identity_id: str_field(&req, "identity_id"),
        generation_id: str_field(&req, "generation_id"),
    })
}

/// `POST /v1/retrieve` — CAG-gated full retrieval chain.
pub fn handle_retrieve(app: &Arc<App>, body: &[u8]) -> Value {
    let req = match parse_request(body) {
        Ok(r) => r,
        Err(v) => return v,
    };
    // Cached semantic evidence never bypasses current canonical integrity,
    // even when the caller supplies a generation id.
    if let Err(error)=app.with_authority(|_|Ok(())) {
        return json!({"ok":false,"contract":crate::CONTRACT,"error":"AUTHORITY_UNAVAILABLE","authority_error":error});
    }

    // Resolve the active generation once — used by the dense barrier
    // and as CAG key material.
    let alias = "gptbridge_rag";
    let active_generation: Option<String> = if req.generation_id.is_empty() {
        app.with_authority(|pg| pg.active_generation(alias))
            .ok()
            .flatten()
    } else {
        Some(req.generation_id.clone())
    };

    // CAG plane: a validated hit returns the stored payload; the DAG
    // never runs for a hit. A miss proceeds — the gate decided.
    let mut cache_request = req.cache_request.clone();
    if let Some(cr) = cache_request.as_mut() {
        if cr.active_generation.is_empty() {
            cr.active_generation = active_generation.clone().unwrap_or_default();
        }
        if use_cache_allowed(cr) {
            if let Ok(mut guard) = app.cache.lock() {
                let (entry, decision) = guard.get(cr);
                if decision.allowed {
                    if let Some(entry) = entry {
                        let mut payload = entry.payload.clone();
                        if let Some(obj) = payload.as_object_mut() {
                            obj.insert("ok".into(), Value::Bool(true));
                            obj.insert("contract".into(), json!(crate::CONTRACT));
                            obj.insert("cached".into(), Value::Bool(true));
                            obj.insert("cache_id".into(), json!(entry.cache_id));
                            obj.insert("cache_level".into(), json!(entry.level.as_str()));
                            obj.insert("cache_decision".into(), decision.to_record());
                        }
                        return payload;
                    }
                }
            }
        }
    }

    // Build + validate the bounded plan (fail-closed).
    let rid = request_id();
    let context = dag::execution_context(
        &rid,
        &rid,
        &req.identity_id,
        req.module_ids.first().cloned().unwrap_or_default().as_str(),
        &rid,
        "",
        req.module_ids.clone(),
        req.data_categories.clone(),
        &req.permission_scope,
        Some(req.budgets),
    );
    let plan_req = RagDagPlanRequest {
        kind: req.kind,
        dag_id: rid.clone(),
        module_ids: req.module_ids.clone(),
        data_categories: req.data_categories.clone(),
        rag_types: req.rag_types.clone(),
        resource_ids: Vec::new(),
        repair_target: String::new(),
    };
    let plan = match dag::RagDagPlanner::plan(&plan_req, context) {
        Ok(p) => p,
        Err(e) => {
            return crate::err(&format!("RAG_DAG_PLAN_REJECTED:{}", e));
        }
    };

    let handlers = build_handlers(app, &req, active_generation.clone());
    let executor = RagDagExecutor::new(handlers, dag::DEFAULT_NODE_TIMEOUT_SECONDS, 0);
    let result = executor.execute(&plan);
    let record = result.to_record();

    if !matches!(result.state, dag::RagDagState::Succeeded) {
        return json!({
            "ok": false,
            "contract": crate::CONTRACT,
            "service": "ragd",
            "error": format!("RAG_DAG_EXECUTION:{}", result.state.as_str()),
            "failure_reasons": result.failure_reasons,
            "dag": record,
        });
    }

    // Pull the chain outputs from upstream evidence.
    let mut context_text = String::new();
    let mut citations = Value::Array(vec![]);
    let mut fused: Vec<RagEvidence> = Vec::new();
    for r in &result.node_results {
        match r.node_type {
            RagDagNodeType::ContextBuild => {
                context_text = r
                    .evidence
                    .get("context_text")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                citations = r
                    .evidence
                    .get("citations")
                    .cloned()
                    .unwrap_or(Value::Array(vec![]));
            }
            RagDagNodeType::Rerank => {
                fused = evidence_list_from_json(
                    r.evidence.get("reranked_candidates").unwrap_or(&Value::Null),
                );
            }
            _ => {}
        }
    }

    let sufficiency = evaluate_sufficiency(
        &fused,
        &SufficiencyPolicy::default(),
        &req.required_aspects,
        req.budgets.max_rounds,
    );

    let response = json!({
        "ok": true,
        "contract": crate::CONTRACT,
        "service": "ragd",
        "version": crate::VERSION,
        "cached": false,
        "dag_id": rid,
        "kind": req.kind.as_str(),
        "context_text": context_text,
        "citations": citations,
        "evidence": evidence_json_list(&fused),
        "evidence_count": fused.len(),
        "sufficiency": sufficiency,
        "generation_id": active_generation,
        "dag": record,
    });

    // CAG store: cache the derived payload for gated reuse.
    if let Some(cr) = cache_request {
        if let Ok(mut guard) = app.cache.lock() {
            let payload = response.clone();
            guard.put(&cr, payload, &active_generation.clone().unwrap_or_default());
        }
    }
    response
}

fn use_cache_allowed(request: &CacheRequest) -> bool {
    // L4 requires explicit approval; L1-L3 are implemented.
    request.level.implemented() || request.level4_approved
}

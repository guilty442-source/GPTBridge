//! Context Builder + citation validation — native ports of
//! `core_system/rag/orchestration/context_builder.py` and the
//! CITATION_VALIDATION handler semantics (A549).
//!
//! Fixed four-layer order:
//!   1. System / Governance
//!   2. Canonical Source Evidence (SOURCE_TEXT, STRUCTURED_DATA)
//!   3. Specialized Evidence (CODE_SNIPPET, SYMBOL, DEPENDENCY, MEMORY, DERIVED)
//!   4. Task Instruction

use std::collections::{BTreeMap, HashMap, HashSet};

use serde::Serialize;
use serde_json::Value;

use crate::evidence::{make_citation, Citation, EvidenceKind, RagEvidence};

pub const MAX_CONTEXT_CHARS: usize = 24000;

#[derive(Debug, Clone, Serialize)]
pub struct ContextSection {
    pub layer: usize,
    pub title: String,
    pub body: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct BuiltContext {
    pub sections: Vec<ContextSection>,
    pub text: String,
    pub citations: Vec<Citation>,
    pub evidence_used: usize,
}

fn is_layer2(kind: EvidenceKind) -> bool {
    matches!(kind, EvidenceKind::SourceText | EvidenceKind::StructuredData)
}

/// `_merge_adjacent` — merge SOURCE_TEXT chunks adjacent in one
/// resource (chunk_index consecutive).
fn merge_adjacent(evidence: &[RagEvidence]) -> Vec<RagEvidence> {
    let mut by_res: HashMap<String, Vec<RagEvidence>> = HashMap::new();
    let mut out: Vec<RagEvidence> = Vec::new();
    for e in evidence {
        if e.evidence_kind != EvidenceKind::SourceText
            || e.resource_id.is_empty()
            || !e.provenance.contains_key("sequence")
        {
            out.push(e.clone());
            continue;
        }
        by_res
            .entry(e.resource_id.clone())
            .or_default()
            .push(e.clone());
    }
    for group in by_res.values_mut() {
        group.sort_by_key(|e| {
            e.provenance
                .get("sequence")
                .and_then(|v| v.as_i64())
                .unwrap_or(0)
        });
        let mut merged: Vec<RagEvidence> = Vec::new();
        for e in group.drain(..) {
            let idx = e
                .provenance
                .get("sequence")
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            let adjacent = merged
                .last()
                .and_then(|prev| {
                    prev.provenance
                        .get("sequence")
                        .and_then(|v| v.as_i64())
                        .map(|p| idx == p + 1)
                })
                .unwrap_or(false);
            if adjacent {
                let prev = merged.last_mut().unwrap();
                prev.content = format!("{}\n{}", prev.content, e.content);
                prev.provenance.insert(
                    "merged_with".to_string(),
                    Value::String(e.evidence_id.clone()),
                );
            } else {
                merged.push(e);
            }
        }
        out.extend(merged);
    }
    out
}

/// `_format_evidence` — kind-aware rendering.
fn format_evidence(e: &RagEvidence, label: &str) -> String {
    match e.evidence_kind {
        EvidenceKind::Memory => {
            let scope = e
                .provenance
                .get("memory_kind")
                .and_then(|v| v.as_str())
                .unwrap_or("memory");
            format!("{} (memory:{}) {}", label, scope, e.content)
        }
        EvidenceKind::Dependency => {
            let src = e
                .provenance
                .get("source_symbol")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let dst = e
                .provenance
                .get("target_symbol")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let et = e
                .provenance
                .get("edge_type")
                .and_then(|v| v.as_str())
                .unwrap_or("REFERENCES");
            format!("{} {} -[{}]-> {}", label, src, et, dst)
        }
        EvidenceKind::Symbol => {
            let sym = e
                .provenance
                .get("symbol_id")
                .and_then(|v| v.as_str())
                .unwrap_or(&e.evidence_id);
            format!("{} symbol `{}`\n{}", label, sym, e.content)
        }
        _ => format!("{} {}", label, e.content),
    }
}

/// `build_context` — assemble the four-layer context within the budget.
pub fn build_context(
    evidence: &[RagEvidence],
    system_governance: &str,
    task_instruction: &str,
    max_context_chars: usize,
) -> BuiltContext {
    let prepared = merge_adjacent(
        &evidence
            .iter()
            .filter(|e| e.authorized)
            .cloned()
            .collect::<Vec<_>>(),
    );
    let mut citations: Vec<Citation> = Vec::new();
    let mut layer2: Vec<String> = Vec::new();
    let mut layer3: Vec<String> = Vec::new();
    for (i, e) in prepared.iter().enumerate() {
        let cit = make_citation(i + 1, e);
        let line = format_evidence(e, &cit.label);
        citations.push(cit);
        if is_layer2(e.evidence_kind) {
            layer2.push(line);
        } else {
            layer3.push(line);
        }
    }
    let sections = vec![
        ContextSection {
            layer: 1,
            title: "System / Governance".to_string(),
            body: system_governance.to_string(),
        },
        ContextSection {
            layer: 2,
            title: "Canonical Source Evidence".to_string(),
            body: layer2.join("\n\n"),
        },
        ContextSection {
            layer: 3,
            title: "Specialized Evidence".to_string(),
            body: layer3.join("\n\n"),
        },
        ContextSection {
            layer: 4,
            title: "Task Instruction".to_string(),
            body: task_instruction.to_string(),
        },
    ];
    let mut text: String = sections
        .iter()
        .filter(|s| !s.body.is_empty())
        .map(|s| format!("## {}\n{}", s.title, s.body))
        .collect::<Vec<_>>()
        .join("\n\n");
    let budget = if max_context_chars == 0 {
        MAX_CONTEXT_CHARS
    } else {
        max_context_chars
    };
    if text.len() > budget {
        // Truncate on a char boundary.
        let mut end = budget;
        while end > 0 && !text.is_char_boundary(end) {
            end -= 1;
        }
        text.truncate(end);
    }
    BuiltContext {
        sections,
        text,
        citations,
        evidence_used: prepared.len(),
    }
}

/// CITATION_VALIDATION node semantics — every `[R#]` label the answer
/// cites must resolve to a citation whose evidence is authorized and
/// present in the candidate pool. Returns the validated citation
/// records + verdict; callers treat "rejected" as fail-closed.
pub fn validate_citations(
    answer_text: &str,
    citations: &[Citation],
    candidates: &[RagEvidence],
) -> Value {
    let authorized: HashMap<&str, &RagEvidence> = candidates
        .iter()
        .filter(|e| e.authorized)
        .map(|e| (e.evidence_id.as_str(), e))
        .collect();
    let mut validated: Vec<Value> = Vec::new();
    let mut rejected = 0usize;
    for citation in citations {
        match authorized.get(citation.evidence_id.as_str()) {
            None => rejected += 1,
            Some(ev) => {
                let char_start = ev
                    .provenance
                    .get("character_start")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);
                let char_end = ev
                    .provenance
                    .get("character_end")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);
                validated.push(serde_json::json!({
                    "citation_id": citation.label,
                    "resource_id": ev.resource_id,
                    "chunk_id": ev.chunk_id,
                    "locator_id": if citation.locator_id.is_empty() { ev.locator_id.clone() } else { citation.locator_id.clone() },
                    "character_start": char_start,
                    "character_end": char_end,
                    "content_hash": ev.content_hash,
                    "generation_id": ev.generation_id,
                    "retrieval_score": if ev.reranker_score != 0.0 { ev.reranker_score } else { ev.dense_score },
                    "module_id": if ev.module_id.is_empty() { Value::Null } else { Value::String(ev.module_id.clone()) },
                }));
            }
        }
    }
    let known_labels: HashSet<String> = citations
        .iter()
        .map(|c| c.label.trim_matches(|ch| ch == '[' || ch == ']').to_string())
        .collect();
    let mut unresolved: Vec<String> = Vec::new();
    let bytes = answer_text.as_bytes();
    let mut i = 0;
    while i + 1 < bytes.len() {
        if bytes[i] == b'[' {
            if let Some(end) = bytes[i..].iter().position(|&b| b == b']') {
                let token = &answer_text[i + 1..i + end];
                let is_label = !token.is_empty()
                    && token.chars().next().map(|c| c.is_ascii_uppercase()).unwrap_or(false)
                    && token.chars().all(|c| c.is_ascii_alphanumeric());
                if is_label && !known_labels.contains(token) {
                    unresolved.push(token.to_string());
                }
                i += end + 1;
                continue;
            }
        }
        i += 1;
    }
    let verdict = if !validated.is_empty() && rejected == 0 && unresolved.is_empty() {
        "validated"
    } else {
        "rejected"
    };
    serde_json::json!({
        "validated_citations": validated,
        "citation_verdict": verdict,
        "rejected_count": rejected,
        "unresolved_labels": unresolved,
    })
}

/// Helper: `[R1]`-style label tokens appearing in an answer. Kept for
/// callers that render labels separately.
#[allow(dead_code)]
pub fn citation_labels(answer_text: &str) -> BTreeMap<String, ()> {
    let mut labels = BTreeMap::new();
    let bytes = answer_text.as_bytes();
    let mut i = 0;
    while i + 1 < bytes.len() {
        if bytes[i] == b'[' {
            if let Some(end) = bytes[i..].iter().position(|&b| b == b']') {
                let token = &answer_text[i + 1..i + end];
                if !token.is_empty()
                    && token.chars().next().map(|c| c.is_ascii_uppercase()).unwrap_or(false)
                    && token.chars().all(|c| c.is_ascii_alphanumeric())
                {
                    labels.insert(token.to_string(), ());
                }
                i += end + 1;
                continue;
            }
        }
        i += 1;
    }
    labels
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::evidence::{EvidenceKind, RagArchitecture, RagEvidence};

    fn ev(id: &str, content: &str) -> RagEvidence {
        RagEvidence {
            evidence_id: id.into(),
            rag_type: RagArchitecture::Hybrid,
            content: content.into(),
            evidence_kind: EvidenceKind::SourceText,
            resource_id: "r1".into(),
            ..Default::default()
        }
    }

    #[test]
    fn context_has_fixed_layer_order() {
        let built = build_context(&[ev("a", "alpha"), ev("b", "beta")], "SYS", "TASK", 0);
        assert!(built.text.contains("## System / Governance"));
        assert!(built.text.contains("## Canonical Source Evidence"));
        assert!(built.text.contains("## Task Instruction"));
        let sys = built.text.find("System / Governance").unwrap();
        let evs = built.text.find("Canonical Source Evidence").unwrap();
        let task = built.text.find("Task Instruction").unwrap();
        assert!(sys < evs && evs < task);
        assert_eq!(built.citations.len(), 2);
        assert_eq!(built.citations[0].label, "R1");
    }

    #[test]
    fn unauthorized_evidence_never_enters_context() {
        let mut bad = ev("bad", "secret");
        bad.authorized = false;
        let built = build_context(&[bad], "", "", 0);
        assert_eq!(built.evidence_used, 0);
        assert!(!built.text.contains("secret"));
    }

    #[test]
    fn citation_validation_fails_closed() {
        let candidates = vec![ev("e1", "x")];
        let citations = vec![crate::evidence::make_citation(1, &candidates[0])];
        let ok = validate_citations("answer cites [R1]", &citations, &candidates);
        assert_eq!(ok["citation_verdict"], "validated");
        let bad = validate_citations("answer cites [R9]", &citations, &candidates);
        assert_eq!(bad["citation_verdict"], "rejected");
        assert_eq!(bad["unresolved_labels"][0], "R9");
    }
}

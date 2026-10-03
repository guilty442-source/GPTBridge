//! Unified evidence contract — native port of
//! `core_system/rag/orchestration/evidence.py` (A549).
//!
//! Every retriever lane emits `RagEvidence`; fusion, rerank, context
//! building and citation validation all consume this single type.
//! Authority is a separate field, never blended into similarity scores:
//! canonical-source > derived > contextual-memory.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum RagArchitecture {
    #[serde(rename = "hybrid-rag")]
    Hybrid,
    #[serde(rename = "code-rag")]
    Code,
    #[serde(rename = "memory-rag")]
    Memory,
    #[serde(rename = "agentic-rag")]
    Agentic,
    #[serde(rename = "multi-agent-rag")]
    MultiAgent,
    #[serde(rename = "graph-rag")]
    Graph,
    #[serde(rename = "tag-rag")]
    Tag,
    #[serde(rename = "multimodal-rag")]
    Multimodal,
}

impl RagArchitecture {
    /// Tolerates bare names ("hybrid") alongside enum values
    /// ("hybrid-rag") — same resolution as the Python `_ARCH_BY_NAME`.
    /// Retrieval-plane roles (C106): Agentic-RAG is single-agent
    /// autonomous retrieval; Multi-Agent-RAG is collaborative
    /// retrieval under the same bounded DAG; GraphRAG/TAG/Memory/
    /// Multimodal are specialized governed data paths. XRAG is not a
    /// lane (context compression) and GAG resolves to the DAG plane —
    /// neither parses here.
    pub fn from_name(name: &str) -> Option<Self> {
        match name {
            "hybrid-rag" | "hybrid" => Some(Self::Hybrid),
            "code-rag" | "code" => Some(Self::Code),
            "memory-rag" | "memory" => Some(Self::Memory),
            "agentic-rag" | "agentic" => Some(Self::Agentic),
            "multi-agent-rag" | "multi-agent" | "multiagent" => Some(Self::MultiAgent),
            "graph-rag" | "graphrag" | "graph" => Some(Self::Graph),
            "tag-rag" | "tag" => Some(Self::Tag),
            "multimodal-rag" | "multimodal" => Some(Self::Multimodal),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Hybrid => "hybrid-rag",
            Self::Code => "code-rag",
            Self::Memory => "memory-rag",
            Self::Agentic => "agentic-rag",
            Self::MultiAgent => "multi-agent-rag",
            Self::Graph => "graph-rag",
            Self::Tag => "tag-rag",
            Self::Multimodal => "multimodal-rag",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
pub enum EvidenceKind {
    #[serde(rename = "SOURCE_TEXT")]
    #[default]
    SourceText,
    #[serde(rename = "CODE_SNIPPET")]
    CodeSnippet,
    #[serde(rename = "SYMBOL")]
    Symbol,
    #[serde(rename = "DEPENDENCY")]
    Dependency,
    #[serde(rename = "MEMORY")]
    Memory,
    #[serde(rename = "DERIVED")]
    Derived,
    #[serde(rename = "STRUCTURED_DATA")]
    StructuredData,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum SourceAuthority {
    CanonicalSource,
    StructuredAuthority,
    CodeCurrent,
    Derived,
    ContextualMemory,
    DegradedCache,
}

impl SourceAuthority {
    /// `AUTHORITY_RANK` — ordering beats similarity on conflict.
    pub fn rank(self) -> i64 {
        match self {
            Self::CanonicalSource => 6,
            Self::StructuredAuthority => 5,
            Self::CodeCurrent => 4,
            Self::Derived => 3,
            Self::ContextualMemory => 2,
            Self::DegradedCache => 1,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::CanonicalSource => "canonical-source",
            Self::StructuredAuthority => "structured-authority",
            Self::CodeCurrent => "code-current",
            Self::Derived => "derived",
            Self::ContextualMemory => "contextual-memory",
            Self::DegradedCache => "degraded-cache",
        }
    }
}

/// Authority resolution — `authority_of(e)`: explicit provenance marker
/// wins, otherwise the evidence's stored authority field, otherwise the
/// architecture default (memory -> contextual-memory, everything else
/// -> canonical-source because every served lane is PG-proved).
pub fn authority_of(e: &RagEvidence) -> SourceAuthority {
    if let Some(marker) = e
        .provenance
        .get("authority")
        .and_then(Value::as_str)
    {
        match marker {
            "canonical-source" => return SourceAuthority::CanonicalSource,
            "structured-authority" => return SourceAuthority::StructuredAuthority,
            "code-current" => return SourceAuthority::CodeCurrent,
            "derived" => return SourceAuthority::Derived,
            "contextual-memory" => return SourceAuthority::ContextualMemory,
            "degraded-cache" => return SourceAuthority::DegradedCache,
            _ => {}
        }
    }
    if let Some(a) = e.authority {
        return a;
    }
    match e.rag_type {
        RagArchitecture::Memory => SourceAuthority::ContextualMemory,
        RagArchitecture::Code => SourceAuthority::CodeCurrent,
        RagArchitecture::Tag => SourceAuthority::StructuredAuthority,
        _ => SourceAuthority::CanonicalSource,
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RagEvidence {
    pub evidence_id: String,
    pub rag_type: RagArchitecture,
    #[serde(default)]
    pub module_id: String,
    #[serde(default)]
    pub resource_id: String,
    #[serde(default)]
    pub chunk_id: String,
    #[serde(default)]
    pub locator_id: String,
    #[serde(default)]
    pub evidence_kind: EvidenceKind,
    #[serde(default)]
    pub generation_id: String,
    #[serde(default)]
    pub source_version: i64,
    #[serde(default)]
    pub content_hash: String,

    // Channel scores — kept separate; fusion decides how they combine.
    #[serde(default)]
    pub dense_score: f64,
    #[serde(default)]
    pub sparse_score: f64,
    #[serde(default)]
    pub graph_score: f64,
    #[serde(default)]
    pub memory_score: f64,
    #[serde(default)]
    pub reranker_score: f64,

    // Authority / admission.
    #[serde(default)]
    pub authority: Option<SourceAuthority>,
    #[serde(default = "default_true")]
    pub authorized: bool,
    #[serde(default)]
    pub freshness: f64,
    #[serde(default)]
    pub evidence_conflict: bool,

    #[serde(default)]
    pub content: String,
    #[serde(default)]
    pub provenance: BTreeMap<String, Value>,
}

fn default_true() -> bool {
    true
}

impl Default for RagEvidence {
    fn default() -> Self {
        Self {
            evidence_id: String::new(),
            rag_type: RagArchitecture::Hybrid,
            module_id: String::new(),
            resource_id: String::new(),
            chunk_id: String::new(),
            locator_id: String::new(),
            evidence_kind: EvidenceKind::SourceText,
            generation_id: String::new(),
            source_version: 0,
            content_hash: String::new(),
            dense_score: 0.0,
            sparse_score: 0.0,
            graph_score: 0.0,
            memory_score: 0.0,
            reranker_score: 0.0,
            authority: None,
            authorized: true,
            freshness: 0.0,
            evidence_conflict: false,
            content: String::new(),
            provenance: BTreeMap::new(),
        }
    }
}

impl RagEvidence {
    /// Build evidence from a barrier-proved record (`pipeline.py`
    /// `_apply_read_barrier` shape) — every field here is canonical:
    /// the record already passed module scope, generation match,
    /// chunk-metadata existence and index-state proofing.
    pub fn from_proved_record(record: &Value, rag_type: RagArchitecture) -> Self {
        let get = |k: &str| record.get(k).and_then(Value::as_str).unwrap_or("");
        let point_id = get("point_id");
        let id = if point_id.is_empty() {
            get("id").to_string()
        } else {
            point_id.to_string()
        };
        let score = record
            .get("vector_score")
            .or_else(|| record.get("score"))
            .and_then(Value::as_f64)
            .unwrap_or(0.0);
        let mut provenance = BTreeMap::new();
        provenance.insert(
            "title".to_string(),
            json!(get("title")),
        );
        provenance.insert("source".to_string(), json!(get("source")));
        provenance.insert(
            "sequence".to_string(),
            record.get("sequence").cloned().unwrap_or(Value::Null),
        );
        provenance.insert(
            "character_start".to_string(),
            record.get("character_start").cloned().unwrap_or(Value::Null),
        );
        provenance.insert(
            "character_end".to_string(),
            record.get("character_end").cloned().unwrap_or(Value::Null),
        );
        if let Some(meta) = record.get("metadata") {
            provenance.insert("resource_metadata".to_string(), meta.clone());
        }
        Self {
            evidence_id: id,
            rag_type,
            module_id: get("module_id").to_string(),
            resource_id: {
                let r = get("resource_id");
                if r.is_empty() {
                    get("document_resource_id").to_string()
                } else {
                    r.to_string()
                }
            },
            chunk_id: get("chunk_id").to_string(),
            locator_id: get("locator_fragment").to_string(),
            generation_id: get("generation_id").to_string(),
            dense_score: score,
            content: get("content").to_string(),
            freshness: 1.0,
            provenance,
            ..Default::default()
        }
    }
}

/// Citation record — `make_citation(i, e)` shape from evidence.py.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Citation {
    pub label: String,
    pub evidence_id: String,
    pub resource_id: String,
    pub chunk_id: String,
    pub locator_id: String,
    pub module_id: String,
    pub authority: String,
}

pub fn make_citation(index: usize, e: &RagEvidence) -> Citation {
    Citation {
        label: format!("R{}", index),
        evidence_id: e.evidence_id.clone(),
        resource_id: e.resource_id.clone(),
        chunk_id: e.chunk_id.clone(),
        locator_id: e.locator_id.clone(),
        module_id: e.module_id.clone(),
        authority: authority_of(e).as_str().to_string(),
    }
}

pub fn evidence_list_from_json(value: &Value) -> Vec<RagEvidence> {
    value
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|v| serde_json::from_value::<RagEvidence>(v.clone()).ok())
                .collect()
        })
        .unwrap_or_default()
}

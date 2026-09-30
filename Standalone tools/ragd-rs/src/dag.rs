//! RAG DAG plane — native port of `core_system/rag/dag/` (A549).
//!
//! Declarative contract surface: fixed node catalog, closed state set,
//! six bounded DAG kinds, declarative node/edge records. The planner
//! builds and validates plans fail-closed (cycles, unregistered nodes,
//! unauthorized dependencies, incomplete inputs, budget envelopes).
//! The executor runs a validated plan in deterministic topological
//! order with per-node timeout, bounded retries, cancellation and
//! compensation — no outcome is ever fabricated.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

// ---------------------------------------------------------------------
// Closed catalogs (contracts.py)
// ---------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
pub enum RagDagNodeType {
    #[serde(rename = "retrieval")]
    Retrieval,
    #[serde(rename = "cache-lookup")]
    CacheLookup,
    #[serde(rename = "cache-validate")]
    CacheValidate,
    #[serde(rename = "fusion")]
    Fusion,
    #[serde(rename = "rerank")]
    Rerank,
    #[serde(rename = "context-build")]
    ContextBuild,
    #[serde(rename = "model-inference")]
    ModelInference,
    #[serde(rename = "citation-validation")]
    CitationValidation,
    #[serde(rename = "index")]
    Index,
    #[serde(rename = "repair")]
    Repair,
    #[serde(rename = "verification")]
    Verification,
    #[serde(rename = "publish-barrier")]
    PublishBarrier,
}

impl RagDagNodeType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Retrieval => "retrieval",
            Self::CacheLookup => "cache-lookup",
            Self::CacheValidate => "cache-validate",
            Self::Fusion => "fusion",
            Self::Rerank => "rerank",
            Self::ContextBuild => "context-build",
            Self::ModelInference => "model-inference",
            Self::CitationValidation => "citation-validation",
            Self::Index => "index",
            Self::Repair => "repair",
            Self::Verification => "verification",
            Self::PublishBarrier => "publish-barrier",
        }
    }
}

/// Closed state set (A549) — variants beyond what the current
/// executor constructs are kept for contract parity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[allow(dead_code)]
pub enum RagDagState {
    #[serde(rename = "pending")]
    Pending,
    #[serde(rename = "ready")]
    Ready,
    #[serde(rename = "running")]
    Running,
    #[serde(rename = "succeeded")]
    Succeeded,
    #[serde(rename = "failed")]
    Failed,
    #[serde(rename = "skipped")]
    Skipped,
    #[serde(rename = "cancelled")]
    Cancelled,
    #[serde(rename = "blocked")]
    Blocked,
    #[serde(rename = "requires-reconcile")]
    RequiresReconcile,
    #[serde(rename = "quarantined")]
    Quarantined,
}

impl RagDagState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Ready => "ready",
            Self::Running => "running",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Skipped => "skipped",
            Self::Cancelled => "cancelled",
            Self::Blocked => "blocked",
            Self::RequiresReconcile => "requires-reconcile",
            Self::Quarantined => "quarantined",
        }
    }
    /// `TERMINAL_STATES` — contract helper kept for state-machine parity.
    #[allow(dead_code)]
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Succeeded
                | Self::Failed
                | Self::Skipped
                | Self::Cancelled
                | Self::Blocked
                | Self::RequiresReconcile
                | Self::Quarantined
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
pub enum RagDagKind {
    #[serde(rename = "query")]
    Query,
    #[serde(rename = "multi-rag")]
    MultiRag,
    #[serde(rename = "retrieval-chain")]
    RetrievalChain,
    #[serde(rename = "index")]
    Index,
    #[serde(rename = "rebuild")]
    Rebuild,
    #[serde(rename = "repair")]
    Repair,
}

impl RagDagKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Query => "query",
            Self::MultiRag => "multi-rag",
            Self::RetrievalChain => "retrieval-chain",
            Self::Index => "index",
            Self::Rebuild => "rebuild",
            Self::Repair => "repair",
        }
    }
    pub fn from_name(name: &str) -> Option<Self> {
        match name {
            "query" => Some(Self::Query),
            "multi-rag" => Some(Self::MultiRag),
            "retrieval-chain" | "retrieval_chain" => Some(Self::RetrievalChain),
            "index" => Some(Self::Index),
            "rebuild" => Some(Self::Rebuild),
            "repair" => Some(Self::Repair),
            _ => None,
        }
    }
}

/// `REGISTERED_NODE_CATALOG` — declarative spec per node type.
pub struct RagDagNodeSpec {
    pub required_inputs: &'static [&'static str],
    pub required_evidence: &'static [&'static str],
    pub side_effecting: bool,
    pub permission_scope_required: bool,
}

pub fn node_spec(t: RagDagNodeType) -> RagDagNodeSpec {
    use RagDagNodeType::*;
    match t {
        Retrieval => RagDagNodeSpec {
            required_inputs: &["query", "module_ids", "scope"],
            required_evidence: &["candidates", "provenance"],
            side_effecting: false,
            permission_scope_required: false,
        },
        CacheLookup => RagDagNodeSpec {
            required_inputs: &["cache_key"],
            required_evidence: &["cache_hit", "cache_metadata"],
            side_effecting: false,
            permission_scope_required: false,
        },
        CacheValidate => RagDagNodeSpec {
            required_inputs: &["cache_entry"],
            required_evidence: &[
                "scope_check",
                "permission_check",
                "revision_check",
                "expiry_check",
                "authority_check",
            ],
            side_effecting: false,
            permission_scope_required: false,
        },
        Fusion => RagDagNodeSpec {
            required_inputs: &["candidate_sets"],
            required_evidence: &["fused_candidates", "fusion_method"],
            side_effecting: false,
            permission_scope_required: false,
        },
        Rerank => RagDagNodeSpec {
            required_inputs: &["fused_candidates", "reranker_limit"],
            required_evidence: &["reranked_candidates", "reranker_model"],
            side_effecting: false,
            permission_scope_required: false,
        },
        ContextBuild => RagDagNodeSpec {
            required_inputs: &["reranked_candidates", "max_context_tokens"],
            required_evidence: &["context_text", "token_budget"],
            side_effecting: false,
            permission_scope_required: false,
        },
        ModelInference => RagDagNodeSpec {
            required_inputs: &["context_text", "model_identity"],
            required_evidence: &["answer_text", "model_identity", "policy_version"],
            side_effecting: false,
            permission_scope_required: false,
        },
        CitationValidation => RagDagNodeSpec {
            required_inputs: &["answer_text", "citations"],
            required_evidence: &["validated_citations", "citation_verdict"],
            side_effecting: false,
            permission_scope_required: false,
        },
        Index => RagDagNodeSpec {
            required_inputs: &["resource_id", "content_hash", "generation_id"],
            required_evidence: &["indexed_points", "source_revision"],
            side_effecting: true,
            permission_scope_required: true,
        },
        Repair => RagDagNodeSpec {
            required_inputs: &["repair_target", "repair_plan"],
            required_evidence: &["repair_result", "rollback_reference"],
            side_effecting: true,
            permission_scope_required: true,
        },
        Verification => RagDagNodeSpec {
            required_inputs: &["verification_target"],
            required_evidence: &["verification_result", "independent_verifier"],
            side_effecting: false,
            permission_scope_required: false,
        },
        PublishBarrier => RagDagNodeSpec {
            required_inputs: &["publish_target", "verification_result"],
            required_evidence: &["publish_state", "authority_marker"],
            side_effecting: true,
            permission_scope_required: true,
        },
    }
}

/// `KIND_REQUIRED_NODE_TYPES`.
fn kind_required_nodes(kind: RagDagKind) -> &'static [RagDagNodeType] {
    use RagDagNodeType::*;
    match kind {
        RagDagKind::Query | RagDagKind::MultiRag => {
            &[Retrieval, Fusion, ModelInference, CitationValidation]
        }
        RagDagKind::RetrievalChain => &[Retrieval, Fusion, ContextBuild],
        RagDagKind::Index | RagDagKind::Rebuild => &[Index, PublishBarrier],
        RagDagKind::Repair => &[Repair, Verification, PublishBarrier],
    }
}

/// `KIND_ORDERING` — declared ordering pairs per kind.
fn kind_ordering(kind: RagDagKind) -> &'static [(RagDagNodeType, RagDagNodeType)] {
    use RagDagNodeType::*;
    match kind {
        RagDagKind::Query | RagDagKind::MultiRag => &[
            (CacheLookup, CacheValidate),
            (CacheValidate, Retrieval),
            (Retrieval, Fusion),
            (Fusion, Rerank),
            (Rerank, ContextBuild),
            (ContextBuild, ModelInference),
            (ModelInference, CitationValidation),
        ],
        RagDagKind::RetrievalChain => &[
            (Retrieval, Fusion),
            (Fusion, Rerank),
            (Rerank, ContextBuild),
        ],
        RagDagKind::Index => &[(Index, PublishBarrier)],
        RagDagKind::Rebuild => &[(Index, Verification), (Verification, PublishBarrier)],
        RagDagKind::Repair => &[(Repair, Verification), (Verification, PublishBarrier)],
    }
}

// ---------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct RagDagEdge {
    pub source_id: String,
    pub target_id: String,
    #[allow(dead_code)]
    pub kind: String,
    pub authorized: bool,
}

impl RagDagEdge {
    #[allow(dead_code)]
    fn to_record(&self) -> Value {
        serde_json::json!({
            "source_id": self.source_id,
            "target_id": self.target_id,
            "kind": self.kind,
            "authorized": self.authorized,
        })
    }
}

#[derive(Debug, Clone)]
pub struct RagDagNode {
    pub node_id: String,
    pub node_type: RagDagNodeType,
    pub inputs: Map<String, Value>,
    pub dependencies: Vec<String>,
    #[allow(dead_code)]
    pub state: RagDagState,
}

impl RagDagNode {
    #[allow(dead_code)]
    fn to_record(&self) -> Value {
        serde_json::json!({
            "node_id": self.node_id,
            "node_type": self.node_type.as_str(),
            "inputs": self.inputs,
            "dependencies": self.dependencies,
            "state": self.state.as_str(),
            "failure_policy": "FAIL_CLOSED",
        })
    }
}

#[derive(Debug, Clone, Copy)]
pub struct RagDagBudgets {
    pub max_steps: usize,
    pub max_seconds: f64,
    pub max_cost: f64,
    pub max_rounds: usize,
}

pub const HARD_MAX_STEPS: usize = 64;
pub const HARD_MAX_SECONDS: f64 = 600.0;
pub const HARD_MAX_COST: f64 = 10_000.0;
pub const HARD_MAX_ROUNDS: usize = 3;

/// `default_budgets` — sane bounds inside the hard envelope.
pub fn default_budgets(max_steps: usize, max_seconds: f64) -> RagDagBudgets {
    RagDagBudgets {
        max_steps: max_steps.clamp(1, HARD_MAX_STEPS),
        max_seconds: max_seconds.clamp(0.1, HARD_MAX_SECONDS),
        max_cost: 100.0,
        max_rounds: 1,
    }
}

#[derive(Debug, Clone)]
pub struct RagDagExecutionContext {
    pub execution_id: String,
    pub correlation_id: String,
    #[allow(dead_code)]
    pub actor_id: String,
    #[allow(dead_code)]
    pub module_id: String,
    #[allow(dead_code)]
    pub request_id: String,
    #[allow(dead_code)]
    pub decision_id: String,
    pub module_ids: Vec<String>,
    #[allow(dead_code)]
    pub data_categories: Vec<String>,
    pub permission_scope: String,
    pub budgets: RagDagBudgets,
    #[allow(dead_code)]
    pub created_at: f64,
    pub cancel_requested: bool,
    pub cancel_event: Option<Arc<AtomicBool>>,
}

impl RagDagExecutionContext {
    /// Record surface — used by plan records / audit export.
    #[allow(dead_code)]
    pub fn to_record(&self) -> Value {
        serde_json::json!({
            "execution_id": self.execution_id,
            "correlation_id": self.correlation_id,
            "actor_id": self.actor_id,
            "module_id": self.module_id,
            "request_id": self.request_id,
            "decision_id": self.decision_id,
            "module_ids": self.module_ids,
            "data_categories": self.data_categories,
            "permission_scope": self.permission_scope,
            "budgets": {
                "max_steps": self.budgets.max_steps,
                "max_seconds": self.budgets.max_seconds,
                "max_cost": self.budgets.max_cost,
                "max_rounds": self.budgets.max_rounds,
            },
            "created_at": self.created_at,
        })
    }

    pub fn cancelled(&self) -> bool {
        if self.cancel_requested {
            return true;
        }
        self.cancel_event
            .as_ref()
            .map(|e| e.load(Ordering::SeqCst))
            .unwrap_or(false)
    }
}

#[derive(Debug, Clone)]
pub struct RagDagPlan {
    pub dag_id: String,
    pub kind: RagDagKind,
    pub nodes: Vec<RagDagNode>,
    pub edges: Vec<RagDagEdge>,
    pub context: RagDagExecutionContext,
}

impl RagDagPlan {
    pub fn node(&self, node_id: &str) -> Option<&RagDagNode> {
        self.nodes.iter().find(|n| n.node_id == node_id)
    }
    /// Record surface — used by callers that persist the plan shape.
    #[allow(dead_code)]
    pub fn to_record(&self) -> Value {
        serde_json::json!({
            "dag_id": self.dag_id,
            "kind": self.kind.as_str(),
            "nodes": self.nodes.iter().map(|n| n.to_record()).collect::<Vec<_>>(),
            "edges": self.edges.iter().map(|e| e.to_record()).collect::<Vec<_>>(),
            "context": self.context.to_record(),
        })
    }
}

#[derive(Debug, Clone)]
pub struct RagDagPlanRequest {
    pub kind: RagDagKind,
    pub dag_id: String,
    pub module_ids: Vec<String>,
    #[allow(dead_code)]
    pub data_categories: Vec<String>,
    pub rag_types: Vec<String>,
    pub resource_ids: Vec<String>,
    pub repair_target: String,
}

#[derive(Debug, Clone)]
pub struct RagDagNodeResult {
    pub node_id: String,
    pub node_type: RagDagNodeType,
    pub state: RagDagState,
    pub attempts: usize,
    pub latency_ms: i64,
    pub evidence: Map<String, Value>,
    pub error: String,
}

impl RagDagNodeResult {
    fn to_record(&self) -> Value {
        serde_json::json!({
            "node_id": self.node_id,
            "node_type": self.node_type.as_str(),
            "state": self.state.as_str(),
            "attempts": self.attempts,
            "latency_ms": self.latency_ms,
            "evidence": self.evidence,
            "error": self.error,
        })
    }
}

#[derive(Debug)]
pub struct RagDagExecutionResult {
    pub dag_id: String,
    pub kind: RagDagKind,
    pub state: RagDagState,
    pub execution_id: String,
    pub correlation_id: String,
    pub node_results: Vec<RagDagNodeResult>,
    pub latency_ms: i64,
    pub evidence_digest: String,
    pub failure_reasons: Vec<String>,
    pub compensations: Map<String, Value>,
}

impl RagDagExecutionResult {
    pub fn to_record(&self) -> Value {
        serde_json::json!({
            "dag_id": self.dag_id,
            "kind": self.kind.as_str(),
            "state": self.state.as_str(),
            "execution_id": self.execution_id,
            "correlation_id": self.correlation_id,
            "node_results": self.node_results.iter().map(|r| r.to_record()).collect::<Vec<_>>(),
            "latency_ms": self.latency_ms,
            "evidence_digest": self.evidence_digest,
            "failure_reasons": self.failure_reasons,
            "compensations": self.compensations,
        })
    }
}

// ---------------------------------------------------------------------
// Planner (planner.py)
// ---------------------------------------------------------------------

pub struct RagDagPlanner;

fn node(
    node_id: &str,
    node_type: RagDagNodeType,
    inputs: Map<String, Value>,
    dependencies: Vec<String>,
) -> RagDagNode {
    RagDagNode {
        node_id: node_id.to_string(),
        node_type,
        inputs,
        dependencies,
        state: RagDagState::Pending,
    }
}

fn inputs(pairs: &[(&str, Value)]) -> Map<String, Value> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.clone()))
        .collect()
}

impl RagDagPlanner {
    pub fn plan(
        request: &RagDagPlanRequest,
        context: RagDagExecutionContext,
    ) -> Result<RagDagPlan, String> {
        let nodes = match request.kind {
            RagDagKind::Query => Self::build_query(request, &context),
            RagDagKind::MultiRag => Self::build_multi_rag(request, &context),
            RagDagKind::RetrievalChain => Self::build_retrieval_chain(request, &context),
            RagDagKind::Index => Self::build_index(request, &context),
            RagDagKind::Rebuild => Self::build_rebuild(request, &context),
            RagDagKind::Repair => Self::build_repair(request, &context),
        };
        let edges: Vec<RagDagEdge> = nodes
            .iter()
            .flat_map(|n| {
                n.dependencies.iter().map(|dep| RagDagEdge {
                    source_id: dep.clone(),
                    target_id: n.node_id.clone(),
                    kind: "data".to_string(),
                    authorized: true,
                })
            })
            .collect();
        let plan = RagDagPlan {
            dag_id: request.dag_id.clone(),
            kind: request.kind,
            nodes,
            edges,
            context,
        };
        Self::validate(&plan)?;
        Ok(plan)
    }

    /// Fail closed on any structural or declarative violation —
    /// `RagDagPlanner.validate` port, same check names.
    pub fn validate(plan: &RagDagPlan) -> Result<(), String> {
        if plan.dag_id.is_empty() {
            return Err("dag-id-required".into());
        }
        if plan.nodes.is_empty() {
            return Err("empty-dag".into());
        }
        if plan.context.cancel_requested {
            return Err("execution-cancelled".into());
        }

        let node_ids: Vec<&str> = plan.nodes.iter().map(|n| n.node_id.as_str()).collect();
        let known: HashSet<&str> = node_ids.iter().copied().collect();
        if node_ids.len() != known.len() {
            return Err("duplicate-node-id".into());
        }

        for n in &plan.nodes {
            let spec = node_spec(n.node_type);
            let missing: Vec<&str> = spec
                .required_inputs
                .iter()
                .copied()
                .filter(|k| !n.inputs.contains_key(*k))
                .collect();
            if !missing.is_empty() {
                return Err(format!(
                    "missing-required-input:{}:{}",
                    n.node_id,
                    missing.join(",")
                ));
            }
            if spec.permission_scope_required && plan.context.permission_scope.is_empty() {
                return Err(format!("permission-scope-required:{}", n.node_id));
            }
        }

        for e in &plan.edges {
            if !known.contains(e.source_id.as_str()) || !known.contains(e.target_id.as_str()) {
                return Err(format!("dangling-edge:{}->{}", e.source_id, e.target_id));
            }
            if e.source_id == e.target_id {
                return Err(format!("self-edge:{}", e.source_id));
            }
            if !e.authorized {
                return Err(format!(
                    "unauthorized-dependency:{}->{}",
                    e.source_id, e.target_id
                ));
            }
        }

        Self::assert_acyclic(plan)?;
        Self::assert_ordering(plan)?;
        Self::assert_bounded(plan)?;

        let present: HashSet<RagDagNodeType> = plan.nodes.iter().map(|n| n.node_type).collect();
        for required in kind_required_nodes(plan.kind) {
            if !present.contains(required) {
                return Err(format!(
                    "missing-required-node:{}:{}",
                    plan.kind.as_str(),
                    required.as_str()
                ));
            }
        }
        Ok(())
    }

    fn assert_acyclic(plan: &RagDagPlan) -> Result<(), String> {
        let mut indegree: HashMap<&str, usize> =
            plan.nodes.iter().map(|n| (n.node_id.as_str(), 0)).collect();
        let mut adjacency: HashMap<&str, Vec<&str>> = HashMap::new();
        for e in &plan.edges {
            adjacency
                .entry(e.source_id.as_str())
                .or_default()
                .push(e.target_id.as_str());
            *indegree.get_mut(e.target_id.as_str()).unwrap() += 1;
        }
        let mut queue: VecDeque<&str> = indegree
            .iter()
            .filter(|(_, d)| **d == 0)
            .map(|(id, _)| *id)
            .collect();
        let mut visited = 0usize;
        while let Some(id) = queue.pop_front() {
            visited += 1;
            if let Some(targets) = adjacency.get(id) {
                for t in targets {
                    let d = indegree.get_mut(t).unwrap();
                    *d -= 1;
                    if *d == 0 {
                        queue.push_back(t);
                    }
                }
            }
        }
        if visited != plan.nodes.len() {
            return Err("dag-cycle".into());
        }
        Ok(())
    }

    fn assert_ordering(plan: &RagDagPlan) -> Result<(), String> {
        for (source_type, target_type) in kind_ordering(plan.kind) {
            let sources: HashSet<&str> = plan
                .nodes
                .iter()
                .filter(|n| n.node_type == *source_type)
                .map(|n| n.node_id.as_str())
                .collect();
            for n in plan.nodes.iter().filter(|n| n.node_type == *target_type) {
                let incoming: HashSet<&str> = plan
                    .edges
                    .iter()
                    .filter(|e| e.target_id == n.node_id)
                    .map(|e| e.source_id.as_str())
                    .collect();
                if incoming.is_empty() || incoming.is_disjoint(&sources) {
                    return Err(format!(
                        "bypass-dependency:{}->{}",
                        source_type.as_str(),
                        target_type.as_str()
                    ));
                }
            }
        }
        Ok(())
    }

    fn assert_bounded(plan: &RagDagPlan) -> Result<(), String> {
        let b = plan.context.budgets;
        if b.max_steps < plan.nodes.len() {
            return Err("budget-steps-too-small".into());
        }
        if b.max_steps == 0 || b.max_steps > HARD_MAX_STEPS {
            return Err("budget-steps-out-of-envelope".into());
        }
        if b.max_seconds <= 0.0 || b.max_seconds > HARD_MAX_SECONDS {
            return Err("budget-seconds-out-of-envelope".into());
        }
        if !(0.0..=HARD_MAX_COST).contains(&b.max_cost) {
            return Err("budget-cost-out-of-envelope".into());
        }
        if b.max_rounds < 1 || b.max_rounds > HARD_MAX_ROUNDS {
            return Err("budget-rounds-out-of-envelope".into());
        }
        Ok(())
    }

    fn build_query(request: &RagDagPlanRequest, context: &RagDagExecutionContext) -> Vec<RagDagNode> {
        let rag_types = if request.rag_types.is_empty() {
            vec!["hybrid".to_string()]
        } else {
            request.rag_types.clone()
        };
        vec![
            node(
                "cache-lookup",
                RagDagNodeType::CacheLookup,
                inputs(&[(
                    "cache_key",
                    Value::String(format!("{}:{}", request.dag_id, request.module_ids.join(","))),
                )]),
                vec![],
            ),
            node(
                "cache-validate",
                RagDagNodeType::CacheValidate,
                inputs(&[(
                    "cache_entry",
                    Value::String("cache-lookup.cache_entry".into()),
                )]),
                vec!["cache-lookup".into()],
            ),
            node(
                "retrieval",
                RagDagNodeType::Retrieval,
                inputs(&[
                    ("query", Value::String("$query".into())),
                    (
                        "module_ids",
                        serde_json::json!(request.module_ids),
                    ),
                    ("scope", Value::String(context.permission_scope.clone())),
                    ("rag_types", serde_json::json!(rag_types)),
                ]),
                vec!["cache-validate".into()],
            ),
            node(
                "fusion",
                RagDagNodeType::Fusion,
                inputs(&[(
                    "candidate_sets",
                    serde_json::json!(["retrieval.candidates"]),
                )]),
                vec!["retrieval".into()],
            ),
            node(
                "rerank",
                RagDagNodeType::Rerank,
                inputs(&[
                    (
                        "fused_candidates",
                        Value::String("fusion.fused_candidates".into()),
                    ),
                    ("reranker_limit", serde_json::json!(20)),
                ]),
                vec!["fusion".into()],
            ),
            node(
                "context-build",
                RagDagNodeType::ContextBuild,
                inputs(&[
                    (
                        "reranked_candidates",
                        Value::String("rerank.reranked_candidates".into()),
                    ),
                    ("max_context_tokens", serde_json::json!(8000)),
                ]),
                vec!["rerank".into()],
            ),
            node(
                "model-inference",
                RagDagNodeType::ModelInference,
                inputs(&[
                    (
                        "context_text",
                        Value::String("context-build.context_text".into()),
                    ),
                    ("model_identity", Value::String("$model".into())),
                ]),
                vec!["context-build".into()],
            ),
            node(
                "citation-validation",
                RagDagNodeType::CitationValidation,
                inputs(&[
                    (
                        "answer_text",
                        Value::String("model-inference.answer_text".into()),
                    ),
                    ("citations", Value::String("$citations".into())),
                ]),
                vec!["model-inference".into()],
            ),
        ]
    }

    fn build_multi_rag(request: &RagDagPlanRequest, context: &RagDagExecutionContext) -> Vec<RagDagNode> {
        let rag_types = if request.rag_types.is_empty() {
            vec!["hybrid".to_string()]
        } else {
            request.rag_types.clone()
        };
        let retrieval_ids: Vec<String> = rag_types
            .iter()
            .map(|t| format!("retrieval-{}", t))
            .collect();
        let mut nodes = vec![
            node(
                "cache-lookup",
                RagDagNodeType::CacheLookup,
                inputs(&[(
                    "cache_key",
                    Value::String(format!("{}:{}", request.dag_id, request.module_ids.join(","))),
                )]),
                vec![],
            ),
            node(
                "cache-validate",
                RagDagNodeType::CacheValidate,
                inputs(&[(
                    "cache_entry",
                    Value::String("cache-lookup.cache_entry".into()),
                )]),
                vec!["cache-lookup".into()],
            ),
        ];
        for (t, id) in rag_types.iter().zip(retrieval_ids.iter()) {
            nodes.push(node(
                id,
                RagDagNodeType::Retrieval,
                inputs(&[
                    ("query", Value::String("$query".into())),
                    (
                        "module_ids",
                        serde_json::json!(request.module_ids),
                    ),
                    ("scope", Value::String(context.permission_scope.clone())),
                    ("rag_types", serde_json::json!([t])),
                ]),
                vec!["cache-validate".into()],
            ));
        }
        nodes.push(node(
            "fusion",
            RagDagNodeType::Fusion,
            inputs(&[(
                "candidate_sets",
                serde_json::json!(
                    retrieval_ids
                        .iter()
                        .map(|id| format!("{}.candidates", id))
                        .collect::<Vec<_>>()
                ),
            )]),
            retrieval_ids.clone(),
        ));
        nodes.push(node(
            "rerank",
            RagDagNodeType::Rerank,
            inputs(&[
                (
                    "fused_candidates",
                    Value::String("fusion.fused_candidates".into()),
                ),
                ("reranker_limit", serde_json::json!(20)),
            ]),
            vec!["fusion".into()],
        ));
        nodes.push(node(
            "context-build",
            RagDagNodeType::ContextBuild,
            inputs(&[
                (
                    "reranked_candidates",
                    Value::String("rerank.reranked_candidates".into()),
                ),
                ("max_context_tokens", serde_json::json!(8000)),
            ]),
            vec!["rerank".into()],
        ));
        nodes.push(node(
            "model-inference",
            RagDagNodeType::ModelInference,
            inputs(&[
                (
                    "context_text",
                    Value::String("context-build.context_text".into()),
                ),
                ("model_identity", Value::String("$model".into())),
            ]),
            vec!["context-build".into()],
        ));
        nodes.push(node(
            "citation-validation",
            RagDagNodeType::CitationValidation,
            inputs(&[
                (
                    "answer_text",
                    Value::String("model-inference.answer_text".into()),
                ),
                ("citations", Value::String("$citations".into())),
            ]),
            vec!["model-inference".into()],
        ));
        nodes
    }

    fn build_retrieval_chain(
        request: &RagDagPlanRequest,
        context: &RagDagExecutionContext,
    ) -> Vec<RagDagNode> {
        let rag_types = if request.rag_types.is_empty() {
            vec!["hybrid".to_string()]
        } else {
            request.rag_types.clone()
        };
        vec![
            node(
                "retrieval",
                RagDagNodeType::Retrieval,
                inputs(&[
                    ("query", Value::String("$query".into())),
                    (
                        "module_ids",
                        serde_json::json!(request.module_ids),
                    ),
                    ("scope", Value::String(context.permission_scope.clone())),
                    ("rag_types", serde_json::json!(rag_types)),
                ]),
                vec![],
            ),
            node(
                "fusion",
                RagDagNodeType::Fusion,
                inputs(&[(
                    "candidate_sets",
                    serde_json::json!(["retrieval.candidates"]),
                )]),
                vec!["retrieval".into()],
            ),
            node(
                "rerank",
                RagDagNodeType::Rerank,
                inputs(&[
                    (
                        "fused_candidates",
                        Value::String("fusion.fused_candidates".into()),
                    ),
                    ("reranker_limit", serde_json::json!(20)),
                ]),
                vec!["fusion".into()],
            ),
            node(
                "context-build",
                RagDagNodeType::ContextBuild,
                inputs(&[
                    (
                        "reranked_candidates",
                        Value::String("rerank.reranked_candidates".into()),
                    ),
                    ("max_context_tokens", serde_json::json!(8000)),
                ]),
                vec!["rerank".into()],
            ),
        ]
    }

    fn build_index(request: &RagDagPlanRequest, context: &RagDagExecutionContext) -> Vec<RagDagNode> {
        let _ = context;
        let resource_ids = if request.resource_ids.is_empty() {
            vec!["$resource".to_string()]
        } else {
            request.resource_ids.clone()
        };
        vec![
            node(
                "index",
                RagDagNodeType::Index,
                inputs(&[
                    ("resource_id", serde_json::json!(resource_ids)),
                    ("content_hash", Value::String("$content_hash".into())),
                    ("generation_id", Value::String("$generation".into())),
                ]),
                vec![],
            ),
            node(
                "publish-barrier",
                RagDagNodeType::PublishBarrier,
                inputs(&[
                    ("publish_target", Value::String("$generation".into())),
                    (
                        "verification_result",
                        Value::String("$index_receipt".into()),
                    ),
                ]),
                vec!["index".into()],
            ),
        ]
    }

    fn build_rebuild(request: &RagDagPlanRequest, context: &RagDagExecutionContext) -> Vec<RagDagNode> {
        let _ = context;
        let resource_ids = if request.resource_ids.is_empty() {
            vec!["$resource".to_string()]
        } else {
            request.resource_ids.clone()
        };
        vec![
            node(
                "index",
                RagDagNodeType::Index,
                inputs(&[
                    ("resource_id", serde_json::json!(resource_ids)),
                    ("content_hash", Value::String("$content_hash".into())),
                    ("generation_id", Value::String("$generation".into())),
                ]),
                vec![],
            ),
            node(
                "verification",
                RagDagNodeType::Verification,
                inputs(&[(
                    "verification_target",
                    Value::String("$generation".into()),
                )]),
                vec!["index".into()],
            ),
            node(
                "publish-barrier",
                RagDagNodeType::PublishBarrier,
                inputs(&[
                    ("publish_target", Value::String("$generation".into())),
                    (
                        "verification_result",
                        Value::String("verification.verification_result".into()),
                    ),
                ]),
                vec!["verification".into()],
            ),
        ]
    }

    fn build_repair(request: &RagDagPlanRequest, context: &RagDagExecutionContext) -> Vec<RagDagNode> {
        let _ = context;
        let target = if request.repair_target.is_empty() {
            "$target".to_string()
        } else {
            request.repair_target.clone()
        };
        vec![
            node(
                "repair",
                RagDagNodeType::Repair,
                inputs(&[
                    ("repair_target", Value::String(target.clone())),
                    ("repair_plan", Value::String("$plan".into())),
                ]),
                vec![],
            ),
            node(
                "verification",
                RagDagNodeType::Verification,
                inputs(&[("verification_target", Value::String(target.clone()))]),
                vec!["repair".into()],
            ),
            node(
                "publish-barrier",
                RagDagNodeType::PublishBarrier,
                inputs(&[
                    ("publish_target", Value::String(target)),
                    (
                        "verification_result",
                        Value::String("verification.verification_result".into()),
                    ),
                ]),
                vec!["verification".into()],
            ),
        ]
    }
}

// ---------------------------------------------------------------------
// Executor (executor.py)
// ---------------------------------------------------------------------

/// One handler invocation outcome — evidence keys are JSON so upstream
/// references (`"<node_id>.<key>"`) resolve uniformly.
pub struct NodeOutcome {
    pub ok: bool,
    pub error: String,
    pub evidence: Map<String, Value>,
}

impl NodeOutcome {
    pub fn ok(evidence: Map<String, Value>) -> Self {
        Self {
            ok: true,
            error: String::new(),
            evidence,
        }
    }
    pub fn fail(error: &str) -> Self {
        let mut evidence = Map::new();
        evidence.insert("error".into(), Value::String(error.to_string()));
        Self {
            ok: false,
            error: error.to_string(),
            evidence,
        }
    }
    pub fn cancelled() -> Self {
        let mut evidence = Map::new();
        evidence.insert("cancelled".into(), Value::Bool(true));
        Self {
            ok: false,
            error: "execution-cancelled".into(),
            evidence,
        }
    }
}

pub type NodeHandler = Arc<
    dyn Fn(&RagDagNode, &RagDagExecutionContext, &HashMap<String, Value>) -> NodeOutcome
        + Send
        + Sync,
>;

pub const DEFAULT_NODE_TIMEOUT_SECONDS: f64 = 30.0;

pub struct RagDagExecutor {
    handlers: HashMap<RagDagNodeType, NodeHandler>,
    node_timeout: Duration,
    retry_limit: usize,
}

impl RagDagExecutor {
    pub fn new(handlers: HashMap<RagDagNodeType, NodeHandler>, node_timeout_seconds: f64, retry_limit: usize) -> Self {
        Self {
            handlers,
            node_timeout: Duration::from_secs_f64(node_timeout_seconds.max(0.1)),
            retry_limit,
        }
    }

    /// Deterministic topological order — Kahn over declaration order.
    fn topological_order(plan: &RagDagPlan) -> Vec<RagDagNode> {
        let mut indegree: HashMap<&str, usize> =
            plan.nodes.iter().map(|n| (n.node_id.as_str(), 0)).collect();
        let mut adjacency: HashMap<&str, Vec<&str>> = HashMap::new();
        for e in &plan.edges {
            adjacency
                .entry(e.source_id.as_str())
                .or_default()
                .push(e.target_id.as_str());
            *indegree.get_mut(e.target_id.as_str()).unwrap() += 1;
        }
        let mut queue: VecDeque<&str> = plan
            .nodes
            .iter()
            .filter(|n| indegree.get(n.node_id.as_str()).copied().unwrap_or(0) == 0)
            .map(|n| n.node_id.as_str())
            .collect();
        let mut order: Vec<RagDagNode> = Vec::with_capacity(plan.nodes.len());
        while let Some(id) = queue.pop_front() {
            if let Some(n) = plan.node(id) {
                order.push(n.clone());
            }
            if let Some(targets) = adjacency.get(id) {
                for t in targets {
                    let d = indegree.get_mut(t).unwrap();
                    *d -= 1;
                    if *d == 0 {
                        queue.push_back(t);
                    }
                }
            }
        }
        order
    }

    /// Run one handler on a worker thread under the node timeout —
    /// a timed-out worker detaches (same contract as the Python pool:
    /// the run is cancelled, the stuck handler is never joined).
    fn run_with_timeout(
        &self,
        handler: &NodeHandler,
        node: &RagDagNode,
        context: &RagDagExecutionContext,
        upstream: &HashMap<String, Value>,
    ) -> Result<NodeOutcome, String> {
        let handler = handler.clone();
        let node = node.clone();
        let context = context.clone();
        let upstream = upstream.clone();
        let (tx, rx) = mpsc::channel::<NodeOutcome>();
        std::thread::spawn(move || {
            let outcome = handler(&node, &context, &upstream);
            let _ = tx.send(outcome);
        });
        match rx.recv_timeout(self.node_timeout) {
            Ok(outcome) => Ok(outcome),
            Err(mpsc::RecvTimeoutError::Timeout) => Err("node-timeout".into()),
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                Err("handler-panicked".into())
            }
        }
    }

    fn run_with_retries(
        &self,
        handler: &NodeHandler,
        node: &RagDagNode,
        context: &RagDagExecutionContext,
        upstream: &HashMap<String, Value>,
    ) -> (Option<NodeOutcome>, String, usize) {
        let mut attempts = 0usize;
        let mut last_error = String::new();
        while attempts <= self.retry_limit {
            attempts += 1;
            match self.run_with_timeout(handler, node, context, upstream) {
                Err(e) if e == "node-timeout" => {
                    return (None, "node-timeout".into(), attempts);
                }
                Err(e) => {
                    last_error = e;
                    continue;
                }
                Ok(outcome) => return (Some(outcome), String::new(), attempts),
            }
        }
        (
            None,
            if last_error.is_empty() {
                "handler-not-ok".to_string()
            } else {
                last_error
            },
            attempts,
        )
    }

    pub fn execute(&self, plan: &RagDagPlan) -> RagDagExecutionResult {
        let started = Instant::now();
        let mut upstream: HashMap<String, Value> = HashMap::new();
        let mut results: Vec<RagDagNodeResult> = Vec::new();
        let mut failure_reasons: Vec<String> = Vec::new();
        let mut compensations = Map::new();
        let mut execution_state = RagDagState::Succeeded;

        for n in Self::topological_order(plan) {
            if plan.context.cancelled()
                || started.elapsed().as_secs_f64() > plan.context.budgets.max_seconds
            {
                execution_state = RagDagState::Cancelled;
                failure_reasons.push("execution-cancelled".into());
                break;
            }

            let spec = node_spec(n.node_type);
            let handler = match self.handlers.get(&n.node_type) {
                Some(h) => h.clone(),
                None => {
                    results.push(RagDagNodeResult {
                        node_id: n.node_id.clone(),
                        node_type: n.node_type,
                        state: RagDagState::Quarantined,
                        attempts: 0,
                        latency_ms: 0,
                        evidence: Map::new(),
                        error: format!("unregistered-handler:{}", n.node_type.as_str()),
                    });
                    execution_state = RagDagState::Quarantined;
                    failure_reasons
                        .push(format!("unregistered-handler:{}", n.node_type.as_str()));
                    break;
                }
            };

            let node_started = Instant::now();
            let (outcome, error, attempts) =
                self.run_with_retries(&handler, &n, &plan.context, &upstream);
            let latency_ms = node_started.elapsed().as_millis() as i64;

            if plan.context.cancelled() {
                results.push(RagDagNodeResult {
                    node_id: n.node_id.clone(),
                    node_type: n.node_type,
                    state: RagDagState::Cancelled,
                    attempts,
                    latency_ms,
                    evidence: Map::new(),
                    error: "execution-cancelled".into(),
                });
                execution_state = RagDagState::Cancelled;
                failure_reasons.push(format!("execution-cancelled:{}", n.node_id));
                break;
            }

            if error == "node-timeout" {
                results.push(RagDagNodeResult {
                    node_id: n.node_id.clone(),
                    node_type: n.node_type,
                    state: RagDagState::Cancelled,
                    attempts,
                    latency_ms,
                    evidence: Map::new(),
                    error,
                });
                execution_state = RagDagState::Cancelled;
                failure_reasons.push(format!("node-timeout:{}", n.node_id));
                break;
            }

            let outcome = outcome.unwrap_or_else(|| NodeOutcome::fail(&error));
            let evidence = outcome.evidence;
            let missing: Vec<&str> = spec
                .required_evidence
                .iter()
                .copied()
                .filter(|k| !evidence.contains_key(*k))
                .collect();
            let outcome_error = {
                let e = evidence.get("error").and_then(Value::as_str).unwrap_or("");
                if !outcome.ok {
                    if !e.is_empty() {
                        e.to_string()
                    } else {
                        outcome.error.clone()
                    }
                } else {
                    String::new()
                }
            };
            if !outcome.ok || !missing.is_empty() {
                let reason = if !outcome_error.is_empty() {
                    outcome_error
                } else if !missing.is_empty() {
                    format!("evidence-incomplete:{}", missing.join(","))
                } else {
                    "handler-not-ok".to_string()
                };
                results.push(RagDagNodeResult {
                    node_id: n.node_id.clone(),
                    node_type: n.node_type,
                    state: RagDagState::Failed,
                    attempts,
                    latency_ms,
                    evidence,
                    error: reason.clone(),
                });
                execution_state = RagDagState::Failed;
                failure_reasons.push(format!("{}:{}", reason, n.node_id));
                break;
            }

            upstream.insert(
                n.node_id.clone(),
                Value::Object(evidence.clone()),
            );
            results.push(RagDagNodeResult {
                node_id: n.node_id.clone(),
                node_type: n.node_type,
                state: RagDagState::Succeeded,
                attempts,
                latency_ms,
                evidence,
                error: String::new(),
            });
        }

        // Compensation: no write-path callables are wired in ragd, so
        // any side-effecting success followed by failure marks the run
        // requires-reconcile (never silently compensated).
        if matches!(
            execution_state,
            RagDagState::Failed | RagDagState::Quarantined | RagDagState::Cancelled
        ) {
            for n in Self::topological_order(plan)
                .into_iter()
                .filter(|n| {
                    node_spec(n.node_type).side_effecting
                        && results.iter().any(|r| {
                            r.node_id == n.node_id && r.state == RagDagState::Succeeded
                        })
                })
                .rev()
            {
                compensations.insert(
                    n.node_id.clone(),
                    Value::String("no-compensation-registered".into()),
                );
                execution_state = RagDagState::RequiresReconcile;
            }
            if execution_state == RagDagState::RequiresReconcile {
                failure_reasons.push("compensation-requires-reconcile".into());
            }
        }

        let latency_ms = started.elapsed().as_millis() as i64;
        let digest_payload = serde_json::json!({
            "dag_id": plan.dag_id,
            "kind": plan.kind.as_str(),
            "state": execution_state.as_str(),
            "nodes": results.iter().map(|r| r.to_record()).collect::<Vec<_>>(),
            "compensations": compensations,
        });
        let evidence_digest =
            format!("{:x}", Sha256::digest(digest_payload.to_string().as_bytes()));

        RagDagExecutionResult {
            dag_id: plan.dag_id.clone(),
            kind: plan.kind,
            state: execution_state,
            execution_id: plan.context.execution_id.clone(),
            correlation_id: plan.context.correlation_id.clone(),
            node_results: results,
            latency_ms,
            evidence_digest,
            failure_reasons,
            compensations,
        }
    }
}

/// `execution_context` builder — bounded defaults inside the envelope.
#[allow(clippy::too_many_arguments)]
pub fn execution_context(
    execution_id: &str,
    correlation_id: &str,
    actor_id: &str,
    module_id: &str,
    request_id: &str,
    decision_id: &str,
    module_ids: Vec<String>,
    data_categories: Vec<String>,
    permission_scope: &str,
    budgets: Option<RagDagBudgets>,
) -> RagDagExecutionContext {
    RagDagExecutionContext {
        execution_id: execution_id.to_string(),
        correlation_id: correlation_id.to_string(),
        actor_id: actor_id.to_string(),
        module_id: module_id.to_string(),
        request_id: request_id.to_string(),
        decision_id: decision_id.to_string(),
        module_ids,
        data_categories,
        permission_scope: permission_scope.to_string(),
        budgets: budgets.unwrap_or_else(|| default_budgets(16, 60.0)),
        created_at: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0),
        cancel_requested: false,
        cancel_event: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx() -> RagDagExecutionContext {
        execution_context(
            "ex", "co", "a", "m1", "rq", "d",
            vec!["m1".into()], vec![], "read:rag", None,
        )
    }

    fn retrieval_chain_req() -> RagDagPlanRequest {
        RagDagPlanRequest {
            kind: RagDagKind::RetrievalChain,
            dag_id: "d1".into(),
            module_ids: vec!["m1".into()],
            data_categories: vec![],
            rag_types: vec!["hybrid".into()],
            resource_ids: vec![],
            repair_target: String::new(),
        }
    }

    fn evidence_map_t(pairs: &[(&str, Value)]) -> Map<String, Value> {
        pairs.iter().map(|(k, v)| (k.to_string(), v.clone())).collect()
    }

    fn ok_handler(map: Map<String, Value>) -> NodeHandler {
        Arc::new(move |_, _, _| NodeOutcome::ok(map.clone()))
    }

    fn all_handlers() -> HashMap<RagDagNodeType, NodeHandler> {
        let mut h: HashMap<RagDagNodeType, NodeHandler> = HashMap::new();
        h.insert(RagDagNodeType::Retrieval, ok_handler(evidence_map_t(&[
            ("candidates", Value::Array(vec![])), ("provenance", serde_json::json!({})),
        ])));
        h.insert(RagDagNodeType::Fusion, ok_handler(evidence_map_t(&[
            ("fused_candidates", Value::Array(vec![])), ("fusion_method", serde_json::json!("x")),
        ])));
        h.insert(RagDagNodeType::Rerank, ok_handler(evidence_map_t(&[
            ("reranked_candidates", Value::Array(vec![])), ("reranker_model", serde_json::json!("none")),
        ])));
        h.insert(RagDagNodeType::ContextBuild, ok_handler(evidence_map_t(&[
            ("context_text", serde_json::json!("ctx")), ("token_budget", serde_json::json!(8000)),
        ])));
        h
    }

    #[test]
    fn plan_validates_retrieval_chain() {
        let plan = RagDagPlanner::plan(&retrieval_chain_req(), ctx()).unwrap();
        assert_eq!(plan.nodes.len(), 4);
        assert_eq!(plan.edges.len(), 3);
    }

    #[test]
    fn plan_rejects_missing_required_node() {
        // Drop the context-build node + its edge: ordering passes (no
        // target of that type remains) but the required-type check
        // must still reject the chain.
        let mut plan = RagDagPlanner::plan(&retrieval_chain_req(), ctx()).unwrap();
        plan.edges.retain(|e| e.target_id != "context-build");
        plan.nodes.retain(|n| n.node_id != "context-build");
        let err = RagDagPlanner::validate(&plan).unwrap_err();
        assert!(err.starts_with("missing-required-node"), "{}", err);
    }

    #[test]
    fn plan_rejects_cycle() {
        let mut plan = RagDagPlanner::plan(&retrieval_chain_req(), ctx()).unwrap();
        plan.edges.push(RagDagEdge {
            source_id: "context-build".into(),
            target_id: "retrieval".into(),
            kind: "data".into(),
            authorized: true,
        });
        let err = RagDagPlanner::validate(&plan).unwrap_err();
        assert_eq!(err, "dag-cycle");
    }

    #[test]
    fn plan_rejects_budget_out_of_envelope() {
        let mut plan = RagDagPlanner::plan(&retrieval_chain_req(), ctx()).unwrap();
        plan.context.budgets.max_steps = HARD_MAX_STEPS + 1;
        let err = RagDagPlanner::validate(&plan).unwrap_err();
        assert_eq!(err, "budget-steps-out-of-envelope");
    }

    #[test]
    fn plan_rejects_bypass_ordering() {
        let mut plan = RagDagPlanner::plan(&retrieval_chain_req(), ctx()).unwrap();
        plan.edges.retain(|e| !(e.source_id == "fusion" && e.target_id == "rerank"));
        plan.edges.push(RagDagEdge {
            source_id: "retrieval".into(),
            target_id: "rerank".into(),
            kind: "data".into(),
            authorized: true,
        });
        let err = RagDagPlanner::validate(&plan).unwrap_err();
        assert!(err.starts_with("bypass-dependency"), "{}", err);
    }

    #[test]
    fn executor_succeeds_and_propagates_evidence() {
        let plan = RagDagPlanner::plan(&retrieval_chain_req(), ctx()).unwrap();
        let ex = RagDagExecutor::new(all_handlers(), 5.0, 0);
        let result = ex.execute(&plan);
        assert_eq!(result.state, RagDagState::Succeeded);
        assert_eq!(result.node_results.len(), 4);
        assert!(!result.evidence_digest.is_empty());
    }

    #[test]
    fn executor_fails_closed_on_handler_error() {
        let plan = RagDagPlanner::plan(&retrieval_chain_req(), ctx()).unwrap();
        let mut h = all_handlers();
        h.insert(RagDagNodeType::Fusion, Arc::new(|_, _, _| {
            NodeOutcome::fail("fusion:boom")
        }));
        let ex = RagDagExecutor::new(h, 5.0, 0);
        let result = ex.execute(&plan);
        assert_eq!(result.state, RagDagState::Failed);
        assert!(result.failure_reasons.iter().any(|r| r.contains("fusion:boom")));
        assert_eq!(result.node_results.len(), 2);
    }

    #[test]
    fn executor_fails_closed_on_missing_evidence() {
        let plan = RagDagPlanner::plan(&retrieval_chain_req(), ctx()).unwrap();
        let mut h = all_handlers();
        h.insert(RagDagNodeType::Fusion, ok_handler(evidence_map_t(&[
            ("fused_candidates", Value::Array(vec![])),
        ])));
        let ex = RagDagExecutor::new(h, 5.0, 0);
        let result = ex.execute(&plan);
        assert_eq!(result.state, RagDagState::Failed);
        assert!(result.failure_reasons.iter().any(|r| r.contains("evidence-incomplete")));
    }

    #[test]
    fn executor_cancels_on_node_timeout() {
        let plan = RagDagPlanner::plan(&retrieval_chain_req(), ctx()).unwrap();
        let mut h = all_handlers();
        h.insert(RagDagNodeType::Retrieval, Arc::new(|_, _, _| {
            std::thread::sleep(std::time::Duration::from_secs(2));
            NodeOutcome::ok(Map::new())
        }));
        let ex = RagDagExecutor::new(h, 0.2, 0);
        let result = ex.execute(&plan);
        assert_eq!(result.state, RagDagState::Cancelled);
        assert!(result.failure_reasons.iter().any(|r| r.starts_with("node-timeout")));
    }
}

//! CAG plane — native port of `core_system/rag/cag/` (A549).
//!
//! Cache-Augmented Generation: bounded L1-L3 caches behind the CAG
//! Gate. Every lookup runs the gate — a denied or expired entry is
//! never returned. Caches carry derived/cache authority only and are
//! never an official fact source (xstore stays canonical).

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum CacheLevel {
    L1,
    L2,
    L3,
    L4,
}

impl CacheLevel {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::L1 => "L1",
            Self::L2 => "L2",
            Self::L3 => "L3",
            Self::L4 => "L4",
        }
    }
    pub fn from_name(name: &str) -> Option<Self> {
        match name {
            "L1" | "l1" => Some(Self::L1),
            "L2" | "l2" => Some(Self::L2),
            "L3" | "l3" => Some(Self::L3),
            "L4" | "l4" => Some(Self::L4),
            _ => None,
        }
    }
    /// `IMPLEMENTED_CACHE_LEVELS` — L4 needs explicit approval.
    pub fn implemented(self) -> bool {
        !matches!(self, Self::L4)
    }
    fn cap(self) -> usize {
        match self {
            Self::L1 => 256,
            Self::L2 => 1024,
            Self::L3 => 4096,
            Self::L4 => 4096,
        }
    }
    fn ttl(self) -> f64 {
        match self {
            Self::L1 => 60.0,
            Self::L2 => 600.0,
            _ => 3600.0,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CacheAuthority {
    Derived,
    Cache,
}

impl CacheAuthority {
    fn as_str(self) -> &'static str {
        match self {
            Self::Derived => "derived",
            Self::Cache => "cache",
        }
    }
}

/// `CACHE_AUTHORITIES` — a cache is never canonical.
const CACHE_AUTHORITIES: [CacheAuthority; 2] =
    [CacheAuthority::Derived, CacheAuthority::Cache];

/// Canonical query normalization — NFKC + casefold + whitespace
/// collapse, the same key material the Python `normalize_query` used.
pub fn normalize_query(query: &str) -> String {
    let folded: String = query
        .nfkc()
        .collect::<String>()
        .trim()
        .to_lowercase();
    folded.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn digest_json(value: &Value) -> String {
    // serde_json::Map is BTreeMap-backed by default: keys serialize in
    // sorted order, matching json.dumps(sort_keys=True).
    let bytes = value.to_string();
    format!("{:x}", Sha256::digest(bytes.as_bytes()))
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CacheKey {
    pub tenant_id: String,
    pub module_id: String,
    pub identity_id: String,
    pub permission_scope: String,
    pub data_classification: String,
    pub query_normalized: String,
    pub model_version: String,
    pub policy_version: String,
    pub source_revision: String,
    #[serde(default)]
    pub rag_architectures: Vec<String>,
    #[serde(default = "default_generation_mode")]
    pub generation_mode: String,
    #[serde(default)]
    pub active_generation: String,
    #[serde(default)]
    pub embedding_model: String,
    #[serde(default)]
    pub embedding_dimension: i64,
    #[serde(default)]
    pub reranker_version: String,
    #[serde(default)]
    pub chunk_policy_version: String,
    #[serde(default)]
    pub context_builder_version: String,
}

fn default_generation_mode() -> String {
    "canonical".to_string()
}

impl CacheKey {
    pub fn to_record(&self) -> Value {
        serde_json::to_value(self).unwrap_or(Value::Null)
    }
    pub fn digest(&self) -> String {
        digest_json(&self.to_record())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CacheEntry {
    pub cache_id: String,
    pub key: CacheKey,
    pub level: CacheLevel,
    pub authority: CacheAuthority,
    pub payload: Value,
    pub payload_digest: String,
    pub created_at: f64,
    pub expires_at: f64,
    #[serde(default = "one")]
    pub version: i64,
    #[serde(default)]
    pub generation_id: String,
    #[serde(default)]
    pub invalidation_reason: String,
    #[serde(default)]
    pub evidence_ids: Vec<String>,
    #[serde(default)]
    pub resource_ids: Vec<String>,
    #[serde(default)]
    pub content_hashes: Vec<String>,
    #[serde(default)]
    pub source_versions: Vec<String>,
    #[serde(default)]
    pub context_hash: String,
    #[serde(default)]
    pub canonical_source: String,
    #[serde(default)]
    pub validated_at: f64,
    #[serde(default)]
    pub hit_count: i64,
    #[serde(default)]
    pub last_hit_at: f64,
    #[serde(default = "state_valid")]
    pub state: String,
}

fn one() -> i64 {
    1
}
fn state_valid() -> String {
    "VALID".to_string()
}

impl CacheEntry {
    pub fn is_expired(&self, now: f64) -> bool {
        now >= self.expires_at
    }
    /// Metadata record — the Python `to_record` shape (payload itself
    /// stays out of the record; integrity is carried by the digest).
    pub fn to_record(&self) -> Value {
        serde_json::json!({
            "cache_id": self.cache_id,
            "key": self.key.to_record(),
            "level": self.level.as_str(),
            "authority": self.authority.as_str(),
            "payload_digest": self.payload_digest,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "version": self.version,
            "generation_id": self.generation_id,
            "invalidation_reason": self.invalidation_reason,
            "evidence_ids": self.evidence_ids,
            "resource_ids": self.resource_ids,
            "content_hashes": self.content_hashes,
            "source_versions": self.source_versions,
            "context_hash": self.context_hash,
            "canonical_source": self.canonical_source,
            "validated_at": self.validated_at,
            "hit_count": self.hit_count,
            "last_hit_at": self.last_hit_at,
            "state": self.state,
        })
    }
}

#[derive(Debug, Clone, Default)]
pub struct CacheRequest {
    pub tenant_id: String,
    pub module_ids: Vec<String>,
    pub identity_id: String,
    pub permission_scope: String,
    pub data_classification: String,
    pub query: String,
    pub model_version: String,
    pub policy_version: String,
    pub source_revision: String,
    pub level: CacheLevel,
    pub level4_approved: bool,
    pub rag_architectures: Vec<String>,
    pub generation_mode: String,
    pub active_generation: String,
    pub embedding_model: String,
    pub embedding_dimension: i64,
    pub reranker_version: String,
    pub chunk_policy_version: String,
    pub context_builder_version: String,
}

impl Default for CacheLevel {
    fn default() -> Self {
        Self::L1
    }
}

impl CacheRequest {
    pub fn normalized_query(&self) -> String {
        normalize_query(&self.query)
    }
}

#[derive(Debug, Clone)]
pub struct CacheGateDecision {
    pub allowed: bool,
    pub reason: String,
    pub checks: BTreeMap<String, bool>,
}

impl CacheGateDecision {
    pub fn to_record(&self) -> Value {
        serde_json::json!({
            "allowed": self.allowed,
            "reason": self.reason,
            "checks": self.checks,
        })
    }
}

/// Mandatory validation for every cache hit — any failed check denies
/// the hit fail-closed. Check names/order mirror `gate.py`.
pub struct CagGate {
    level4_approved_modules: std::collections::HashSet<String>,
}

impl CagGate {
    pub fn new(level4_approved_modules: &[String]) -> Self {
        Self {
            level4_approved_modules: level4_approved_modules.iter().cloned().collect(),
        }
    }

    pub fn validate(
        &self,
        entry: &CacheEntry,
        request: &CacheRequest,
        now: f64,
    ) -> CacheGateDecision {
        let mut checks: BTreeMap<String, bool> = BTreeMap::new();

        checks.insert(
            "level-implemented".into(),
            entry.level.implemented()
                || (entry.level == CacheLevel::L4
                    && (request.level4_approved
                        || self
                            .level4_approved_modules
                            .contains(&entry.key.module_id))),
        );
        checks.insert("level-match".into(), entry.level == request.level);
        checks.insert(
            "scope".into(),
            request.module_ids.iter().any(|m| m == &entry.key.module_id),
        );
        checks.insert(
            "permission".into(),
            scope_covers(&request.permission_scope, &entry.key.permission_scope),
        );
        checks.insert(
            "tenant".into(),
            entry.key.tenant_id == request.tenant_id,
        );
        checks.insert(
            "identity".into(),
            entry.key.identity_id == request.identity_id,
        );
        checks.insert(
            "data-classification".into(),
            entry.key.data_classification == request.data_classification,
        );
        checks.insert(
            "query-normalization".into(),
            entry.key.query_normalized == request.normalized_query(),
        );
        checks.insert(
            "model-version".into(),
            entry.key.model_version == request.model_version,
        );
        checks.insert(
            "policy-version".into(),
            entry.key.policy_version == request.policy_version,
        );
        checks.insert(
            "revision".into(),
            entry.key.source_revision == request.source_revision,
        );
        checks.insert(
            "rag-architectures".into(),
            {
                let mut sorted = request.rag_architectures.clone();
                sorted.sort();
                entry.key.rag_architectures == sorted
            },
        );
        checks.insert(
            "generation-mode".into(),
            entry.key.generation_mode == request.generation_mode,
        );
        checks.insert(
            "active-generation".into(),
            entry.key.active_generation == request.active_generation,
        );
        checks.insert(
            "embedding-model".into(),
            entry.key.embedding_model == request.embedding_model,
        );
        checks.insert(
            "embedding-dimension".into(),
            entry.key.embedding_dimension == request.embedding_dimension,
        );
        checks.insert(
            "reranker-version".into(),
            entry.key.reranker_version == request.reranker_version,
        );
        checks.insert(
            "chunk-policy-version".into(),
            entry.key.chunk_policy_version == request.chunk_policy_version,
        );
        checks.insert(
            "context-builder-version".into(),
            entry.key.context_builder_version == request.context_builder_version,
        );
        checks.insert("expiry".into(), !entry.is_expired(now));
        checks.insert(
            "authority".into(),
            CACHE_AUTHORITIES.contains(&entry.authority),
        );
        checks.insert("state".into(), entry.state == "VALID");
        checks.insert(
            "payload-integrity".into(),
            entry.payload_digest == digest_json(&entry.payload),
        );

        for (check, passed) in &checks {
            if !passed {
                return CacheGateDecision {
                    allowed: false,
                    reason: format!("cache-denied:{}", check),
                    checks,
                };
            }
        }
        CacheGateDecision {
            allowed: true,
            reason: "cache-allowed".into(),
            checks,
        }
    }
}

/// `_scope_covers` — true when the request scope covers the entry's
/// stored scope ("a:*" covers "a:b").
fn scope_covers(request_scope: &str, entry_scope: &str) -> bool {
    let requested = request_scope.trim();
    let stored = entry_scope.trim();
    if requested.is_empty() || stored.is_empty() {
        return false;
    }
    if requested == stored {
        return true;
    }
    if let Some(prefix) = requested.strip_suffix(":*") {
        return stored.starts_with(&format!("{}:", prefix));
    }
    false
}

/// Insertion-ordered bounded map — OrderedDict semantics for LRU.
struct Lru {
    map: HashMap<String, CacheEntry>,
    order: VecDeque<String>,
}

impl Lru {
    fn new() -> Self {
        Self {
            map: HashMap::new(),
            order: VecDeque::new(),
        }
    }
    fn get(&self, k: &str) -> Option<&CacheEntry> {
        self.map.get(k)
    }
    fn insert(&mut self, k: String, e: CacheEntry) {
        if !self.map.contains_key(&k) {
            self.order.push_back(k.clone());
        }
        self.map.insert(k, e);
    }
    fn move_to_end(&mut self, k: &str) {
        if self.map.contains_key(k) {
            self.order.retain(|x| x != k);
            self.order.push_back(k.to_string());
        }
    }
    fn remove(&mut self, k: &str) -> Option<CacheEntry> {
        let out = self.map.remove(k);
        if out.is_some() {
            self.order.retain(|x| x != k);
        }
        out
    }
    fn pop_oldest(&mut self) -> Option<CacheEntry> {
        while let Some(k) = self.order.pop_front() {
            if let Some(e) = self.map.remove(&k) {
                return Some(e);
            }
        }
        None
    }
    fn len(&self) -> usize {
        self.map.len()
    }
    fn keys(&self) -> Vec<String> {
        self.map.keys().cloned().collect()
    }
}

/// `CagCacheStore` — bounded, gated, LRU-evicted store per level.
pub struct CagCacheStore {
    gate: CagGate,
    entries: [Lru; 4],
    #[allow(dead_code)]
    invalidations: Vec<Value>,
}

fn level_index(level: CacheLevel) -> usize {
    match level {
        CacheLevel::L1 => 0,
        CacheLevel::L2 => 1,
        CacheLevel::L3 => 2,
        CacheLevel::L4 => 3,
    }
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

impl CagCacheStore {
    pub fn new() -> Self {
        Self {
            gate: CagGate::new(&[]),
            entries: [Lru::new(), Lru::new(), Lru::new(), Lru::new()],
            invalidations: Vec::new(),
        }
    }

    pub fn key_for(request: &CacheRequest) -> CacheKey {
        let mut arches = request.rag_architectures.clone();
        arches.sort();
        CacheKey {
            tenant_id: request.tenant_id.clone(),
            module_id: request.module_ids.first().cloned().unwrap_or_default(),
            identity_id: request.identity_id.clone(),
            permission_scope: request.permission_scope.clone(),
            data_classification: request.data_classification.clone(),
            query_normalized: request.normalized_query(),
            model_version: request.model_version.clone(),
            policy_version: request.policy_version.clone(),
            source_revision: request.source_revision.clone(),
            rag_architectures: arches,
            generation_mode: if request.generation_mode.is_empty() {
                "canonical".to_string()
            } else {
                request.generation_mode.clone()
            },
            active_generation: request.active_generation.clone(),
            embedding_model: request.embedding_model.clone(),
            embedding_dimension: request.embedding_dimension,
            reranker_version: request.reranker_version.clone(),
            chunk_policy_version: request.chunk_policy_version.clone(),
            context_builder_version: request.context_builder_version.clone(),
        }
    }

    pub fn put(&mut self, request: &CacheRequest, payload: Value, generation_id: &str) -> CacheEntry {
        let now = now_secs();
        let key = Self::key_for(request);
        let digest = key.digest();
        let entry = CacheEntry {
            cache_id: format!("cag-{}-{}", &digest[..16.min(digest.len())], request.level.as_str()),
            key,
            level: request.level,
            authority: CacheAuthority::Cache,
            payload_digest: digest_json(&payload),
            payload,
            created_at: now,
            expires_at: now + request.level.ttl(),
            version: 1,
            generation_id: generation_id.to_string(),
            invalidation_reason: String::new(),
            evidence_ids: Vec::new(),
            resource_ids: Vec::new(),
            content_hashes: Vec::new(),
            source_versions: Vec::new(),
            context_hash: String::new(),
            canonical_source: String::new(),
            validated_at: now,
            hit_count: 0,
            last_hit_at: 0.0,
            state: state_valid(),
        };
        let bucket = &mut self.entries[level_index(request.level)];
        bucket.insert(digest.clone(), entry.clone());
        bucket.move_to_end(&digest);
        while bucket.len() > request.level.cap() {
            bucket.pop_oldest();
        }
        entry
    }

    /// Every lookup runs the gate; a denied entry is evicted, never
    /// returned.
    pub fn get(&mut self, request: &CacheRequest) -> (Option<CacheEntry>, CacheGateDecision) {
        let key_digest = Self::key_for(request).digest();
        let bucket = &mut self.entries[level_index(request.level)];
        let entry = match bucket.get(&key_digest) {
            Some(e) => e.clone(),
            None => {
                return (
                    None,
                    CacheGateDecision {
                        allowed: false,
                        reason: "cache-miss".into(),
                        checks: BTreeMap::new(),
                    },
                )
            }
        };
        let decision = self.gate.validate(&entry, request, now_secs());
        if !decision.allowed {
            bucket.remove(&key_digest);
            return (None, decision);
        }
        let mut refreshed = entry.clone();
        refreshed.hit_count += 1;
        refreshed.last_hit_at = now_secs();
        bucket.insert(key_digest.clone(), refreshed.clone());
        bucket.move_to_end(&key_digest);
        (Some(refreshed), decision)
    }

    /// Contract surface (store.py parity) — used by callers that hold
    /// a request record; ragd's read path currently invalidates by
    /// module sweep only.
    #[allow(dead_code)]
    pub fn invalidate(&mut self, request: &CacheRequest, reason: &str) -> bool {
        let key_digest = Self::key_for(request).digest();
        let bucket = &mut self.entries[level_index(request.level)];
        match bucket.remove(&key_digest) {
            Some(entry) => {
                self.record_invalidation(&entry, reason);
                true
            }
            None => false,
        }
    }

    /// Contract surface — module-wide invalidation sweep (tombstone/
    /// revision-change propagation per A549).
    #[allow(dead_code)]
    pub fn invalidate_module(&mut self, module_id: &str, reason: &str) -> usize {
        let mut evicted: Vec<CacheEntry> = Vec::new();
        for bucket in self.entries.iter_mut() {
            for k in bucket.keys() {
                if bucket.get(&k).map(|e| e.key.module_id.as_str()) == Some(module_id) {
                    if let Some(e) = bucket.remove(&k) {
                        evicted.push(e);
                    }
                }
            }
        }
        let removed = evicted.len();
        for e in &evicted {
            self.record_invalidation(e, reason);
        }
        removed
    }

    fn record_invalidation(&mut self, entry: &CacheEntry, reason: &str) {
        self.invalidations.push(serde_json::json!({
            "cache_id": entry.cache_id,
            "module_id": entry.key.module_id,
            "level": entry.level.as_str(),
            "reason": reason,
            "at": now_secs(),
        }));
        if self.invalidations.len() > 100 {
            let excess = self.invalidations.len() - 100;
            self.invalidations.drain(..excess);
        }
    }

    /// Contract surface — periodic expiry sweep; lazy eviction on
    /// `get` already guards every served entry.
    #[allow(dead_code)]
    pub fn purge_expired(&mut self) -> usize {
        let now = now_secs();
        let mut removed = 0;
        for bucket in self.entries.iter_mut() {
            for k in bucket.keys() {
                if bucket.get(&k).map(|e| e.is_expired(now)) == Some(true) {
                    bucket.remove(&k);
                    removed += 1;
                }
            }
        }
        removed
    }

    pub fn stats(&self) -> Value {
        let now = now_secs();
        let mut levels = serde_json::Map::new();
        let mut total = 0usize;
        let mut expired = 0usize;
        for (i, name) in ["L1", "L2", "L3", "L4"].iter().enumerate() {
            let bucket = &self.entries[i];
            let exp = bucket
                .keys()
                .iter()
                .filter(|k| bucket.get(k).map(|e| e.is_expired(now)) == Some(true))
                .count();
            levels.insert(
                name.to_string(),
                serde_json::json!({"entries": bucket.len(), "expired": exp}),
            );
            total += bucket.len();
            expired += exp;
        }
        serde_json::json!({
            "levels": levels,
            "total_entries": total,
            "expired_entries": expired,
            "implemented_levels": ["L1", "L2", "L3"],
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn request() -> CacheRequest {
        CacheRequest {
            tenant_id: "t1".into(),
            module_ids: vec!["m1".into()],
            identity_id: "i1".into(),
            permission_scope: "read:rag".into(),
            data_classification: "public".into(),
            query: "  Hello   World ".into(),
            model_version: "m".into(),
            policy_version: "p".into(),
            source_revision: "r".into(),
            level: CacheLevel::L1,
            level4_approved: false,
            rag_architectures: vec!["hybrid".into()],
            generation_mode: "canonical".into(),
            active_generation: String::new(),
            embedding_model: String::new(),
            embedding_dimension: 0,
            reranker_version: String::new(),
            chunk_policy_version: String::new(),
            context_builder_version: String::new(),
        }
    }

    #[test]
    fn normalize_collapses_case_and_space() {
        assert_eq!(normalize_query("  Hello   World\n"), "hello world");
    }

    #[test]
    fn hit_requires_gate() {
        let mut store = CagCacheStore::new();
        let req = request();
        store.put(&req, json!({"context_text": "c"}), "g1");
        let (entry, decision) = store.get(&req);
        assert!(decision.allowed);
        assert!(entry.is_some());
        // Wrong module -> gate denies via scope/key mismatch (miss).
        let mut other = request();
        other.module_ids = vec!["m2".into()];
        let (entry, decision) = store.get(&other);
        assert!(entry.is_none());
        assert!(!decision.allowed);
    }

    #[test]
    fn scope_covers_wildcard() {
        assert!(scope_covers("a:*", "a:b"));
        assert!(scope_covers("a:b", "a:b"));
        assert!(!scope_covers("a:b", "a:c"));
        assert!(!scope_covers("", "a:b"));
    }
}

//! kernels.rs — star-kernel-registry/v1 for the xcorpus data lane.
//!
//! Same contract as the xstore/trainer registries: governed inventory
//! of every compute kernel in this lane (the corpus pipeline stages),
//! single audited Rust implementation each, plus star-kernel-policy/v1
//! deny enforcement. Phase 1 = inventory + policy gate; dispatch
//! authority stays with the pipeline itself until phase 2.

use serde_json::{json, Value};

struct K {
    name: &'static str,
    category: &'static str,
    determinism: &'static str,
    dispatch: &'static str,
}

static KERNELS: &[K] = &[
    K { name: "registry-gate", category: "GATE", determinism: "exact",
        dispatch: "enabled + license + clearance check before any read" },
    K { name: "doc-scan", category: "SCAN", determinism: "exact",
        dispatch: "bounded file walk under --root; denied paths counted" },
    K { name: "doc-parse", category: "PARSE", determinism: "exact",
        dispatch: "per-doc decode + NFC normalize; cache-gated reparse" },
    K { name: "nfc-normalize", category: "PARSE", determinism: "exact",
        dispatch: "textutil NFC pass over every document body" },
    K { name: "minhash-dedup", category: "DEDUP", determinism: "exact",
        dispatch: "C108 exact-hash + MinHash near-dup merge" },
    K { name: "seq-pack", category: "PACK", determinism: "exact",
        dispatch: "fixed-length sequence packing; val split by ratio" },
    K { name: "manifest-emit", category: "EMIT", determinism: "exact",
        dispatch: "content-addressed dataset_version + counts JSON" },
    K { name: "tokenizer", category: "TOKENIZE", determinism: "exact",
        dispatch: "engine_tokenizer.h port; encode on ingest" },
];

/// Pipeline element names the `corpus` command dispatches to — a deny
/// on any element refuses the whole run (fail-closed).
pub fn corpus_kernels() -> &'static [&'static str] {
    static KS: &[&str] = &[
        "registry-gate", "doc-scan", "doc-parse", "nfc-normalize",
        "minhash-dedup", "seq-pack", "manifest-emit", "tokenizer",
    ];
    KS
}

pub struct Policy {
    pub loaded: bool,
    pub source: String,
    pub deny_kernels: Vec<String>,
    pub deny_variants: Vec<String>,
}

pub fn policy_load(path: &str) -> Result<Policy, String> {
    if path.is_empty() {
        return Ok(Policy { loaded: false, source: String::new(),
                           deny_kernels: vec![], deny_variants: vec![] });
    }
    let text = std::fs::read_to_string(path)
        .map_err(|e| format!("kernel-policy: unreadable {path}: {e}"))?;
    let v: Value = serde_json::from_str(&text)
        .map_err(|e| format!("kernel-policy: JSON_PARSE: {e}"))?;
    if v.get("format").and_then(|f| f.as_str())
        != Some("star-kernel-policy/v1")
    {
        return Err("kernel-policy: FORMAT_MISMATCH".into());
    }
    let arr = |k: &str| -> Vec<String> {
        v.get(k).and_then(|a| a.as_array()).map(|a| {
            a.iter().filter_map(|e| e.as_str().map(String::from))
                .collect()
        }).unwrap_or_default()
    };
    let enabled =
        v.get("enabled").and_then(|e| e.as_bool()).unwrap_or(true);
    if !enabled {
        return Ok(Policy { loaded: false, source: path.into(),
                           deny_kernels: vec![], deny_variants: vec![] });
    }
    Ok(Policy {
        loaded: true,
        source: path.into(),
        deny_kernels: arr("deny_kernels"),
        deny_variants: arr("deny_variants"),
    })
}

pub fn policy_path(arg: Option<&String>) -> String {
    if let Some(p) = arg {
        return p.clone();
    }
    std::env::var("XCT_KERNEL_POLICY").unwrap_or_default()
}

/// `corpus` is denied when ANY pipeline kernel it dispatches is denied.
pub fn policy_gate(pol: &Policy, cmd: &str) -> Result<(), String> {
    if !pol.loaded {
        return Ok(());
    }
    if cmd == "corpus" {
        for k in corpus_kernels() {
            if pol.deny_kernels.iter().any(|d| d == k) {
                return Err(format!("KERNEL_POLICY_DENIED: {k}"));
            }
        }
    }
    Ok(())
}

pub fn registry_emit(policy_arg: Option<&String>) -> Value {
    let path = policy_path(policy_arg);
    let (pol, perr) = match policy_load(&path) {
        Ok(p) => (Some(p), None),
        Err(e) => (None, Some(e)),
    };
    let kernels: Vec<Value> = KERNELS
        .iter()
        .map(|k| {
            let denied = pol
                .as_ref()
                .map(|p| p.deny_kernels.iter().any(|d| d == k.name))
                .unwrap_or(false);
            json!({
                "name": k.name,
                "category": k.category,
                "family": "data-plane",
                "determinism": k.determinism,
                "dispatch": k.dispatch,
                "active": if denied { "denied" } else { "rust-safe" },
                "variants": [{"id": "rust-safe", "requires": "",
                              "parity": "exact"}],
            })
        })
        .collect();
    json!({
        "format": "star-kernel-registry/v1",
        "lane": "xcorpus",
        "count": KERNELS.len(),
        "policy": {
            "source": pol.as_ref().map(|p| p.source.clone())
                .unwrap_or(path),
            "loaded": pol.as_ref().map(|p| p.loaded).unwrap_or(false),
            "deny_kernels": pol.as_ref()
                .map(|p| p.deny_kernels.clone()).unwrap_or_default(),
            "deny_variants": pol.as_ref()
                .map(|p| p.deny_variants.clone()).unwrap_or_default(),
            "error": perr,
        },
        "kernels": kernels,
    })
}

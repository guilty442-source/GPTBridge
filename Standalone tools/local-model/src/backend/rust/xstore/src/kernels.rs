//! kernels.rs ??star-kernel-registry for the xstore data/safety lane.
//!
//! The registry is the governed inventory of every compute kernel this
//! lane owns (same contract the C++ trainer emits for its TPU lanes).
//! Every entry here has exactly one implementation ??the audited Rust
//! path ??so `variants` documents capability, not dispatch. Phase 1
//! scope: inventory + star-kernel-policy deny enforcement; the
//! dispatch-authority phase is separate.

use serde_json::{json, Value};

struct K {
    name: &'static str,
    category: &'static str,
    determinism: &'static str,
    dispatch: &'static str,
}

static KERNELS: &[K] = &[
    K { name: "xcn1-parse", category: "PARSE", determinism: "exact",
        dispatch: "bounds-checked header + tensor table walk" },
    K { name: "xcn1-verify", category: "PARSE", determinism: "exact",
        dispatch: "full structural verify incl. payload extents" },
    K { name: "ckpt-diff", category: "DIFF", determinism: "exact",
        dispatch: "config parity + byte-exact payload compare" },
    K { name: "sha256", category: "HASH", determinism: "exact",
        dispatch: "single-shot sha256 over mapped bytes" },
    K { name: "store-put", category: "STORE", determinism: "exact",
        dispatch: "verify -> tmp spill -> fsync -> rehash -> rename" },
    K { name: "store-get", category: "STORE", determinism: "exact",
        dispatch: "resident rehash before export" },
    K { name: "store-verify", category: "STORE", determinism: "exact",
        dispatch: "rehash every object + walk receipt chain" },
    K { name: "audit-append", category: "AUDIT", determinism: "exact",
        dispatch: "hash-chained JSONL append with prev link" },
    K { name: "audit-verify", category: "AUDIT", determinism: "exact",
        dispatch: "chain walk; truncation/forgery flips ok" },
    K { name: "snapshot", category: "STORE", determinism: "exact",
        dispatch: "content-addressed dataset snapshot write" },
    K { name: "snapshot-verify", category: "STORE", determinism: "exact",
        dispatch: "manifest rehash + object presence" },
];

/// Command name -> registry kernel it dispatches to.
pub fn kernel_for(cmd: &str) -> Option<&'static str> {
    Some(match cmd {
        "ckpt-info" => "xcn1-parse",
        "ckpt-verify" => "xcn1-verify",
        "ckpt-diff" => "ckpt-diff",
        "hash" => "sha256",
        "put" => "store-put",
        "get" => "store-get",
        "verify-store" => "store-verify",
        "audit-append" => "audit-append",
        "audit-verify" => "audit-verify",
        "snapshot" => "snapshot",
        "snapshot-verify" => "snapshot-verify",
        _ => return None,
    })
}

/// star-kernel-policy (minimal surface): format tag + enabled +
/// deny_kernels. A referenced but unreadable/malformed policy is a hard
/// failure ??callers propagate the Err verbatim.
pub struct Policy {
    pub loaded: bool,
    pub enabled: bool,
    pub force_serial: bool,
    pub max_threads: i64,
    pub source: String,
    pub deny_kernels: Vec<String>,
    pub deny_variants: Vec<String>,
}

impl Policy {
    fn empty() -> Self {
        Policy { loaded: false, enabled: true, force_serial: false,
                 max_threads: 0, source: String::new(),
                 deny_kernels: vec![], deny_variants: vec![] }
    }
}

pub fn policy_load(path: &str) -> Result<Policy, String> {
    if path.is_empty() {
        return Ok(Policy::empty());
    }
    let text = std::fs::read_to_string(path)
        .map_err(|e| format!("kernel-policy: unreadable {path}: {e}"))?;
    let v: Value = serde_json::from_str(&text)
        .map_err(|e| format!("kernel-policy: JSON_PARSE: {e}"))?;
    if v.get("format").and_then(|f| f.as_str())
        != Some("star-kernel-policy")
    {
        return Err("kernel-policy: FORMAT_MISMATCH".into());
    }
    let arr = |k: &str| -> Vec<String> {
        v.get(k).and_then(|a| a.as_array()).map(|a| {
            a.iter().filter_map(|e| e.as_str().map(String::from))
                .collect()
        }).unwrap_or_default()
    };
    let mut pol = Policy {
        loaded: true,
        enabled: v.get("enabled").and_then(|e| e.as_bool())
            .unwrap_or(true),
        force_serial: v.get("force_serial").and_then(|e| e.as_bool())
            .unwrap_or(false),
        max_threads: v.get("max_threads").and_then(|e| e.as_i64())
            .unwrap_or(0),
        source: path.into(),
        deny_kernels: arr("deny_kernels"),
        deny_variants: arr("deny_variants"),
    };
    if !pol.enabled {
        pol.loaded = false;
        pol.deny_kernels.clear();
        pol.deny_variants.clear();
    }
    Ok(pol)
}

/// Policy path precedence: explicit --policy arg, else
/// XCT_KERNEL_POLICY env, else none.
pub fn policy_path(arg: Option<&String>) -> String {
    if let Some(p) = arg {
        return p.clone();
    }
    std::env::var("XCT_KERNEL_POLICY").unwrap_or_default()
}

/// Fail-closed deny check for a command's kernel. Returns Err with the
/// KERNEL_POLICY_DENIED code ??callers surface it as XSTORE_FAILED.
pub fn policy_gate(pol: &Policy, cmd: &str) -> Result<(), String> {
    if !pol.loaded {
        return Ok(());
    }
    if let Some(k) = kernel_for(cmd) {
        if pol.deny_kernels.iter().any(|d| d == k) {
            return Err(format!("KERNEL_POLICY_DENIED: {k}"));
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
        "format": "star-kernel-registry",
        "lane": "xstore",
        "count": KERNELS.len(),
        "policy": {
            "source": pol.as_ref().map(|p| p.source.clone())
                .unwrap_or(path),
            "loaded": pol.as_ref().map(|p| p.loaded).unwrap_or(false),
            "enabled": pol.as_ref().map(|p| p.enabled).unwrap_or(true),
            "force_serial": pol.as_ref()
                .map(|p| p.force_serial).unwrap_or(false),
            "max_threads": pol.as_ref()
                .map(|p| p.max_threads).unwrap_or(0),
            "deny_kernels": pol.as_ref()
                .map(|p| p.deny_kernels.clone()).unwrap_or_default(),
            "deny_variants": pol.as_ref()
                .map(|p| p.deny_variants.clone()).unwrap_or_default(),
            "error": perr,
        },
        "kernels": kernels,
    })
}

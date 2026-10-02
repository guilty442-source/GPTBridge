//! failpool.rs — capability failure pool (star-capability-failure-pool/v1).
//!
//! Rust persisted-form port of C# FailurePool.cs (§29 + §55): per-class
//! bounded JSONL pools, exact + normalized fingerprints, dedup that
//! bumps seen_count in place, state transitions (OPEN/TRAINED/
//! RESOLVED/REGRESSED). Records are canonical-sorted JSON (serde_json
//! BTreeMap) so a Rust-written line is interchangeable with a
//! C#-written line — the pool dir format is unchanged.

use crate::hash;
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use unicode_categories::UnicodeCategories;

pub const FORMAT: &str = "star-capability-failure-pool/v1";
pub const MAX_ENTRIES: usize = 4096;

/// §7 vocabulary — identical spelling to FailurePool.Classes.
const CLASSES: [&str; 15] = [
    "instruction", "context", "multi-turn", "structured-output",
    "tool-call", "reading", "rag", "math", "coding", "vision",
    "system1", "thinking", "reasoning", "routing", "grounding",
];

const STATES: [&str; 4] = ["OPEN", "TRAINED", "RESOLVED", "REGRESSED"];

fn pool_path(pool_dir: &Path, cls: &str) -> PathBuf {
    pool_dir.join(format!("pool-{cls}.jsonl"))
}

fn iso_now() -> String {
    use time::macros::format_description;
    time::OffsetDateTime::now_utc()
        .format(format_description!(
            "[year]-[month]-[day]T[hour]:[minute]:[second]Z"
        ))
        .unwrap_or_default()
}

/// sha256(utf8 input)[:16] lower-hex — FailurePool.Fingerprint.
pub fn fingerprint(input: &str) -> String {
    hash::sha256_hex(input.as_bytes())[..16].to_string()
}

/// §11 normalized fingerprint: full lowercase, whitespace runs fold to
/// a single space, Unicode punctuation and symbols dropped — matches
/// FailurePool.NormFingerprint (C# iterates UTF-16 units; a surrogate
/// pair is neither punctuation nor symbol and is appended whole —
/// iterating chars() on the lowercased String emits identical bytes).
pub fn norm_fingerprint(input: &str) -> String {
    let mut sb = String::with_capacity(input.len());
    let mut ws = false;
    for ch in input.to_lowercase().chars() {
        if ch.is_whitespace() {
            ws = true;
            continue;
        }
        if ch.is_punctuation() || ch.is_symbol() {
            continue;
        }
        if ws && !sb.is_empty() {
            sb.push(' ');
        }
        ws = false;
        sb.push(ch);
    }
    hash::sha256_hex(sb.as_bytes())[..16].to_string()
}

fn read_lines(path: &Path) -> Vec<String> {
    std::fs::read_to_string(path)
        .map(|t| t.lines().map(|l| l.to_string()).collect())
        .unwrap_or_default()
}

fn write_lines(path: &Path, lines: &[String]) -> Result<(), String> {
    let mut txt = lines.join("\n");
    if !txt.is_empty() {
        txt.push('\n');
    }
    std::fs::write(path, txt).map_err(|e| format!("POOL_WRITE: {e}"))
}

pub struct RecordArgs {
    pub failure_class: String,
    pub input: String,
    pub generation: String,
    pub expected: String,
    pub actual: String,
    pub evidence: String,
    pub severity: String,
    pub reproducible: bool,
    pub reason: String,
    pub model_version: Option<String>,
    pub runtime_version: Option<String>,
    pub provenance: String,
}

/// Append one failure record with §11 dedup — a fingerprint or
/// norm-fingerprint repeat bumps seen_count/last_seen in place (and
/// upgrades severity on "high"). Returns the record as stored.
pub fn record(pool_dir: &Path, a: RecordArgs) -> Result<Value, String> {
    let cls = if CLASSES.contains(&a.failure_class.as_str()) {
        a.failure_class.clone()
    } else {
        "reasoning".to_string()
    };
    let fp = fingerprint(&a.input);
    let nfp = norm_fingerprint(&a.input);
    let now = iso_now();
    std::fs::create_dir_all(pool_dir)
        .map_err(|e| format!("POOL_WRITE: {e}"))?;
    let path = pool_path(pool_dir, &cls);
    let mut lines = read_lines(&path);

    // Dedup pass — exact OR normalized fingerprint hit.
    for i in 0..lines.len() {
        if !lines[i].trim_start().starts_with('{') {
            continue;
        }
        let Ok(old) = serde_json::from_str::<Value>(&lines[i]) else {
            continue;
        };
        let ofp = old.get("input_fingerprint").and_then(|x| x.as_str());
        let onfp =
            old.get("norm_fingerprint").and_then(|x| x.as_str());
        if ofp != Some(fp.as_str()) && onfp != Some(nfp.as_str()) {
            continue;
        }
        let mut o = match old {
            Value::Object(m) => m,
            _ => continue,
        };
        let seen = o
            .get("seen_count")
            .and_then(|x| x.as_i64())
            .unwrap_or(1)
            + 1;
        o.insert("seen_count".into(), json!(seen));
        o.insert("last_seen".into(), json!(now));
        if a.severity == "high" {
            o.insert("severity".into(), json!("high"));
        }
        let new_line = serde_json::to_string(&Value::Object(o))
            .map_err(|e| format!("POOL_WRITE: {e}"))?;
        lines[i] = new_line.clone();
        write_lines(&path, &lines)?;
        let mut rec = serde_json::from_str::<Value>(&new_line)
            .map_err(|e| format!("POOL_WRITE: {e}"))?;
        rec["dedup"] = json!("repeat");
        return Ok(rec);
    }

    let mut m = Map::new();
    m.insert("format".into(), json!(FORMAT));
    m.insert("input_fingerprint".into(), json!(fp));
    m.insert("norm_fingerprint".into(), json!(nfp));
    m.insert("input".into(), json!(a.input));
    m.insert("generation".into(), json!(a.generation));
    m.insert(
        "model_version".into(),
        json!(a.model_version.unwrap_or_else(|| a.generation.clone())),
    );
    m.insert(
        "runtime_version".into(),
        json!(a
            .runtime_version
            .unwrap_or_else(|| "xc-native-cpp23".into())),
    );
    m.insert("failure_class".into(), json!(cls));
    m.insert("failure_reason".into(), json!(a.reason));
    m.insert("expected".into(), json!(a.expected));
    m.insert("actual".into(), json!(a.actual));
    m.insert("evidence".into(), json!(a.evidence));
    m.insert("provenance".into(), json!(a.provenance));
    m.insert("severity".into(), json!(a.severity));
    m.insert("reproducible".into(), json!(a.reproducible));
    m.insert("state".into(), json!("OPEN"));
    m.insert("seen_count".into(), json!(1));
    m.insert("recorded_at".into(), json!(now));
    m.insert("last_seen".into(), json!(now));
    let rec = Value::Object(m);
    let line = serde_json::to_string(&rec)
        .map_err(|e| format!("POOL_WRITE: {e}"))?;
    lines.push(line);
    // Bounded pool — keep the newest records.
    if lines.len() > MAX_ENTRIES {
        let keep = lines.split_off(lines.len() - MAX_ENTRIES);
        lines = keep;
    }
    write_lines(&path, &lines)?;
    Ok(rec)
}

/// Read every record in one class pool.
pub fn list(pool_dir: &Path, cls: &str) -> Result<Value, String> {
    let path = pool_path(pool_dir, cls);
    let mut rows = Vec::new();
    for line in read_lines(&path) {
        if !line.trim_start().starts_with('{') {
            continue;
        }
        if let Ok(v) = serde_json::from_str::<Value>(&line) {
            rows.push(v);
        }
    }
    Ok(json!({
        "format": "xstore-fail-list/v1",
        "failure_class": cls,
        "count": rows.len(),
        "records": rows,
    }))
}

/// §55 state transition on a fingerprint — find the record and set
/// `state`; REJECTED transitions keep fail-closed semantics.
pub fn mark(
    pool_dir: &Path,
    cls: &str,
    fp: &str,
    state: &str,
) -> Result<Value, String> {
    if !STATES.contains(&state) {
        return Err(format!("POOL_STATE_UNKNOWN: {state}"));
    }
    let path = pool_path(pool_dir, cls);
    let mut lines = read_lines(&path);
    let now = iso_now();
    let mut hit = false;
    for line in lines.iter_mut() {
        let Ok(v) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if v.get("input_fingerprint").and_then(|x| x.as_str())
            != Some(fp)
            && v.get("norm_fingerprint").and_then(|x| x.as_str())
                != Some(fp)
        {
            continue;
        }
        let Value::Object(mut o) = v else { continue };
        o.insert("state".into(), json!(state));
        o.insert("last_seen".into(), json!(now));
        *line = serde_json::to_string(&Value::Object(o))
            .map_err(|e| format!("POOL_WRITE: {e}"))?;
        hit = true;
    }
    if hit {
        write_lines(&path, &lines)?;
    }
    Ok(json!({
        "format": "xstore-fail-mark/v1",
        "ok": hit,
        "failure_class": cls,
        "fingerprint": fp,
        "state": state,
    }))
}

/// Pool status — same shape as FailurePool.Status.
pub fn status(pool_dir: &Path) -> Result<Value, String> {
    let mut by_class = Map::new();
    for c in CLASSES {
        by_class.insert(c.to_string(), json!(0));
    }
    let mut by_state = Map::new();
    for s in STATES {
        by_state.insert(s.to_string(), json!(0));
    }
    let mut total = 0u64;
    let mut repeats = 0u64;
    if pool_dir.is_dir() {
        for ent in std::fs::read_dir(pool_dir)
            .map_err(|e| format!("POOL_READ: {e}"))?
            .flatten()
        {
            let name = ent.file_name().to_string_lossy().into_owned();
            if !(name.starts_with("pool-") && name.ends_with(".jsonl"))
            {
                continue;
            }
            for line in read_lines(&ent.path()) {
                if !line.trim_start().starts_with('{') {
                    continue;
                }
                let Ok(v) = serde_json::from_str::<Value>(&line) else {
                    continue;
                };
                if let Some(c) =
                    v.get("failure_class").and_then(|x| x.as_str())
                {
                    if let Some(slot) = by_class.get_mut(c) {
                        *slot = json!(slot.as_u64().unwrap_or(0) + 1);
                    }
                }
                if let Some(s) =
                    v.get("state").and_then(|x| x.as_str())
                {
                    if let Some(slot) = by_state.get_mut(s) {
                        *slot = json!(slot.as_u64().unwrap_or(0) + 1);
                    }
                }
                if let Some(n) =
                    v.get("seen_count").and_then(|x| x.as_i64())
                {
                    if n > 1 {
                        repeats += (n - 1) as u64;
                    }
                }
                total += 1;
            }
        }
    }
    Ok(json!({
        "ok": true,
        "format": FORMAT,
        "entries": total,
        "repeat_observations": repeats,
        "by_class": by_class,
        "by_state": by_state,
        "per_capability_pools": true,
        "bounded": MAX_ENTRIES,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "xstore-failpool-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn args(input: &str, cls: &str) -> RecordArgs {
        RecordArgs {
            failure_class: cls.into(),
            input: input.into(),
            generation: "g1".into(),
            expected: "ok".into(),
            actual: "bad".into(),
            evidence: "eval".into(),
            severity: "medium".into(),
            reproducible: true,
            reason: "".into(),
            model_version: None,
            runtime_version: None,
            provenance: "".into(),
        }
    }

    #[test]
    fn fingerprints_match_csharp_semantics() {
        assert_eq!(fingerprint("abc").len(), 16);
        // Whitespace folds; punctuation/symbols drop entirely.
        assert_eq!(
            norm_fingerprint("  Hello,  World!! "),
            norm_fingerprint("hello world")
        );
        assert_ne!(
            norm_fingerprint("hello world"),
            norm_fingerprint("hello there")
        );
        // Case-insensitive.
        assert_eq!(
            norm_fingerprint("ABC"),
            norm_fingerprint("abc")
        );
    }

    #[test]
    fn record_dedup_bumps_seen_count() {
        let root = scratch();
        let pool = root.join("pool");
        let r1 = record(&pool, args("what is 2+2", "math")).unwrap();
        assert_eq!(r1["seen_count"], 1);
        assert_eq!(r1["state"], "OPEN");
        // Same input → repeat.
        let r2 = record(&pool, args("what is 2+2", "math")).unwrap();
        assert_eq!(r2["dedup"], "repeat");
        assert_eq!(r2["seen_count"], 2);
        // Normalized-only variant also dedups.
        let r3 = record(&pool, args("What is 2+2!", "math")).unwrap();
        assert_eq!(r3["dedup"], "repeat");
        assert_eq!(r3["seen_count"], 3);
        // Pool file still holds a single record.
        let l = list(&pool, "math").unwrap();
        assert_eq!(l["count"], 1);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn unknown_class_falls_back_to_reasoning() {
        let root = scratch();
        let pool = root.join("pool");
        let r = record(&pool, args("x", "not-a-class")).unwrap();
        assert_eq!(r["failure_class"], "reasoning");
        assert!(pool_path(&pool, "reasoning").exists());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn mark_transitions_state() {
        let root = scratch();
        let pool = root.join("pool");
        let r = record(&pool, args("q", "coding")).unwrap();
        let fp = r["input_fingerprint"].as_str().unwrap().to_string();
        let m = mark(&pool, "coding", &fp, "TRAINED").unwrap();
        assert_eq!(m["ok"], true);
        let l = list(&pool, "coding").unwrap();
        assert_eq!(l["records"][0]["state"], "TRAINED");
        assert!(mark(&pool, "coding", &fp, "NOPE").is_err());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn status_counts_classes_and_repeats() {
        let root = scratch();
        let pool = root.join("pool");
        record(&pool, args("a", "math")).unwrap();
        record(&pool, args("a", "math")).unwrap(); // repeat
        record(&pool, args("b", "reading")).unwrap();
        let s = status(&pool).unwrap();
        assert_eq!(s["entries"], 2);
        assert_eq!(s["repeat_observations"], 1);
        assert_eq!(s["by_class"]["math"], 1);
        assert_eq!(s["by_class"]["reading"], 1);
        assert_eq!(s["by_state"]["OPEN"], 2);
        let _ = std::fs::remove_dir_all(&root);
    }
}

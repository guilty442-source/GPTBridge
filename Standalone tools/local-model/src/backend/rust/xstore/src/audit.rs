//! audit.rs — standalone immutable audit log (star-audit-log/v1).
//!
//! Append-only JSONL where every entry is
//!   {"format":"star-audit-log/v1","seq":N,"ts":<unix>,"prev":H,"data":{...}}
//! and `prev` is sha256 of the *previous line's raw bytes* (genesis:
//! 64 zero hex chars). Binding the chain to raw bytes — not a parsed
//! field — means tampering with any byte of entry k breaks the link
//! recorded in entry k+1. Append is O(1): only the tail line is read.
//!
//! Limit (documented, inherent): a lone log can hide *tail truncation*;
//! callers should anchor `head` into an external store (the artifact
//! store index, a DB row, a receipt) to close that gap.

use crate::hash;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Seek, SeekFrom, Write};
use std::path::Path;

const GENESIS: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";

/// Read the last non-empty line of the log (O(1)-ish: seeks backwards
/// in 8 KiB blocks) and return (seq, sha256(line_bytes)).
fn tail(log: &Path) -> Result<(u64, String), String> {
    let mut f = match std::fs::File::open(log) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok((0, GENESIS.to_string()));
        }
        Err(e) => return Err(format!("AUDIT_READ: {e}")),
    };
    let len = f.metadata().map_err(|e| format!("AUDIT_READ: {e}"))?.len();
    if len == 0 {
        return Ok((0, GENESIS.to_string()));
    }
    // Walk backwards until we capture the start of the last non-empty
    // line (or file start).
    let mut pos = len;
    let mut acc: Vec<u8> = Vec::new();
    loop {
        let take = pos.min(8192);
        pos -= take;
        f.seek(SeekFrom::Start(pos)).map_err(|e| format!("AUDIT_READ: {e}"))?;
        let mut blk = vec![0u8; take as usize];
        use std::io::Read;
        f.read_exact(&mut blk).map_err(|e| format!("AUDIT_READ: {e}"))?;
        blk.extend_from_slice(&acc);
        acc = blk;
        // find last '\n' that precedes a non-empty line
        while acc.last() == Some(&b'\n') {
            acc.pop();
        }
        if let Some(nl) = acc.iter().rposition(|&b| b == b'\n') {
            let line = &acc[nl + 1..];
            let v: Value = serde_json::from_slice(line)
                .map_err(|e| format!("AUDIT_TAIL_CORRUPT: {e}"))?;
            if v.get("format").and_then(|x| x.as_str())
                != Some("star-audit-log/v1")
            {
                return Err("AUDIT_TAIL_CORRUPT: bad format tag".into());
            }
            let seq = v.get("seq").and_then(|x| x.as_u64())
                .ok_or("AUDIT_TAIL_CORRUPT: missing seq")?;
            return Ok((seq, hash::sha256_hex(line)));
        }
        if pos == 0 {
            // Whole file is one line.
            let v: Value = serde_json::from_slice(&acc)
                .map_err(|e| format!("AUDIT_TAIL_CORRUPT: {e}"))?;
            let seq = v.get("seq").and_then(|x| x.as_u64())
                .ok_or("AUDIT_TAIL_CORRUPT: missing seq")?;
            return Ok((seq, hash::sha256_hex(&acc)));
        }
    }
}

/// Append one entry; returns the receipt (seq, sha256 of written line,
/// prev). `data` is embedded verbatim as the `data` field.
pub fn append(log: &Path, data: Value) -> Result<Value, String> {
    if let Some(p) = log.parent() {
        std::fs::create_dir_all(p).map_err(|e| format!("AUDIT_WRITE: {e}"))?;
    }
    let (seq, prev) = tail(log)?;
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let entry = json!({
        "format": "star-audit-log/v1",
        "seq": seq + 1,
        "ts": ts,
        "prev": prev,
        "data": data,
    });
    // Deterministic serialization, compact — the raw line is the
    // chained artifact so the writer controls its exact bytes.
    let line = serde_json::to_string(&entry)
        .map_err(|e| format!("AUDIT_WRITE: {e}"))?;
    let line_hash = hash::sha256_hex(line.as_bytes());
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log)
        .map_err(|e| format!("AUDIT_WRITE: {e}"))?;
    writeln!(f, "{line}").map_err(|e| format!("AUDIT_WRITE: {e}"))?;
    f.sync_all().map_err(|e| format!("AUDIT_WRITE: {e}"))?;
    Ok(json!({
        "format": "xstore-audit-append/v1",
        "ok": true,
        "seq": seq + 1,
        "sha256": line_hash,
        "prev": prev,
        "log": log.to_string_lossy(),
    }))
}

/// Verify the whole chain: format tag, seq strictly increasing from 1,
/// and prev == sha256(previous raw line). Returns the report.
pub fn verify(log: &Path) -> Result<Value, String> {
    let f = match std::fs::File::open(log) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok(json!({
                "format": "xstore-audit-verify/v1",
                "ok": true,
                "entries": 0,
                "head": GENESIS,
                "log": log.to_string_lossy(),
            }));
        }
        Err(e) => return Err(format!("AUDIT_READ: {e}")),
    };
    let rdr = BufReader::new(f);
    let mut prev = GENESIS.to_string();
    let mut entries = 0u64;
    let mut first_bad: Option<u64> = None;
    let mut head = GENESIS.to_string();
    for (i, line) in rdr.lines().enumerate() {
        let raw = line.map_err(|e| format!("AUDIT_READ: {e}"))?;
        if raw.trim().is_empty() {
            continue;
        }
        let ln = (i + 1) as u64;
        let Ok(v) = serde_json::from_str::<Value>(&raw) else {
            if first_bad.is_none() {
                first_bad = Some(ln);
            }
            continue;
        };
        let good_tag = v.get("format").and_then(|x| x.as_str())
            == Some("star-audit-log/v1");
        let seq = v.get("seq").and_then(|x| x.as_u64());
        let p = v.get("prev").and_then(|x| x.as_str()).unwrap_or("");
        let ok = good_tag
            && seq == Some(entries + 1)
            && p == prev;
        if !ok && first_bad.is_none() {
            first_bad = Some(ln);
        }
        if ok {
            entries += 1;
            prev = hash::sha256_hex(raw.as_bytes());
            head = prev.clone();
        }
    }
    Ok(json!({
        "format": "xstore-audit-verify/v1",
        "ok": first_bad.is_none(),
        "entries": entries,
        "head": head,
        "first_bad_line": first_bad,
        "log": log.to_string_lossy(),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch() -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "xstore-audit-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn append_verify_chain() {
        let root = scratch();
        let log = root.join("a.jsonl");
        let r1 = append(&log, json!({"op": "one"})).unwrap();
        assert_eq!(r1["seq"], 1);
        assert_eq!(r1["prev"], GENESIS);
        let r2 = append(&log, json!({"op": "two"})).unwrap();
        assert_eq!(r2["seq"], 2);
        assert_eq!(r2["prev"], r1["sha256"]);
        let v = verify(&log).unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["entries"], 2);
        assert_eq!(v["head"], r2["sha256"]);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn tampered_payload_breaks_chain() {
        let root = scratch();
        let log = root.join("a.jsonl");
        append(&log, json!({"op": "one"})).unwrap();
        append(&log, json!({"op": "two"})).unwrap();
        append(&log, json!({"op": "three"})).unwrap();
        // Rewrite entry 2's payload byte-wise (same length: two→six).
        let text = std::fs::read_to_string(&log).unwrap();
        let text = text.replacen("\"two\"", "\"six\"", 1);
        std::fs::write(&log, text).unwrap();
        let v = verify(&log).unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["first_bad_line"], 3); // entry 3's prev mismatches
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn forged_append_detected() {
        let root = scratch();
        let log = root.join("a.jsonl");
        append(&log, json!({"op": "one"})).unwrap();
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(&log)
            .unwrap();
        writeln!(
            f,
            "{{\"format\":\"star-audit-log/v1\",\"seq\":2,\"ts\":0,\"prev\":\"{}\",\"data\":{{}}}}",
            "f".repeat(64)
        )
        .unwrap();
        drop(f);
        let v = verify(&log).unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["first_bad_line"], 2);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn missing_log_verifies_empty() {
        let root = scratch();
        let v = verify(&root.join("nope.jsonl")).unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["entries"], 0);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn seq_reuse_rejected() {
        let root = scratch();
        let log = root.join("a.jsonl");
        append(&log, json!({"a": 1})).unwrap();
        // Append a syntactically valid entry with seq jumped ahead.
        let text = std::fs::read_to_string(&log).unwrap();
        let v: Value = serde_json::from_str(text.trim()).unwrap();
        let prev = hash::sha256_hex(text.trim().as_bytes());
        let mut forged = v.clone();
        forged["seq"] = json!(9);
        forged["prev"] = json!(prev);
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(&log)
            .unwrap();
        writeln!(f, "{}", serde_json::to_string(&forged).unwrap())
            .unwrap();
        drop(f);
        let r = verify(&log).unwrap();
        assert_eq!(r["ok"], false);
        let _ = std::fs::remove_dir_all(&root);
    }
}

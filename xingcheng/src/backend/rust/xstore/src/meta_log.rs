//! meta_log.rs — canonical append-only metadata event log (§5,§24,§26).
//!
//! Layout:
//!   <store>/metadata/events/events.jsonl   canonical hash-chained log
//!   <store>/metadata/audit/receipts.jsonl  mutation receipts (chained)
//!
//! Write order (§24): temp-free append — a whole transaction's lines are
//! staged in one buffer, written in a single write, fsynced, then the
//! receipt lands. A line is COMMITTED iff it parses and hash-verifies;
//! an unparseable or hash-broken tail (crashed write) is the
//! "incomplete tail" of §26 — readers ignore it and a writer truncates
//! it before appending (it was never committed state, so removing it is
//! not a rewrite of history). An invalid line followed by more lines is
//! mid-file corruption -> RECOVERY_REQUIRED (§84), never auto-repaired.

use crate::hash;
use crate::meta_types as mt;
use serde_json::{json, Value};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

pub fn events_path(store: &Path) -> PathBuf {
    store.join("metadata").join("events").join("events.jsonl")
}
pub fn receipts_path(store: &Path) -> PathBuf {
    store.join("metadata").join("audit").join("receipts.jsonl")
}

/// One committed event, parsed and hash-verified.
pub struct LogEvent {
    pub seq: u64,
    pub line_hash: String,
    pub value: Value,
}

/// Result of a full canonical scan.
pub struct Scan {
    pub events: Vec<LogEvent>,
    /// sha256 of the last committed raw line (GENESIS when empty).
    pub head_hash: String,
    /// bytes sitting past the committed prefix (torn tail).
    pub ignored_tail_bytes: u64,
    /// offset just past the last committed line — append/trunc point.
    pub committed_len: u64,
}

/// Full canonical scan. Every line must parse, hash-verify, chain
/// (previous_hash == sha256(prev raw line)) and seq-increment; the first
/// failure ends the committed prefix — acceptable only as the final
/// line (torn tail). Mid-file failure => Err(RECOVERY_REQUIRED).
pub fn scan(store: &Path) -> Result<Scan, String> {
    let path = events_path(store);
    let data = match std::fs::read(&path) {
        Ok(d) => d,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok(Scan {
                events: Vec::new(),
                head_hash: mt::GENESIS.into(),
                ignored_tail_bytes: 0,
                committed_len: 0,
            })
        }
        Err(e) => return Err(format!("META_LOG_READ: {e}")),
    };
    let mut events = Vec::new();
    let mut prev = mt::GENESIS.to_string();
    let mut expect_seq = 1u64;
    let mut offset = 0usize;
    let mut committed_len = 0usize;
    let mut iter = data.split(|b| *b == b'\n').peekable();
    while let Some(raw) = iter.next() {
        let line_end = offset + raw.len() + 1;
        offset = line_end;
        if raw.is_empty() {
            continue; // trailing newline / empty tail
        }
        let line = String::from_utf8_lossy(raw).into_owned();
        match mt::verify_event_line(&line).and_then(|v| {
            let ph = v
                .get("previous_hash")
                .and_then(|x| x.as_str())
                .unwrap_or("");
            if ph != prev {
                return Err("EVENT_CORRUPT: chain break".into());
            }
            let seq = v.get("seq").and_then(|x| x.as_u64()).unwrap_or(0);
            if seq != expect_seq {
                return Err("EVENT_CORRUPT: seq break".into());
            }
            Ok(v)
        }) {
            Ok(v) => {
                let lh = hash::sha256_hex(raw);
                prev = lh.clone();
                expect_seq += 1;
                committed_len = line_end;
                events.push(LogEvent { seq: v["seq"].as_u64().unwrap_or(0), line_hash: lh, value: v });
            }
            Err(e) => {
                let more = iter.any(|r| !r.is_empty());
                if more {
                    return Err(format!(
                        "XSTORE_RECOVERY_REQUIRED: mid-file corrupt event: {e}"
                    ));
                }
                return Ok(Scan {
                    events,
                    head_hash: prev,
                    ignored_tail_bytes: (data.len() - committed_len) as u64,
                    committed_len: committed_len as u64,
                });
            }
        }
    }
    let ignored = (data.len() - committed_len) as u64;
    Ok(Scan {
        events,
        head_hash: prev,
        ignored_tail_bytes: ignored,
        committed_len: committed_len as u64,
    })
}

/// Commit a transaction: append all event lines in one write + fsync.
/// `previous_hash` chaining is established against the live tail; the
/// caller (meta_tx) holds the writer lease so the tail cannot race.
/// Returns (event_hashes, line_hashes).
pub fn commit_events(
    store: &Path,
    specs: &[mt::EventSpec],
    transaction_id: &str,
    operation_id: &str,
    actor: &str,
    writer_epoch: u64,
) -> Result<(Vec<String>, String), String> {
    let path = events_path(store);
    if let Some(p) = path.parent() {
        std::fs::create_dir_all(p).map_err(|e| format!("META_LOG_WRITE: {e}"))?;
    }
    let scan = scan(store)?;
    if scan.ignored_tail_bytes > 0 {
        // Uncommitted residue only — trim to the committed prefix so the
        // chain stays contiguous (§26: the tail was never committed).
        let f = std::fs::OpenOptions::new()
            .write(true)
            .open(&path)
            .map_err(|e| format!("META_LOG_WRITE: {e}"))?;
        f.set_len(scan.committed_len)
            .map_err(|e| format!("META_LOG_WRITE: {e}"))?;
        f.sync_all().map_err(|e| format!("META_LOG_WRITE: {e}"))?;
    }
    let mut buf = String::new();
    let mut prev = scan.head_hash.clone();
    let mut seq = scan.events.last().map(|e| e.seq).unwrap_or(0);
    let mut hashes = Vec::with_capacity(specs.len());
    // One record mutates at most once per transaction — a duplicate
    // would make two events claim the same revision chain position.
    let mut seen = std::collections::HashSet::new();
    for spec in specs {
        if !seen.insert((&spec.record_type, &spec.record_id)) {
            return Err(format!(
                "XSTORE_TX_DUP: {}/{} twice in one transaction",
                spec.record_type, spec.record_id
            ));
        }
        seq += 1;
        let (line, eh) = mt::make_event_line(
            seq, transaction_id, operation_id, spec, actor, &prev,
            writer_epoch,
        )?;
        prev = hash::sha256_hex(line.as_bytes());
        hashes.push(eh);
        buf.push_str(&line);
        buf.push('\n');
    }
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| format!("META_LOG_WRITE: {e}"))?;
    f.write_all(buf.as_bytes())
        .map_err(|e| format!("META_LOG_WRITE: {e}"))?;
    f.sync_all().map_err(|e| format!("META_LOG_WRITE: {e}"))?;
    Ok((hashes, prev))
}

/// O(1)-ish tail: sha256 of the last raw line + its seq, without a full
/// scan (backward 8 KiB walk, audit.rs discipline). Used by read paths
/// to cheaply confirm the derived index is still in step.
pub fn tail_hash(store: &Path) -> Result<(u64, String), String> {
    let path = events_path(store);
    let mut f = match std::fs::File::open(&path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok((0, mt::GENESIS.into()))
        }
        Err(e) => return Err(format!("META_LOG_READ: {e}")),
    };
    let len = f.metadata().map_err(|e| format!("META_LOG_READ: {e}"))?.len();
    if len == 0 {
        return Ok((0, mt::GENESIS.into()));
    }
    let mut pos = len;
    let mut acc: Vec<u8> = Vec::new();
    loop {
        let take = pos.min(8192);
        pos -= take;
        f.seek(SeekFrom::Start(pos))
            .map_err(|e| format!("META_LOG_READ: {e}"))?;
        let mut blk = vec![0u8; take as usize];
        f.read_exact(&mut blk).map_err(|e| format!("META_LOG_READ: {e}"))?;
        blk.extend_from_slice(&acc);
        acc = blk;
        while acc.last() == Some(&b'\n') {
            acc.pop();
        }
        if let Some(nl) = acc.iter().rposition(|&b| b == b'\n') {
            let raw = &acc[nl + 1..];
            let v: Value = serde_json::from_slice(raw)
                .map_err(|e| format!("META_TAIL_CORRUPT: {e}"))?;
            let seq = v.get("seq").and_then(|x| x.as_u64()).unwrap_or(0);
            return Ok((seq, hash::sha256_hex(raw)));
        }
        if pos == 0 {
            let v: Value = serde_json::from_slice(&acc)
                .map_err(|e| format!("META_TAIL_CORRUPT: {e}"))?;
            let seq = v.get("seq").and_then(|x| x.as_u64()).unwrap_or(0);
            return Ok((seq, hash::sha256_hex(&acc)));
        }
    }
}

/// Append a mutation receipt to metadata/audit/receipts.jsonl — same
/// raw-line hash chain as audit.rs (§17: the receipts log plus the
/// event log together are the canonical xstore audit).
pub fn append_receipt(store: &Path, receipt: Value) -> Result<String, String> {
    let path = receipts_path(store);
    if let Some(p) = path.parent() {
        std::fs::create_dir_all(p).map_err(|e| format!("META_AUDIT_WRITE: {e}"))?;
    }
    // chain on previous raw line hash
    let prev = match std::fs::read_to_string(&path) {
        Ok(text) => text
            .lines()
            .filter(|l| !l.trim().is_empty())
            .last()
            .map(|l| hash::sha256_hex(l.as_bytes()))
            .unwrap_or_else(|| mt::GENESIS.into()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => mt::GENESIS.into(),
        Err(e) => return Err(format!("META_AUDIT_READ: {e}")),
    };
    let mut r = receipt;
    r["prev"] = Value::String(prev);
    let line = mt::canonical(&r);
    let line_hash = hash::sha256_hex(line.as_bytes());
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| format!("META_AUDIT_WRITE: {e}"))?;
    writeln!(f, "{line}").map_err(|e| format!("META_AUDIT_WRITE: {e}"))?;
    f.sync_all().map_err(|e| format!("META_AUDIT_WRITE: {e}"))?;
    Ok(line_hash)
}

/// Verify the receipts chain (same raw-byte chain rule).
pub fn verify_receipts(store: &Path) -> Result<Value, String> {
    let path = receipts_path(store);
    let text = match std::fs::read_to_string(&path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok(json!({"ok": true, "receipt_count": 0}))
        }
        Err(e) => return Err(format!("META_AUDIT_READ: {e}")),
    };
    let mut prev = mt::GENESIS.to_string();
    let mut count = 0u64;
    for line in text.lines() {
        if line.trim().is_empty() {
            continue;
        }
        let v: Value = serde_json::from_str(line)
            .map_err(|e| format!("RECEIPT_CORRUPT: {e}"))?;
        if v.get("format").and_then(|x| x.as_str()) != Some(mt::RECEIPT_FORMAT) {
            return Err("RECEIPT_CORRUPT: bad format tag".into());
        }
        if v.get("prev").and_then(|x| x.as_str()) != Some(prev.as_str()) {
            return Err("RECEIPT_CORRUPT: chain break".into());
        }
        prev = hash::sha256_hex(line.as_bytes());
        count += 1;
    }
    Ok(json!({"ok": true, "receipt_count": count, "head": prev}))
}

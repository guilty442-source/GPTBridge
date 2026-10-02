//! store.rs — content-addressed artifact/checkpoint store.
//!
//! Layout under <store>:
//!   objects/<sha256[0..2]>/<sha256>.bin     immutable object bodies
//!   store-index.jsonl                      append-only receipt log
//!                                          (star-store-index/v1)
//!
//! Write path: parse/verify FIRST (kind-gated), stream-hash the source,
//! spill to <store>/tmp/<pid>-<n>.tmp, fsync, then rename into place —
//! the same write-tmp-then-rename discipline as xct_ckpt.h's ckpt_save.
//! An existing object is never overwritten (content addressing makes a
//! same-hash rewrite a no-op; a different-hash object gets a new name
//! by construction, so there is no overwrite path at all).
//!
//! The index is an append-only receipt chain: every line carries the
//! previous line's sha256 in `prev`, so a truncated or reordered log is
//! detectable by `verify-store`.

use crate::hash;
use crate::xcn1;
use std::io::Write;
use std::path::{Path, PathBuf};

pub struct Receipt {
    pub sha256: String,
    pub kind: String,
    pub size: u64,
    pub object: PathBuf,
    pub prev: String,
}

fn objects_dir(store: &Path) -> PathBuf {
    store.join("objects")
}

fn object_path(store: &Path, sha: &str) -> PathBuf {
    objects_dir(store)
        .join(&sha[..2])
        .join(format!("{sha}.bin"))
}

fn index_path(store: &Path) -> PathBuf {
    store.join("store-index.jsonl")
}

/// Last `prev` hash in the index — the chain tip ("0"*64 for empty).
fn index_tip(store: &Path) -> Result<String, String> {
    let path = index_path(store);
    let Ok(text) = std::fs::read_to_string(&path) else {
        return Ok("0".repeat(64));
    };
    let mut tip = "0".repeat(64);
    for line in text.lines() {
        if line.trim().is_empty() {
            continue;
        }
        let v: serde_json::Value = serde_json::from_str(line)
            .map_err(|e| format!("INDEX_CORRUPT: {e}"))?;
        if v.get("format").and_then(|x| x.as_str())
            != Some("star-store-index/v1")
        {
            return Err("INDEX_CORRUPT: bad format tag".into());
        }
        match v.get("sha256").and_then(|x| x.as_str()) {
            Some(s) => tip = s.to_string(),
            None => return Err("INDEX_CORRUPT: missing sha256".into()),
        }
    }
    Ok(tip)
}

fn append_index(
    store: &Path,
    rec: &Receipt,
    extra: &serde_json::Value,
) -> Result<(), String> {
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(index_path(store))
        .map_err(|e| format!("INDEX_WRITE: {e}"))?;
    let line = serde_json::json!({
        "format": "star-store-index/v1",
        "sha256": rec.sha256,
        "kind": rec.kind,
        "size": rec.size,
        "object": rec.object.to_string_lossy(),
        "prev": rec.prev,
        "extra": extra,
    });
    writeln!(f, "{line}").map_err(|e| format!("INDEX_WRITE: {e}"))?;
    f.sync_all().map_err(|e| format!("INDEX_WRITE: {e}"))
}

fn esc(s: &str) -> String {
    // serde_json for internal lines already escapes; this helper exists
    // only for error strings embedded in the caller's JSON output.
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

/// Ingest a file into the store. `kind == "xcn1"` runs the full
/// structural verify before any byte is stored; `blob` accepts any
/// file. Returns the receipt.
pub fn put(store: &Path, file: &Path, kind: &str) -> Result<Receipt, String> {
    let data = std::fs::read(file)
        .map_err(|e| format!("STORE_READ: {}: {e}", esc(&file.to_string_lossy())))?;
    let mut extra_fields = serde_json::json!({});
    match kind {
        "xcn1" => {
            let (header, _entries, trailing) = xcn1::verify(&data)
                .map_err(|e| format!("XCN_VERIFY: {e}"))?;
            extra_fields = serde_json::json!({
                "xcn_version": header.version,
                "tensor_count": header.tensor_count,
                "trailing_bytes": trailing,
            });
        }
        "blob" => {}
        _ => return Err(format!("STORE_KIND_UNKNOWN: {kind}")),
    }
    let sha = hash::sha256_hex(&data);
    let object = object_path(store, &sha);
    if object.exists() {
        // Content-addressed idempotence: verify the resident bytes
        // still match before reporting the hit.
        let resident = std::fs::read(&object)
            .map_err(|e| format!("STORE_READ: resident: {e}"))?;
        if hash::sha256_hex(&resident) != sha {
            return Err("STORE_OBJECT_CORRUPT: hash mismatch on resident object".into());
        }
        let rec = Receipt {
            sha256: sha,
            kind: kind.to_string(),
            size: data.len() as u64,
            object,
            prev: index_tip(store)?,
        };
        append_index(store, &rec, &extra_fields)?;
        return Ok(rec);
    }
    let obj_dir = object.parent().unwrap();
    std::fs::create_dir_all(obj_dir)
        .map_err(|e| format!("STORE_WRITE: {e}"))?;
    let tmp_dir = store.join("tmp");
    std::fs::create_dir_all(&tmp_dir)
        .map_err(|e| format!("STORE_WRITE: {e}"))?;
    let tmp = tmp_dir.join(format!(
        "{}-{}.tmp",
        std::process::id(),
        &sha[..16]
    ));
    {
        let mut f = std::fs::File::create(&tmp)
            .map_err(|e| format!("STORE_WRITE: {e}"))?;
        f.write_all(&data).map_err(|e| format!("STORE_WRITE: {e}"))?;
        f.sync_all().map_err(|e| format!("STORE_WRITE: {e}"))?;
    }
    // Read-back verify before rename: the object entering the store is
    // the bytes we just hashed, not the bytes we meant to write.
    let written = std::fs::read(&tmp)
        .map_err(|e| format!("STORE_WRITE: read-back: {e}"))?;
    if hash::sha256_hex(&written) != sha {
        let _ = std::fs::remove_file(&tmp);
        return Err("STORE_WRITE: read-back hash mismatch".into());
    }
    std::fs::rename(&tmp, &object).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("STORE_WRITE: rename: {e}")
    })?;
    let rec = Receipt {
        sha256: sha,
        kind: kind.to_string(),
        size: data.len() as u64,
        object,
        prev: index_tip(store)?,
    };
    append_index(store, &rec, &extra_fields)?;
    Ok(rec)
}

/// Extract an object to `out` after re-verifying its hash.
pub fn get(store: &Path, sha: &str, out: &Path) -> Result<u64, String> {
    if sha.len() != 64 || !sha.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("STORE_SHA_INVALID".into());
    }
    let object = object_path(store, sha);
    let data = std::fs::read(&object)
        .map_err(|e| format!("STORE_MISS: {e}"))?;
    if hash::sha256_hex(&data) != sha {
        return Err("STORE_OBJECT_CORRUPT: hash mismatch".into());
    }
    std::fs::write(out, &data)
        .map_err(|e| format!("STORE_WRITE: out: {e}"))?;
    Ok(data.len() as u64)
}

/// Walk every object, re-hash, and check the index chain ordering.
/// Returns (objects_ok, objects_bad, index_ok, entries).
pub fn verify_store(store: &Path) -> Result<serde_json::Value, String> {
    let mut ok = 0u64;
    let mut bad = 0u64;
    let mut bad_paths: Vec<String> = Vec::new();
    let objs = objects_dir(store);
    if objs.is_dir() {
        let mut stack = vec![objs];
        while let Some(d) = stack.pop() {
            for ent in std::fs::read_dir(&d)
                .map_err(|e| format!("STORE_SCAN: {e}"))?
                .flatten()
            {
                let p = ent.path();
                if p.is_dir() {
                    stack.push(p);
                    continue;
                }
                let stem = p
                    .file_stem()
                    .map(|s| s.to_string_lossy().into_owned())
                    .unwrap_or_default();
                let data = std::fs::read(&p)
                    .map_err(|e| format!("STORE_SCAN: {e}"))?;
                if hash::sha256_hex(&data) == stem {
                    ok += 1;
                } else {
                    bad += 1;
                    bad_paths.push(p.to_string_lossy().into_owned());
                }
            }
        }
    }
    // Index chain: each line's `prev` must equal the previous line's
    // sha256 (tip seed "0"*64).
    let mut index_ok = true;
    let mut entries = 0u64;
    let mut prev = "0".repeat(64);
    let ipath = index_path(store);
    if ipath.exists() {
        let text = std::fs::read_to_string(&ipath)
            .map_err(|e| format!("INDEX_READ: {e}"))?;
        for line in text.lines() {
            if line.trim().is_empty() {
                continue;
            }
            entries += 1;
            let Ok(v) = serde_json::from_str::<serde_json::Value>(line)
            else {
                index_ok = false;
                continue;
            };
            let lp = v.get("prev").and_then(|x| x.as_str()).unwrap_or("");
            let ls = v.get("sha256").and_then(|x| x.as_str()).unwrap_or("");
            if lp != prev {
                index_ok = false;
            }
            prev = ls.to_string();
        }
    }
    Ok(serde_json::json!({
        "format": "xstore-verify-store/v1",
        "ok": bad == 0 && index_ok,
        "objects_ok": ok,
        "objects_corrupt": bad,
        "corrupt": bad_paths,
        "index_entries": entries,
        "index_chain_ok": index_ok,
    }))
}

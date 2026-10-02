//! snapshot.rs — dataset snapshot: pin a whole output directory into
//! the content-addressed store with a verifiable manifest.
//!
//!   xstore snapshot        --store <dir> --src <dir> [--name <id>]
//!   xstore snapshot-verify --store <dir> --manifest <sha256|path>
//!
//! Flow: walk <src> in sorted relative-path order → put each file into
//! the store (idempotent: unchanged files are receipts, not copies) →
//! emit a `star-dataset-snapshot/v1` manifest {name, files:[{path,
//! sha256,size}]} → store the manifest itself as an object and under
//! <store>/snapshots/<sha>.json → append an audit-log entry → print
//! the receipt. Verification re-loads the manifest and re-hashes every
//! listed object — a missing or corrupted byte flips `ok` to false.

use crate::audit;
use crate::hash;
use crate::store;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

fn rel_posix(root: &Path, p: &Path) -> String {
    p.strip_prefix(root)
        .unwrap_or(p)
        .components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join("/")
}

fn collect_files(src: &Path) -> Result<Vec<PathBuf>, String> {
    let mut out = Vec::new();
    let mut stack = vec![src.to_path_buf()];
    while let Some(d) = stack.pop() {
        for ent in std::fs::read_dir(&d)
            .map_err(|e| format!("SNAPSHOT_SCAN: {}: {e}", d.display()))?
            .flatten()
        {
            let p = ent.path();
            if p.is_dir() {
                stack.push(p);
            } else if p.is_file() {
                out.push(p);
            }
        }
    }
    out.sort();
    Ok(out)
}

/// Snapshot a directory into the store; returns the manifest object,
/// its sha256 and the receipt fields for the CLI wrapper.
pub fn snapshot(
    store_dir: &Path,
    src: &Path,
    name: &str,
) -> Result<Value, String> {
    if !src.is_dir() {
        return Err(format!("SNAPSHOT_SRC: not a directory: {}", src.display()));
    }
    let files = collect_files(src)?;
    if files.is_empty() {
        return Err("SNAPSHOT_EMPTY: no files under --src".into());
    }
    let mut entries = Vec::with_capacity(files.len());
    let mut total = 0u64;
    for f in &files {
        let rec = store::put(store_dir, f, "blob")?;
        total += rec.size;
        entries.push(json!({
            "path": rel_posix(src, f),
            "sha256": rec.sha256,
            "size": rec.size,
        }));
    }
    let manifest = json!({
        "format": "star-dataset-snapshot/v1",
        "name": name,
        "files": entries,
        "file_count": files.len(),
        "total_bytes": total,
    });
    let manifest_txt = serde_json::to_string_pretty(&manifest)
        .map_err(|e| format!("SNAPSHOT_WRITE: {e}"))?;
    let manifest_sha = hash::sha256_hex(manifest_txt.as_bytes());

    // Persist the manifest twice: as an ordinary content-addressed
    // object (so it dedups/fetches like any artifact) and as a named
    // snapshot record under <store>/snapshots/<sha>.json.
    let tmp_manifest = store_dir.join("tmp").join(format!(
        "manifest-{}-{}.json",
        std::process::id(),
        &manifest_sha[..16]
    ));
    std::fs::create_dir_all(tmp_manifest.parent().unwrap())
        .map_err(|e| format!("SNAPSHOT_WRITE: {e}"))?;
    std::fs::write(&tmp_manifest, &manifest_txt)
        .map_err(|e| format!("SNAPSHOT_WRITE: {e}"))?;
    store::put(store_dir, &tmp_manifest, "blob")?;
    let snap_dir = store_dir.join("snapshots");
    std::fs::create_dir_all(&snap_dir)
        .map_err(|e| format!("SNAPSHOT_WRITE: {e}"))?;
    let snap_path = snap_dir.join(format!("{manifest_sha}.json"));
    std::fs::write(&snap_path, &manifest_txt)
        .map_err(|e| format!("SNAPSHOT_WRITE: {e}"))?;
    let _ = std::fs::remove_file(&tmp_manifest);

    audit::append(
        &store_dir.join("store-audit.jsonl"),
        json!({
            "op": "snapshot",
            "name": name,
            "manifest_sha256": manifest_sha,
            "file_count": files.len(),
            "total_bytes": total,
        }),
    )?;

    Ok(json!({
        "format": "xstore-snapshot/v1",
        "ok": true,
        "name": name,
        "manifest_sha256": manifest_sha,
        "manifest": snap_path.to_string_lossy(),
        "file_count": files.len(),
        "total_bytes": total,
    }))
}

/// Verify a snapshot manifest against the store: every listed object
/// must exist and re-hash to its recorded sha256.
pub fn snapshot_verify(
    store_dir: &Path,
    manifest: &str,
) -> Result<Value, String> {
    // Accept a sha256 (resolved under snapshots/) or a path.
    let path = if manifest.len() == 64
        && manifest.bytes().all(|b| b.is_ascii_hexdigit())
    {
        store_dir.join("snapshots").join(format!("{manifest}.json"))
    } else {
        PathBuf::from(manifest)
    };
    let txt = std::fs::read_to_string(&path)
        .map_err(|e| format!("SNAPSHOT_READ: {}: {e}", path.display()))?;
    let manifest_sha = hash::sha256_hex(txt.as_bytes());
    // Self-addressing check: when resolved through snapshots/<sha>.json
    // the file's recomputed hash must equal its lookup address — a
    // tampered manifest is flagged before its contents are trusted.
    let self_addr_ok = !(manifest.len() == 64
        && manifest.bytes().all(|b| b.is_ascii_hexdigit()))
        || manifest_sha == manifest;
    let v: Value = serde_json::from_str(&txt)
        .map_err(|e| format!("SNAPSHOT_PARSE: {e}"))?;
    if v.get("format").and_then(|x| x.as_str())
        != Some("star-dataset-snapshot/v1")
    {
        return Err("SNAPSHOT_FORMAT: bad tag".into());
    }
    let files = v
        .get("files")
        .and_then(|x| x.as_array())
        .ok_or("SNAPSHOT_PARSE: missing files")?;
    let mut ok_count = 0u64;
    let mut bad: Vec<Value> = Vec::new();
    for f in files {
        let sha = f.get("sha256").and_then(|x| x.as_str()).unwrap_or("");
        let valid_sha = sha.len() == 64
            && sha.bytes().all(|b| b.is_ascii_hexdigit());
        let obj = if valid_sha {
            store_dir
                .join("objects")
                .join(&sha[..2])
                .join(format!("{sha}.bin"))
        } else {
            PathBuf::new()
        };
        let status = if !valid_sha {
            "bad-sha"
        } else {
            match std::fs::read(&obj) {
                Err(_) => "missing",
                Ok(data) => {
                    if hash::sha256_hex(&data) == sha {
                        ok_count += 1;
                        continue;
                    }
                    "corrupt"
                }
            }
        };
        bad.push(json!({
            "path": f.get("path"),
            "sha256": sha,
            "status": status,
        }));
    }
    Ok(json!({
        "format": "xstore-snapshot-verify/v1",
        "ok": bad.is_empty() && self_addr_ok,
        "manifest_sha256": manifest_sha,
        "manifest_self_addressed": self_addr_ok,
        "name": v.get("name"),
        "files_ok": ok_count,
        "files_bad": bad.len(),
        "bad": bad,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "xstore-snap-{}-{}",
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
    fn snapshot_roundtrip_and_audit() {
        let root = scratch();
        let src = root.join("out");
        std::fs::create_dir_all(src.join("sub")).unwrap();
        std::fs::write(src.join("a.jsonl"), b"line1\n").unwrap();
        std::fs::write(src.join("sub/b.jsonl"), b"line2\n").unwrap();
        let store_dir = root.join("s");
        let r = snapshot(&store_dir, &src, "ds-1").unwrap();
        assert_eq!(r["file_count"], 2);
        let sha = r["manifest_sha256"].as_str().unwrap();
        let v = snapshot_verify(&store_dir, sha).unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["files_ok"], 2);
        // Audit chain recorded the snapshot.
        let av = audit::verify(&store_dir.join("store-audit.jsonl"))
            .unwrap();
        assert_eq!(av["ok"], true);
        assert_eq!(av["entries"], 1);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn resnapshot_after_change_tracks_delta() {
        let root = scratch();
        let src = root.join("out");
        std::fs::create_dir_all(&src).unwrap();
        std::fs::write(src.join("f.txt"), b"v1").unwrap();
        let store_dir = root.join("s");
        let r1 = snapshot(&store_dir, &src, "ds").unwrap();
        std::fs::write(src.join("f.txt"), b"v2-changed").unwrap();
        let r2 = snapshot(&store_dir, &src, "ds").unwrap();
        assert_ne!(r1["manifest_sha256"], r2["manifest_sha256"]);
        // Both object generations coexist; store holds 2 objects for
        // f.txt + 2 manifest objects.
        let v = crate::store::verify_store(&store_dir).unwrap();
        assert_eq!(v["ok"], true);
        assert_eq!(v["objects_ok"], 4);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn missing_object_fails_verify() {
        let root = scratch();
        let src = root.join("out");
        std::fs::create_dir_all(&src).unwrap();
        std::fs::write(src.join("f.txt"), b"data").unwrap();
        let store_dir = root.join("s");
        let r = snapshot(&store_dir, &src, "ds").unwrap();
        let sha = r["manifest_sha256"].as_str().unwrap().to_string();
        // Delete the payload object.
        let src_sha = hash::sha256_hex(b"data");
        std::fs::remove_file(
            store_dir
                .join("objects")
                .join(&src_sha[..2])
                .join(format!("{src_sha}.bin")),
        )
        .unwrap();
        let v = snapshot_verify(&store_dir, &sha).unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["bad"][0]["status"], "missing");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn tampered_manifest_breaks_self_addressing() {
        let root = scratch();
        let src = root.join("out");
        std::fs::create_dir_all(&src).unwrap();
        std::fs::write(src.join("f.txt"), b"data").unwrap();
        let store_dir = root.join("s");
        let r = snapshot(&store_dir, &src, "ds").unwrap();
        let sha = r["manifest_sha256"].as_str().unwrap().to_string();
        let mp = store_dir.join("snapshots").join(format!("{sha}.json"));
        let txt = std::fs::read_to_string(&mp).unwrap()
            .replacen("f.txt", "g.txt", 1); // same length, different bytes
        std::fs::write(&mp, txt).unwrap();
        let v = snapshot_verify(&store_dir, &sha).unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["manifest_self_addressed"], false);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn empty_src_rejected() {
        let root = scratch();
        let src = root.join("empty");
        std::fs::create_dir_all(&src).unwrap();
        assert!(snapshot(&root.join("s"), &src, "x").is_err());
        let _ = std::fs::remove_dir_all(&root);
    }
}

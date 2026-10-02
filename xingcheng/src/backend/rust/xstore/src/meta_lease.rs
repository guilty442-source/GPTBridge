//! meta_lease.rs — single-writer lease for the metadata plane
//! (§71-§74). Writers: one at a time; readers: unrestricted.
//!
//!   <store>/metadata/leases/writer.lock  held lease (created O_EXCL)
//!   <store>/metadata/leases/epoch.json   monotonically increasing epoch
//!
//! Acquire path: atomic create_new of writer.lock; on contention the
//! contender inspects the holder's expiry — an expired lock is stale
//! (holder died) and is replaced after bumping the epoch file; a live
//! lock yields XSTORE_WRITER_BUSY after a bounded wait. The epoch is a
//! ferpect guard against ABA: a writer that lost its lease and wakes up
//! later carries an epoch older than current and is denied with
//! XSTORE_STALE_WRITER (§73,§74).

use serde_json::{json, Value};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[derive(Debug)]
pub struct Lease {
    pub owner_id: String,
    pub lease_id: String,
    pub epoch: u64,
    lock_path: PathBuf,
}

fn dir(store: &Path) -> PathBuf {
    store.join("metadata").join("leases")
}
fn lock_path(store: &Path) -> PathBuf {
    dir(store).join("writer.lock")
}
fn epoch_path(store: &Path) -> PathBuf {
    dir(store).join("epoch.json")
}

fn now_s() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn current_epoch(store: &Path) -> u64 {
    std::fs::read_to_string(epoch_path(store))
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v.get("epoch").and_then(|x| x.as_u64()))
        .unwrap_or(0)
}

fn write_epoch(store: &Path, epoch: u64) -> Result<(), String> {
    let tmp = dir(store).join("epoch.tmp");
    let body = mt_canonical(&json!({
        "format": "star-xstore-metadata-epoch/v1",
        "epoch": epoch,
        "updated_at": crate::meta_types::now_iso(),
    }));
    {
        let mut f = std::fs::File::create(&tmp)
            .map_err(|e| format!("LEASE_EPOCH_WRITE: {e}"))?;
        f.write_all(body.as_bytes())
            .and_then(|_| f.sync_all())
            .map_err(|e| format!("LEASE_EPOCH_WRITE: {e}"))?;
    }
    std::fs::rename(&tmp, epoch_path(store))
        .map_err(|e| format!("LEASE_EPOCH_WRITE: {e}"))
}

fn mt_canonical(v: &Value) -> String {
    crate::meta_types::canonical(v)
}

/// Take the writer lease. `ttl_s` bounds how long a dead holder may
/// block successors; `wait_ms` bounds how long *we* wait for a live
/// holder. Caller must `release` (or crash — expiry covers that).
pub fn acquire(
    store: &Path,
    owner_id: &str,
    ttl_s: f64,
    wait_ms: u64,
) -> Result<Lease, String> {
    std::fs::create_dir_all(dir(store))
        .map_err(|e| format!("LEASE_WRITE: {e}"))?;
    let lock = lock_path(store);
    let deadline = now_s() + (wait_ms as f64 / 1000.0);
    loop {
        match std::fs::OpenOptions::new().create_new(true).write(true).open(&lock)
        {
            Ok(mut f) => {
                let epoch = current_epoch(store) + 1;
                write_epoch(store, epoch)?;
                let lease = Lease {
                    owner_id: owner_id.to_string(),
                    lease_id: crate::meta_types::new_id("xml"),
                    epoch,
                    lock_path: lock.clone(),
                };
                let body = mt_canonical(&json!({
                    "format": "star-xstore-metadata-lease/v1",
                    "owner_id": lease.owner_id,
                    "lease_id": lease.lease_id,
                    "epoch": lease.epoch,
                    "acquired_at": crate::meta_types::now_iso(),
                    "expires_at": now_s() + ttl_s,
                }));
                f.write_all(body.as_bytes())
                    .and_then(|_| f.sync_all())
                    .map_err(|e| format!("LEASE_WRITE: {e}"))?;
                return Ok(lease);
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                let stale = std::fs::read_to_string(&lock)
                    .ok()
                    .and_then(|t| serde_json::from_str::<Value>(&t).ok())
                    .and_then(|v| v.get("expires_at").and_then(|x| x.as_f64()))
                    .map(|exp| exp < now_s())
                    .unwrap_or(false);
                if stale {
                    let _ = std::fs::remove_file(&lock);
                    continue;
                }
                if now_s() >= deadline {
                    return Err("XSTORE_WRITER_BUSY".into());
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            Err(e) => return Err(format!("LEASE_ACQUIRE: {e}")),
        }
    }
}

/// Release the lease — delete only if the file still names our lease_id
/// (a stale-takeover may have replaced it; deleting that would steal a
/// live successor's lock).
pub fn release(lease: &Lease) {
    let ours = std::fs::read_to_string(&lease.lock_path)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v.get("lease_id").and_then(|x| x.as_str()).map(str::to_string))
        .map(|id| id == lease.lease_id)
        .unwrap_or(false);
    if ours {
        let _ = std::fs::remove_file(&lease.lock_path);
    }
}

/// Stale-writer guard for callers that pin an epoch across calls
/// (§73/§74): a supplied epoch below current means the caller's lease
/// generation was superseded.
pub fn check_epoch(store: &Path, supplied: u64) -> Result<(), String> {
    let cur = current_epoch(store);
    if supplied < cur {
        return Err(format!(
            "XSTORE_STALE_WRITER: epoch {supplied} < current {cur}"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn acquire_release_and_stale_takeover() {
        let root = std::env::temp_dir().join(crate::meta_types::new_id("lease-t"));
        let a = acquire(&root, "w-a", 30.0, 200).unwrap();
        // live lock held -> busy
        assert!(acquire(&root, "w-b", 30.0, 60)
            .unwrap_err()
            .contains("XSTORE_WRITER_BUSY"));
        assert_eq!(current_epoch(&root), 1);
        // forge an expired lock -> successor takes over, epoch bumps
        let body = mt_canonical(&json!({
            "owner_id": "ghost", "lease_id": "x", "epoch": 1,
            "expires_at": now_s() - 1.0,
        }));
        std::fs::write(&a.lock_path, body).unwrap();
        let b = acquire(&root, "w-b", 30.0, 200).unwrap();
        assert_eq!(b.epoch, 2);
        // stale epoch denied
        assert!(check_epoch(&root, 1).unwrap_err().contains("STALE"));
        release(&b);
        release(&a); // a's file already replaced — must not delete b's
        assert!(!lock_path(&root).exists());
        let _ = std::fs::remove_dir_all(&root);
    }
}

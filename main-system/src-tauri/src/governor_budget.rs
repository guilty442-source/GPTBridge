//! governor_budget.rs — `concurrency-budget/v1` read side for the shell.
//!
//! The C++23 resource-governor publishes per-class worker quotas in
//! `main-system/runtime/state/resource-governor.json`.  Shell bridges
//! (tool_bridge, js_bridge/loopback, webview_host) size their fixed
//! connection pools from those quotas, clamped into each module's
//! declared envelope.  Missing/stale state fails open to the envelope
//! maximum — a dead governor must never deadlock the fleet.
//!
//! `bounded_conn_pool` is the shared admission primitive for the
//! loopback accept loops: a `sync_channel` pending queue with fixed
//! capacity plus `pool` long-lived workers.  When the queue is full the
//! accept loop rejects the socket (drop/reject policy) instead of
//! spawning a thread — no thread-per-connection anywhere.

use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

/// Locate the governor state file: `GPTBRIDGE_GOVERNOR_STATE` env
/// override first, then the repo-canonical path derived from the crate
/// dir (`<repo>/main-system/src-tauri` → `<repo>/main-system/runtime`).
pub fn state_path() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("GPTBRIDGE_GOVERNOR_STATE") {
        let p = PathBuf::from(p);
        if p.exists() {
            return Some(p);
        }
    }
    let candidate = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("runtime")
        .join("state")
        .join("resource-governor.json");
    candidate.exists().then_some(candidate)
}

/// `classes.<work_class>.quota` from the state file; `None` when the
/// file is unreadable, the governor is disabled, or the contract or
/// class entry is absent.
pub fn class_quota(state_path: &Path, work_class: &str) -> Option<usize> {
    let text = std::fs::read_to_string(state_path).ok()?;
    let state: serde_json::Value = serde_json::from_str(&text).ok()?;
    if state.get("disabled").and_then(|v| v.as_bool()) == Some(true) {
        return None;
    }
    let budget = state.get("concurrency_budget")?;
    if budget.get("contract").and_then(|v| v.as_str()) != Some("concurrency-budget/v1") {
        return None;
    }
    budget
        .get("classes")?
        .get(work_class)?
        .get("quota")?
        .as_u64()
        .map(|q| q as usize)
}

/// Effective worker count: governor quota clamped into the declared
/// `[min, max]` envelope; absent/unusable state fails open to `max`.
/// A zero/paused quota clamps to `min` — the bridge still answers,
/// just at floor capacity.
pub fn resolve_workers(work_class: &str, min: usize, max: usize) -> usize {
    let quota = state_path()
        .as_deref()
        .and_then(|p| class_quota(p, work_class))
        .filter(|q| *q > 0)
        .unwrap_or(max);
    quota.clamp(min.max(1), max.max(1))
}

/// Spawn a fixed connection worker pool draining `rx`.  Workers exit
/// when the sender side is dropped (listener stopped).  `queue_deadline`
/// is the pending-item TTL: a connection still queued past it is
/// dropped instead of served stale (deadline property of the queue
/// contract).
fn spawn_pool<C, H>(
    rx: Arc<Mutex<Receiver<(TcpStream, Instant)>>>,
    workers: usize,
    queue_deadline: Duration,
    ctx: C,
    handler: H,
) where
    C: Send + Sync + 'static + Clone,
    H: Fn(&C, TcpStream) + Send + Sync + 'static,
{
    let handler = Arc::new(handler);
    for i in 0..workers {
        let rx = rx.clone();
        let ctx = ctx.clone();
        let handler = handler.clone();
        let _ = thread::Builder::new()
            .name(format!("conn-pool-{}", i))
            .spawn(move || loop {
                let (stream, enqueued) = {
                    let guard = match rx.lock() {
                        Ok(g) => g,
                        Err(_) => return,
                    };
                    match guard.recv() {
                        Ok(item) => item,
                        Err(_) => return,
                    }
                };
                if enqueued.elapsed() > queue_deadline {
                    drop(stream); // stale admission — client already gone
                    continue;
                }
                handler(&ctx, stream);
            });
    }
}

/// Bounded connection admission: returns a `SyncSender` fed by the
/// accept loop via `try_send`.  On `Full`/`Disconnected` the caller
/// applies its drop/reject policy inline (`reject` passed here is for
/// shutdown-time draining; admission rejection is the caller's job —
/// e.g. `respond(503)` then drop).  `queue_deadline` expires queued
/// connections (drop policy) — declared per module's B16 latency
/// envelope, never tuned globally.
///
/// ```text
/// match tx.try_send(stream) {
///     Ok(()) => {}
///     Err(TrySendError::Full(s)) | Err(TrySendError::Disconnected(s)) => reject(s),
/// }
/// ```
pub fn bounded_conn_pool<C, H>(
    workers: usize,
    pending_capacity: usize,
    queue_deadline: Duration,
    ctx: C,
    handler: H,
) -> SyncSender<(TcpStream, Instant)>
where
    C: Send + Sync + 'static + Clone,
    H: Fn(&C, TcpStream) + Send + Sync + 'static,
{
    let (tx, rx) = sync_channel::<(TcpStream, Instant)>(pending_capacity.max(1));
    spawn_pool(Arc::new(Mutex::new(rx)), workers.max(1), queue_deadline, ctx, handler);
    tx
}

//! vectord — GPTBridge governed Rust vector engine.
//!
//! Contract `vectord/v1`: loopback-only HTTP/JSON service holding the
//! semantic index as rebuildable derived data.  It never holds formal
//! authority — PostgreSQL owns canonical source data and resolves
//! scope/revision/tombstone on the candidate IDs this engine returns
//! (codex A610 DATA-ARCHITECTURE-TARGET / DATA-SAFETY).

mod embed;
mod requests;
mod store;
mod work_stealing;

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use store::Store;
use work_stealing::WorkStealingPool;

const CONTRACT: &str = "vectord/v1";
const VERSION: &str = env!("CARGO_PKG_VERSION");
const DEFAULT_BIND: &str = "127.0.0.1:8092";
const MAX_BODY_BYTES: usize = 64 * 1024 * 1024;
const MAX_HEADER_BYTES: usize = 16 * 1024;
const SNAPSHOT_NAME: &str = "vectord-snapshot.bin";
const SNAPSHOT_INTERVAL: Duration = Duration::from_secs(2);
// bounded-concurrency/v1: declared envelope — the effective worker
// count is the governor "rag" class quota (concurrency-budget/v1)
// clamped into [MIN_CONN_WORKERS, MAX_CONN_WORKERS]; unreadable state
// fails open to available_parallelism clamped into the same envelope.
const MIN_CONN_WORKERS: usize = 2;
const MAX_CONN_WORKERS: usize = 16;
const PENDING_CONN_CAPACITY: usize = 64;
// Declared latency envelope (B16): a connection still queued after
// this budget has almost certainly been abandoned by its client —
// expire it instead of serving stale work.
const PENDING_CONN_DEADLINE: Duration = Duration::from_millis(2000);


struct App {
    store: Arc<Store>,
    store_dir: PathBuf,
    dirty: Arc<AtomicBool>,
    started: Instant,
}

fn is_loopback_bind(bind: &str) -> bool {
    let host = bind
        .rsplit_once(':')
        .map(|(h, _)| h.trim().to_ascii_lowercase())
        .unwrap_or_else(|| bind.trim().to_ascii_lowercase());
    matches!(host.as_str(), "127.0.0.1" | "localhost" | "::1" | "[::1]")
}

/// concurrency-budget/v1 read side: extract `classes.<work_class>.quota`
/// from the resource-governor state file. Returns None when the file is
/// missing, unparsable, the governor is disabled, or the section/class is
/// absent — callers fall back to the static envelope (fail-open).
fn governor_class_quota(state_path: &std::path::Path, work_class: &str) -> Option<usize> {
    let text = std::fs::read_to_string(state_path).ok()?;
    let state: Value = serde_json::from_str(&text).ok()?;
    if state.get("disabled").and_then(Value::as_bool) == Some(true) {
        return None;
    }
    let budget = state.get("concurrency_budget")?;
    if budget.get("contract").and_then(Value::as_str) != Some("concurrency-budget/v1") {
        return None;
    }
    let quota = budget
        .get("classes")?
        .get(work_class)?
        .get("quota")?
        .as_u64()?;
    Some(quota as usize)
}

fn resolve_conn_workers(governor_state: Option<&std::path::Path>) -> usize {
    let quota = governor_state
        .and_then(|p| governor_class_quota(p, "rag"))
        .filter(|q| *q > 0);
    let fallback = thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(MAX_CONN_WORKERS);
    quota
        .unwrap_or(fallback)
        .clamp(MIN_CONN_WORKERS, MAX_CONN_WORKERS)
}

fn reject_over_capacity(mut stream: TcpStream) {
    send_response(
        &mut stream,
        "503 Service Unavailable",
        &json!({"error": "capacity-exhausted", "contract": CONTRACT}),
    );
}

fn send_response(stream: &mut TcpStream, status: &str, body: &Value) {
    send_bytes(stream, status, "application/json", body.to_string().as_bytes());
}

fn send_bytes(stream: &mut TcpStream, status: &str, content_type: &str, body: &[u8]) {
    let response = format!(
        "HTTP/1.1 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        status,
        content_type,
        body.len()
    );
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.write_all(body);
    let _ = stream.flush();
}

fn read_request(stream: &mut TcpStream) -> Result<(String, String, Vec<u8>), String> {
    stream
        .set_read_timeout(Some(Duration::from_secs(30)))
        .ok();
    let mut buffer = Vec::with_capacity(8192);
    let mut chunk = [0u8; 8192];
    let mut header_end = None;
    while header_end.is_none() {
        let n = stream.read(&mut chunk).map_err(|e| e.to_string())?;
        if n == 0 {
            return Err("EOF".to_string());
        }
        buffer.extend_from_slice(&chunk[..n]);
        if buffer.len() > MAX_HEADER_BYTES + MAX_BODY_BYTES {
            return Err("REQUEST_TOO_LARGE".to_string());
        }
        header_end = buffer
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .map(|p| p + 4);
    }
    let header_end = header_end.unwrap();
    let header_text = String::from_utf8_lossy(&buffer[..header_end]).to_string();
    let mut lines = header_text.lines();
    let request_line = lines.next().unwrap_or("");
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let path = parts.next().unwrap_or("").to_string();
    let mut content_length = 0usize;
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            if name.trim().eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse().unwrap_or(0);
            }
        }
    }
    if content_length > MAX_BODY_BYTES {
        return Err("REQUEST_TOO_LARGE".to_string());
    }
    let mut body = buffer[header_end..].to_vec();
    while body.len() < content_length {
        let n = stream.read(&mut chunk).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        body.extend_from_slice(&chunk[..n]);
    }
    body.truncate(content_length);
    Ok((method, path, body))
}

fn ok(extra: Value) -> Value {
    let mut map = extra.as_object().cloned().unwrap_or_default();
    map.insert("ok".to_string(), Value::Bool(true));
    map.insert("contract".to_string(), json!(CONTRACT));
    Value::Object(map)
}

fn err(code: &str) -> Value {
    json!({"ok": false, "contract": CONTRACT, "error": code})
}


mod routes;

use routes::{handle_connection, load_snapshot, persist_snapshot};
fn main() {
    let mut bind = DEFAULT_BIND.to_string();
    let mut store_dir = PathBuf::from("runtime/vectord");
    let mut capacity_hint = 1_000_000usize;
    let args: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--bind" if i + 1 < args.len() => {
                bind = args[i + 1].clone();
                i += 2;
            }
            "--store-dir" if i + 1 < args.len() => {
                store_dir = PathBuf::from(&args[i + 1]);
                i += 2;
            }
            "--capacity-hint" if i + 1 < args.len() => {
                capacity_hint = args[i + 1].parse().unwrap_or(capacity_hint);
                i += 2;
            }
            _ => i += 1,
        }
    }
    if let Ok(v) = std::env::var("VECTOR_STORE_DIR") {
        store_dir = PathBuf::from(v);
    }

    // Fail-closed: the canonical semantic index is local-owned; a
    // non-loopback bind is refused outright rather than warned.
    if !is_loopback_bind(&bind) {
        eprintln!("vectord: refusing non-loopback bind {}", bind);
        std::process::exit(2);
    }

    let dirty = Arc::new(AtomicBool::new(false));
    let store = match load_snapshot(&store_dir, capacity_hint) {
        Some(store) => {
            let (collections, points) = store.stats();
            eprintln!(
                "vectord: snapshot loaded ({} collections, {} points)",
                collections, points
            );
            Arc::new(store)
        }
        None => {
            eprintln!("vectord: no usable snapshot; starting empty (rebuildable derived data)");
            Arc::new(Store::new(capacity_hint))
        }
    };
    let app = Arc::new(App {
        store: store.clone(),
        store_dir: store_dir.clone(),
        dirty: dirty.clone(),
        started: Instant::now(),
    });

    {
        let store = store.clone();
        let dirty = dirty.clone();
        let dir = store_dir.clone();
        thread::spawn(move || loop {
            thread::sleep(SNAPSHOT_INTERVAL);
            if dirty.swap(false, Ordering::Relaxed) {
                if let Err(e) = persist_snapshot(&store, &dir) {
                    eprintln!("vectord: snapshot failed: {}", e);
                    dirty.store(true, Ordering::Relaxed);
                }
            }
        });
    }

    let listener = match TcpListener::bind(&bind) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("vectord: bind {} failed: {}", bind, e);
            std::process::exit(3);
        }
    };

    // bounded-concurrency/v1: connections are admitted into a bounded
    // pending channel drained by a fixed worker pool (rag-class quota).
    // A full queue rejects with HTTP 503 — never a thread per conn.
    let governor_state = std::env::var("GPTBRIDGE_GOVERNOR_STATE")
        .ok()
        .map(PathBuf::from)
        .or_else(|| {
            let mut p = store_dir.clone();
            p.pop(); // runtime/
            p.pop(); // tool root
            let candidate = p
                .join("..")
                .join("main-system")
                .join("runtime")
                .join("state")
                .join("resource-governor.json");
            candidate.exists().then_some(candidate)
        });
    let workers = resolve_conn_workers(governor_state.as_deref());
    let pool = Arc::new(WorkStealingPool::new(workers, PENDING_CONN_CAPACITY));
    eprintln!(
        "vectord: listening on {} (contract {}, conn_workers={}, pending_cap={})",
        bind, CONTRACT, workers, PENDING_CONN_CAPACITY
    );

    for connection in listener.incoming() {
        match connection {
            Ok(stream) => {
                let task_stream = match stream.try_clone() {
                    Ok(clone) => clone,
                    Err(_) => {
                        reject_over_capacity(stream);
                        continue;
                    }
                };
                let app = app.clone();
                if pool
                    .submit_opts(
                        move || handle_connection(task_stream, app),
                        work_stealing::TaskOpts {
                            deadline: Some(PENDING_CONN_DEADLINE),
                            ..work_stealing::TaskOpts::default()
                        },
                    )
                    .is_err()
                {
                    reject_over_capacity(stream);
                }
            }
            Err(e) => eprintln!("vectord: accept failed: {}", e),
        }
    }
}

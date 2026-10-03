//! ragd — GPTBridge governed Rust RAG orchestration service.
//!
//! Contract `ragd/v1` (increment 1): canonical dense query read path.
//! vectord generates candidates; xstore canonical replay supplies authority —
//! every hit is proved against migrated chunk + resource records;
//! tombstones and index state are read from the same canonical replay.
//! before entering the evidence pool (A610/A374, pipeline.py read
//! barrier). Loopback-only; migration or integrity failures reject authority.

mod barrier;
mod cag;
mod context;
mod dag;

mod evidence;
mod fusion;
mod http;
mod lanes;
mod native_authority;
mod retrieve;
mod vectord;

use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::{sync_channel, TrySendError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

pub(crate) const CONTRACT: &str = "ragd/v1";
pub(crate) const VERSION: &str = env!("CARGO_PKG_VERSION");
const DEFAULT_BIND: &str = "127.0.0.1:8094";
const DEFAULT_VECTORD: &str = "http://127.0.0.1:8092";
const MAX_BODY_BYTES: usize = 8 * 1024 * 1024;
const MAX_HEADER_BYTES: usize = 16 * 1024;

pub(crate) struct App {
    pub vectord_base: String,
    pub metadata_store: Option<std::path::PathBuf>,

    pub cache: Mutex<cag::CagCacheStore>,
    pub conn: ConnStats,
    started: Instant,
}

impl App {
    /// Every authority closure sees one canonical replay, including all joins.
    pub(crate) fn with_authority<T>(
        &self,
        f: impl FnOnce(&mut native_authority::Authority) -> Result<T, String>,
    ) -> Result<T, String> {
        let path = self.metadata_store.as_ref().ok_or("RAG_METADATA_STORE_REQUIRED")?;
        let mut authority = native_authority::Authority::open(path)?;
        f(&mut authority)
    }
}
fn is_loopback_bind(bind: &str) -> bool {
    let host = bind
        .rsplit_once(':')
        .map(|(h, _)| h.trim().to_ascii_lowercase())
        .unwrap_or_else(|| bind.trim().to_ascii_lowercase());
    matches!(host.as_str(), "127.0.0.1" | "localhost" | "::1" | "[::1]")
}

// bounded-concurrency/v1: fixed worker pool + bounded pending queue,
// mirroring vectord's envelope (MIN/MAX workers, 64 pending, 2 s
// expiry). A connection still queued past the deadline is dropped
// instead of served stale; a full queue rejects with HTTP 503 — never
// a thread per connection (PERF-04/PERF-05).
const MIN_CONN_WORKERS: usize = 2;
const MAX_CONN_WORKERS: usize = 16;
const PENDING_CONN_CAPACITY: usize = 64;
const PENDING_CONN_DEADLINE: Duration = Duration::from_millis(2000);

pub(crate) struct ConnStats {
    submitted: AtomicUsize,
    completed: AtomicUsize,
    rejected: AtomicUsize,
    expired: AtomicUsize,
}

impl ConnStats {
    fn new() -> Self {
        Self {
            submitted: AtomicUsize::new(0),
            completed: AtomicUsize::new(0),
            rejected: AtomicUsize::new(0),
            expired: AtomicUsize::new(0),
        }
    }

    fn snapshot(&self) -> Value {
        let submitted = self.submitted.load(Ordering::Relaxed);
        let completed = self.completed.load(Ordering::Relaxed);
        let rejected = self.rejected.load(Ordering::Relaxed);
        let expired = self.expired.load(Ordering::Relaxed);
        json!({
            "workers_min": MIN_CONN_WORKERS,
            "workers_max": MAX_CONN_WORKERS,
            "pending_cap": PENDING_CONN_CAPACITY,
            "submitted": submitted,
            "completed": completed,
            "rejected": rejected,
            "expired": expired,
            "pending_approx": submitted
                .saturating_sub(completed)
                .saturating_sub(rejected)
                .saturating_sub(expired),
        })
    }
}

struct PendingConn {
    stream: TcpStream,
    enqueued: Instant,
}

/// concurrency-budget/v1 read side: extract `classes.rag.quota` from
/// the resource-governor state file. Returns None when the file is
/// missing, unparsable, the governor is disabled, or the section is
/// absent — callers fall back to the static envelope (fail-open).
/// Same contract as vectord's governor_class_quota.
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
    let fallback = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(MAX_CONN_WORKERS);
    quota
        .unwrap_or(fallback)
        .clamp(MIN_CONN_WORKERS, MAX_CONN_WORKERS)
}

/// Walk up from the current directory for the governor state file
/// (`main-system/runtime/state/resource-governor.json`). Returns None
/// when not found — the caller fails open to the static envelope.
fn default_governor_state() -> Option<std::path::PathBuf> {
    if let Ok(path) = std::env::var("GPTBRIDGE_GOVERNOR_STATE") {
        let candidate = std::path::PathBuf::from(path);
        if candidate.exists() {
            return Some(candidate);
        }
    }
    let mut dir = std::env::current_dir().ok()?;
    for _ in 0..8 {
        let candidate = dir
            .join("main-system")
            .join("runtime")
            .join("state")
            .join("resource-governor.json");
        if candidate.exists() {
            return Some(candidate);
        }
        if !dir.pop() {
            break;
        }
    }
    None
}

fn reject_over_capacity(mut stream: TcpStream) {
    send_response(&mut stream, "503 Service Unavailable", &err("CAPACITY_EXHAUSTED"));
}

fn send_response(stream: &mut TcpStream, status: &str, body: &Value) {
    let bytes = body.to_string();
    let head = format!(
        "HTTP/1.1 {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        status,
        bytes.len()
    );
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(bytes.as_bytes());
    let _ = stream.flush();
}

fn read_request(stream: &mut TcpStream) -> Result<(String, String, Vec<u8>), String> {
    stream
        .set_read_timeout(Some(Duration::from_secs(30)))
        .ok();
    let mut buffer = Vec::with_capacity(4096);
    let mut chunk = [0u8; 4096];
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

pub(crate) fn err(code: &str) -> Value {
    json!({"ok": false, "contract": CONTRACT, "error": code})
}

fn ok(extra: Value) -> Value {
    let mut map = extra.as_object().cloned().unwrap_or_default();
    map.insert("ok".to_string(), Value::Bool(true));
    map.insert("contract".to_string(), json!(CONTRACT));
    Value::Object(map)
}

/// `POST /v1/rag/query` — canonical dense query with PG read barrier.
///
/// Request:
///   collection  vectord collection name (required)
///   module_ids  governed request scope (required, non-empty)
///   text        query text (engine embeds)  — xor —
///   vector      pre-embedded f32 vector
///   top_k       default 10, capped 256
///   alias       generation alias (default "gptbridge_rag")
///   generation_id  explicit generation pin; otherwise ragd resolves the
///                  ACTIVE generation for `alias` from PG
fn handle_query(app: &App, body: &[u8]) -> Value {
    let req: Value = match serde_json::from_slice(body) {
        Ok(v) => v,
        Err(_) => return err("INVALID_JSON"),
    };
    let collection = req.get("collection").and_then(Value::as_str).unwrap_or("");
    let alias = req
        .get("alias")
        .and_then(Value::as_str)
        .unwrap_or("gptbridge_rag");
    let module_ids: Vec<String> = req
        .get("module_ids")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let top_k = req
        .get("top_k")
        .and_then(Value::as_u64)
        .map(|n| (n as usize).min(256))
        .unwrap_or(10);
    let text = req.get("text").and_then(Value::as_str);
    let vector: Option<Vec<f32>> = req.get("vector").and_then(Value::as_array).map(|a| {
        a.iter()
            .filter_map(Value::as_f64)
            .map(|f| f as f32)
            .collect()
    });
    if collection.is_empty() || module_ids.is_empty() || (text.is_none() && vector.is_none()) {
        return err("INVALID_REQUEST");
    }

    let hits = match vectord::search(
        &app.vectord_base,
        collection,
        text,
        vector,
        top_k,
        &module_ids,
    ) {
        Ok(h) => h,
        Err(e) => return err(&format!("CANDIDATE_FETCH_FAILED:{}", e)),
    };

    let explicit_gen = req.get("generation_id").and_then(Value::as_str);
    let active_gen: Option<String> = match explicit_gen {
        Some(g) => Some(g.to_string()),
        None => match app.with_authority(|pg| pg.active_generation(alias)) {
            Ok(g) => g,
            Err(_) => None,
        },
    };

    let scope: HashSet<String> = module_ids.iter().cloned().collect();
    let point_ids: Vec<String> = hits.iter().map(|h| h.id.clone()).collect();
    let mut rids_by_module: HashMap<String, Vec<String>> = HashMap::new();
    for hit in &hits {
        let mid = hit
            .payload
            .get("module_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let rid = hit
            .payload
            .get("document_resource_id")
            .or_else(|| hit.payload.get("resource_id"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if !mid.is_empty() && !rid.is_empty() {
            rids_by_module
                .entry(mid.to_string())
                .or_default()
                .push(rid.to_string());
        }
    }

    let (chunk_rows, index_states, authority_error) =
        match app.with_authority(|pg| {
            let chunks = pg.chunks_for_points(&module_ids, &point_ids)?;
            let mut states = HashMap::new();
            for (mid, rids) in &rids_by_module {
                for (rid, status) in pg.index_states(mid, rids)? {
                    states.insert((mid.clone(), rid), status);
                }
            }
            Ok((chunks, states))
        }) {
            Ok((c, s)) => (c, s, Value::Null),
            // Fail-closed: authority unreachable -> every hit unproved.
            Err(e) => (HashMap::new(), HashMap::new(), Value::String(e)),
        };

    let (proved, drops) = barrier::apply(
        &hits,
        &chunk_rows,
        &index_states,
        &scope,
        active_gen.as_deref(),
    );
    ok(json!({
        "service": "ragd",
        "version": VERSION,
        "hits": proved,
        "candidates": hits.len(),
        "generation_id": active_gen,
        "dropped": {
            "unauthorized": drops.unauthorized,
            "generation_mismatch": drops.generation_mismatch,
            "missing_metadata": drops.missing_metadata,
            "tombstoned": drops.tombstoned,
        },
        "authority_error": authority_error,
    }))
}

fn route(app: &Arc<App>, method: &str, path: &str, body: &[u8]) -> Value {
    match (method, path) {
        ("GET", "/healthz") => ok(json!({
            "service": "ragd",
            "version": VERSION,
            "vectord": app.vectord_base,
            "metadata_authority": "xstore",
            "metadata_store_configured": app.metadata_store.is_some(),
            "uptime_s": app.started.elapsed().as_secs(),
            "cag": app.cache.lock().map(|c| c.stats()).unwrap_or_default(),
            "conn": app.conn.snapshot(),
        })),
        ("POST", "/v1/rag/query") => handle_query(app, body),
        ("POST", "/v1/retrieve") => retrieve::handle_retrieve(app, body),
        _ => err("NOT_FOUND"),
    }
}

#[cfg(test)]
mod retirement_tests {
    use super::*;
    #[test]
    fn explicit_generation_cannot_bypass_unmigrated_authority() {
        let app=Arc::new(App {
            vectord_base:DEFAULT_VECTORD.into(),metadata_store:None,
            cache:Mutex::new(cag::CagCacheStore::new()),conn:ConnStats::new(),started:Instant::now(),
        });
        let request=json!({"query":"proof","module_ids":["m"],"generation_id":"caller-generation","use_cache":true});
        let result=route(&app,"POST","/v1/retrieve",&serde_json::to_vec(&request).unwrap());
        assert_eq!(result["ok"],false);
        assert_eq!(result["error"],"AUTHORITY_UNAVAILABLE");
        assert_eq!(result["authority_error"],"RAG_METADATA_STORE_REQUIRED");
    }
}

fn handle_connection(mut stream: TcpStream, app: &Arc<App>) {
    match read_request(&mut stream) {
        Ok((method, path, body)) => {
            send_response(&mut stream, "200 OK", &route(app, &method, &path, &body));
        }
        Err(e) => send_response(&mut stream, "400 Bad Request", &err(&e)),
    }
}

fn main() {
    let mut bind = DEFAULT_BIND.to_string();
    let mut vectord_base = std::env::var("VECTORD_URL")
        .unwrap_or_else(|_| DEFAULT_VECTORD.to_string());
    let mut metadata_store = std::env::var_os("GPTBRIDGE_RAG_METADATA_STORE").map(std::path::PathBuf::from);
    let mut migration_source: Option<std::path::PathBuf> = None;
    let args: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--bind" if i + 1 < args.len() => {
                bind = args[i + 1].clone();
                i += 2;
            }
            "--vectord" if i + 1 < args.len() => {
                vectord_base = args[i + 1].clone();
                i += 2;
            }
            "--metadata-store" if i + 1 < args.len() => {
                metadata_store = Some(args[i + 1].clone().into());
                i += 2;
            }
            "--migrate-rag-source" if i + 1 < args.len() => {
                migration_source = Some(args[i + 1].clone().into());
                i += 2;
            }
            "--dsn" => { eprintln!("POSTGRESQL_RETIRED_USE_METADATA_STORE"); std::process::exit(2); }
            _ => i += 1,
        }
    }

    // Fail-closed: governed read path is local-owned; non-loopback
    // refused outright rather than warned (same contract as vectord).
    if !is_loopback_bind(&bind) {
        eprintln!("ragd: refusing non-loopback bind {}", bind);
        std::process::exit(2);
    }

    if let Some(source_path) = migration_source {
        let result = (|| -> Result<Value, String> {
            let store = metadata_store.as_ref().ok_or("RAG_METADATA_STORE_REQUIRED")?;
            let bytes = std::fs::read(&source_path).map_err(|e| e.to_string())?;
            let source: Value = serde_json::from_slice(&bytes).map_err(|e|e.to_string())?;
            xstore::rag::migrate(store, &source)
        })();
        match result {
            Ok(receipt) => println!("{}", receipt),
            Err(error) => { eprintln!("{}", error); std::process::exit(2); }
        }
        return;
    }
    let app = Arc::new(App {
        vectord_base,
        metadata_store,

        cache: Mutex::new(cag::CagCacheStore::new()),
        conn: ConnStats::new(),
        started: Instant::now(),
    });
    let listener = match TcpListener::bind(&bind) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("ragd: bind {} failed: {}", bind, e);
            std::process::exit(1);
        }
    };
    // Bounded admission: connections wait in a bounded pending queue
    // drained by a fixed worker pool (rag-class quota). A full queue
    // rejects with HTTP 503; a stale queued connection is dropped.
    let workers = resolve_conn_workers(default_governor_state().as_deref());
    let (tx, rx) = sync_channel::<PendingConn>(PENDING_CONN_CAPACITY);
    let rx = Arc::new(Mutex::new(rx));
    for _ in 0..workers {
        let rx = Arc::clone(&rx);
        let app_ref = Arc::clone(&app);
        std::thread::spawn(move || loop {
            let conn = match rx
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .recv()
            {
                Ok(conn) => conn,
                Err(_) => break,
            };
            if conn.enqueued.elapsed() > PENDING_CONN_DEADLINE {
                app_ref.conn.expired.fetch_add(1, Ordering::Relaxed);
                continue;
            }
            handle_connection(conn.stream, &app_ref);
            app_ref.conn.completed.fetch_add(1, Ordering::Relaxed);
        });
    }
    eprintln!(
        "ragd: listening on {} ({} conn_workers={} pending_cap={})",
        bind, CONTRACT, workers, PENDING_CONN_CAPACITY
    );
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let pending = PendingConn {
                    stream,
                    enqueued: Instant::now(),
                };
                match tx.try_send(pending) {
                    Ok(()) => {
                        app.conn.submitted.fetch_add(1, Ordering::Relaxed);
                    }
                    Err(TrySendError::Full(returned))
                    | Err(TrySendError::Disconnected(returned)) => {
                        app.conn.rejected.fetch_add(1, Ordering::Relaxed);
                        reject_over_capacity(returned.stream);
                    }
                }
            }
            Err(e) => eprintln!("ragd: accept failed: {}", e),
        }
    }
}

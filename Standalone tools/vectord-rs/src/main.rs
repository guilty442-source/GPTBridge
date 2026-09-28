//! vectord — GPTBridge governed Rust vector engine.
//!
//! Contract `vectord/v1`: loopback-only HTTP/JSON service holding the
//! semantic index as rebuildable derived data.  It never holds formal
//! authority — PostgreSQL owns canonical source data and resolves
//! scope/revision/tombstone on the candidate IDs this engine returns
//! (codex A610 DATA-ARCHITECTURE-TARGET / DATA-SAFETY).

mod store;

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::{json, Value};
use store::{Filter, SnapshotFile, Store};

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

#[derive(Deserialize)]
struct EnsureRequest {
    name: String,
    dimension: usize,
}

#[derive(Deserialize)]
struct AliasSetRequest {
    alias: String,
    collection: String,
}

#[derive(Deserialize)]
struct AliasGetRequest {
    alias: String,
}

#[derive(Deserialize)]
struct PointIn {
    id: String,
    vector: Vec<f32>,
    #[serde(default)]
    payload: Value,
}

#[derive(Deserialize)]
struct UpsertRequest {
    collection: String,
    points: Vec<PointIn>,
}

#[derive(Deserialize)]
struct SearchRequest {
    collection: String,
    vector: Vec<f32>,
    #[serde(default)]
    top_k: Option<usize>,
    #[serde(default)]
    score_threshold: Option<f32>,
    #[serde(default)]
    filter: Filter,
}

#[derive(Deserialize)]
struct FilteredRequest {
    collection: String,
    #[serde(default)]
    filter: Filter,
}

#[derive(Deserialize)]
struct InfoRequest {
    name: String,
}

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
    let payload = body.to_string();
    let response = format!(
        "HTTP/1.1 {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        status,
        payload.len()
    );
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.write_all(payload.as_bytes());
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

fn route(app: &App, method: &str, path: &str, body: &[u8]) -> Value {
    match (method, path) {
        ("GET", "/healthz") => {
            let (collections, points) = app.store.stats();
            ok(json!({
                "service": "vectord",
                "version": VERSION,
                "collections": collections,
                "points": points,
                "uptime_s": app.started.elapsed().as_secs(),
            }))
        }
        ("POST", "/v1/collections/ensure") => {
            let req: EnsureRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(_) => return err("INVALID_JSON"),
            };
            match app.store.ensure_collection(&req.name, req.dimension) {
                Ok(()) => {
                    app.dirty.store(true, Ordering::Relaxed);
                    ok(json!({"collection": req.name, "dimension": req.dimension}))
                }
                Err(e) => err(&e),
            }
        }
        ("POST", "/v1/collections/info") => {
            let req: InfoRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(_) => return err("INVALID_JSON"),
            };
            match app.store.collection_info(&req.name) {
                Some((points_count, dimension)) => ok(json!({
                    "collection": req.name,
                    "points_count": points_count,
                    "dimension": dimension,
                })),
                None => err("COLLECTION_MISSING"),
            }
        }
        ("POST", "/v1/collections/list") => {
            let names = app.store.collection_names();
            ok(json!({"collections": names}))
        }
        ("POST", "/v1/collections/delete") => {
            let req: InfoRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(_) => return err("INVALID_JSON"),
            };
            let existed = app.store.drop_collection(&req.name);
            if existed {
                app.dirty.store(true, Ordering::Relaxed);
            }
            ok(json!({"collection": req.name, "existed": existed}))
        }
        ("POST", "/v1/aliases/set") => {
            let req: AliasSetRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(_) => return err("INVALID_JSON"),
            };
            app.store.set_alias(&req.alias, &req.collection);
            ok(json!({"alias": req.alias, "collection": req.collection}))
        }
        ("POST", "/v1/aliases/get") => {
            let req: AliasGetRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(_) => return err("INVALID_JSON"),
            };
            match app.store.get_alias(&req.alias) {
                Some(collection) => ok(json!({"alias": req.alias, "collection": collection})),
                None => err("ALIAS_MISSING"),
            }
        }
        ("POST", "/v1/aliases/list") => {
            ok(json!({
                "aliases": app
                    .store
                    .alias_pairs()
                    .into_iter()
                    .map(|(a, c)| json!({"alias_name": a, "collection_name": c}))
                    .collect::<Vec<_>>(),
            }))
        }
        ("POST", "/v1/aliases/delete") => {
            let req: AliasGetRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(_) => return err("INVALID_JSON"),
            };
            let existed = app.store.delete_alias(&req.alias);
            if existed {
                app.dirty.store(true, Ordering::Relaxed);
            }
            ok(json!({"alias": req.alias, "existed": existed}))
        }
        ("POST", "/v1/points/upsert") => {
            let req: UpsertRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(_) => return err("INVALID_JSON"),
            };
            let points: Vec<(String, Vec<f32>, Value)> = req
                .points
                .into_iter()
                .map(|p| (p.id, p.vector, p.payload))
                .collect();
            match app.store.upsert_points(&req.collection, points) {
                Ok(n) => {
                    app.dirty.store(true, Ordering::Relaxed);
                    ok(json!({"upserted": n}))
                }
                Err(e) => err(&e),
            }
        }
        ("POST", "/v1/search") => {
            let req: SearchRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(_) => return err("INVALID_JSON"),
            };
            let top_k = req.top_k.unwrap_or(10).min(256);
            let threshold = req.score_threshold.unwrap_or(0.0);
            match app
                .store
                .search(&req.collection, &req.vector, top_k, threshold, &req.filter)
            {
                Ok(hits) => ok(json!({
                    "hits": hits
                        .into_iter()
                        .map(|(id, score, payload)| json!({
                            "id": id,
                            "score": score,
                            "payload": payload,
                        }))
                        .collect::<Vec<_>>(),
                })),
                Err(e) => err(&e),
            }
        }
        ("POST", "/v1/points/delete") => {
            let req: FilteredRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(_) => return err("INVALID_JSON"),
            };
            match app.store.delete_where(&req.collection, &req.filter) {
                Ok(n) => {
                    app.dirty.store(true, Ordering::Relaxed);
                    ok(json!({"deleted": n}))
                }
                Err(e) => err(&e),
            }
        }
        ("POST", "/v1/points/count") => {
            let req: FilteredRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(_) => return err("INVALID_JSON"),
            };
            match app.store.count_where(&req.collection, &req.filter) {
                Ok(n) => ok(json!({"count": n})),
                Err(e) => err(&e),
            }
        }
        ("POST", "/v1/snapshot") => match persist_snapshot(&app.store, &app.store_dir) {
            Ok(path) => {
                app.dirty.store(false, Ordering::Relaxed);
                ok(json!({"snapshot": path.to_string_lossy()}))
            }
            Err(e) => err(&format!("SNAPSHOT_FAILED:{}", e)),
        },
        _ => err("NOT_FOUND"),
    }
}

fn persist_snapshot(store: &Store, dir: &PathBuf) -> Result<PathBuf, String> {
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let target = dir.join(SNAPSHOT_NAME);
    let tmp = dir.join(format!("{}.tmp", SNAPSHOT_NAME));
    let bytes = bincode::serialize(&store.dump()).map_err(|e| e.to_string())?;
    std::fs::write(&tmp, bytes).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, &target).map_err(|e| e.to_string())?;
    Ok(target)
}

fn load_snapshot(dir: &PathBuf, capacity_hint: usize) -> Option<Store> {
    let path = dir.join(SNAPSHOT_NAME);
    let bytes = std::fs::read(&path).ok()?;
    let dump: SnapshotFile = bincode::deserialize(&bytes).ok()?;
    Store::load(dump, capacity_hint).ok()
}

fn handle_connection(mut stream: TcpStream, app: Arc<App>) {
    match read_request(&mut stream) {
        Ok((method, path, body)) => {
            let response = route(&app, &method, &path, &body);
            send_response(&mut stream, "200 OK", &response);
        }
        Err(e) => send_response(&mut stream, "400 Bad Request", &err(&e)),
    }
}

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
    let (tx, rx) = std::sync::mpsc::sync_channel::<TcpStream>(PENDING_CONN_CAPACITY);
    let rx = Arc::new(std::sync::Mutex::new(rx));
    for i in 0..workers {
        let rx = rx.clone();
        let app = app.clone();
        thread::Builder::new()
            .name(format!("vectord-conn-{}", i))
            .spawn(move || loop {
                let stream = {
                    let guard = match rx.lock() {
                        Ok(g) => g,
                        Err(_) => return,
                    };
                    match guard.recv() {
                        Ok(s) => s,
                        Err(_) => return,
                    }
                };
                handle_connection(stream, app.clone());
            })
            .expect("vectord conn worker spawn");
    }
    eprintln!(
        "vectord: listening on {} (contract {}, conn_workers={}, pending_cap={})",
        bind, CONTRACT, workers, PENDING_CONN_CAPACITY
    );

    for connection in listener.incoming() {
        match connection {
            Ok(stream) => match tx.try_send(stream) {
                Ok(_) => {}
                Err(std::sync::mpsc::TrySendError::Full(s))
                | Err(std::sync::mpsc::TrySendError::Disconnected(s)) => {
                    reject_over_capacity(s);
                }
            },
            Err(e) => eprintln!("vectord: accept failed: {}", e),
        }
    }

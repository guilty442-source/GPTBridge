//! ragd — GPTBridge governed Rust RAG orchestration service.
//!
//! Contract `ragd/v1` (increment 1): canonical dense query read path.
//! vectord generates candidates; PostgreSQL stays the sole authority —
//! every hit is proved against `gptbridge_rag.chunk` + `gptbridge_index.
//! resource` + `gptbridge_rag.tombstone` + `gptbridge_rag.index_state`
//! before entering the evidence pool (A610/A374, pipeline.py read
//! barrier).  Loopback-only; PG failure is fail-closed (zero hits).

mod barrier;
mod dsn;
mod http;
mod pg;
mod vectord;

use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

const CONTRACT: &str = "ragd/v1";
const VERSION: &str = env!("CARGO_PKG_VERSION");
const DEFAULT_BIND: &str = "127.0.0.1:8094";
const DEFAULT_VECTORD: &str = "http://127.0.0.1:8092";
const MAX_BODY_BYTES: usize = 8 * 1024 * 1024;
const MAX_HEADER_BYTES: usize = 16 * 1024;

struct App {
    vectord_base: String,
    dsn: Option<String>,
    pg: Mutex<Option<pg::Authority>>,
    started: Instant,
}

impl App {
    /// Lazily (re)connect the authority client. A dead connection is
    /// dropped and retried once per call — never cached across errors.
    fn with_pg<T>(
        &self,
        f: impl FnOnce(&mut pg::Authority) -> Result<T, String>,
    ) -> Result<T, String> {
        let mut guard = self.pg.lock().map_err(|_| "PG_LOCK".to_string())?;
        if guard.is_none() {
            let dsn = self
                .dsn
                .clone()
                .ok_or_else(|| "DSN_UNAVAILABLE".to_string())?;
            *guard = Some(pg::Authority::connect(&dsn)?);
        }
        match f(guard.as_mut().unwrap()) {
            Ok(v) => Ok(v),
            Err(e) => {
                *guard = None;
                Err(e)
            }
        }
    }
}

fn is_loopback_bind(bind: &str) -> bool {
    let host = bind
        .rsplit_once(':')
        .map(|(h, _)| h.trim().to_ascii_lowercase())
        .unwrap_or_else(|| bind.trim().to_ascii_lowercase());
    matches!(host.as_str(), "127.0.0.1" | "localhost" | "::1" | "[::1]")
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

fn ok(extra: Value) -> Value {
    let mut map = extra.as_object().cloned().unwrap_or_default();
    map.insert("ok".to_string(), Value::Bool(true));
    map.insert("contract".to_string(), json!(CONTRACT));
    Value::Object(map)
}

fn err(code: &str) -> Value {
    json!({"ok": false, "contract": CONTRACT, "error": code})
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
        None => match app.with_pg(|pg| pg.active_generation(alias)) {
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
        match app.with_pg(|pg| {
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

fn route(app: &App, method: &str, path: &str, body: &[u8]) -> Value {
    match (method, path) {
        ("GET", "/healthz") => ok(json!({
            "service": "ragd",
            "version": VERSION,
            "vectord": app.vectord_base,
            "dsn_configured": app.dsn.is_some(),
            "uptime_s": app.started.elapsed().as_secs(),
        })),
        ("POST", "/v1/rag/query") => handle_query(app, body),
        _ => err("NOT_FOUND"),
    }
}

fn handle_connection(mut stream: TcpStream, app: &App) {
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
    let mut dsn_arg: Option<String> = None;
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
            "--dsn" if i + 1 < args.len() => {
                dsn_arg = Some(args[i + 1].clone());
                i += 2;
            }
            _ => i += 1,
        }
    }

    // Fail-closed: governed read path is local-owned; non-loopback
    // refused outright rather than warned (same contract as vectord).
    if !is_loopback_bind(&bind) {
        eprintln!("ragd: refusing non-loopback bind {}", bind);
        std::process::exit(2);
    }

    let dsn = match dsn::resolve_dsn(dsn_arg.as_deref()) {
        Ok(d) => Some(d),
        Err(e) => {
            eprintln!("ragd: {} — query path will fail closed", e);
            None
        }
    };

    let app = App {
        vectord_base,
        dsn,
        pg: Mutex::new(None),
        started: Instant::now(),
    };
    let listener = match TcpListener::bind(&bind) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("ragd: bind {} failed: {}", bind, e);
            std::process::exit(1);
        }
    };
    eprintln!("ragd: listening on {} ({})", bind, CONTRACT);
    for stream in listener.incoming() {
        if let Ok(stream) = stream {
            let app_ref = &app;
            std::thread::scope(|s| {
                s.spawn(|| handle_connection(stream, app_ref));
            });
        }
    }
}

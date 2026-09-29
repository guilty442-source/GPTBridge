//! vectord route/snapshot/connection surface (B94 split of main.rs).

use std::net::TcpStream;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::Ordering;

use serde_json::{Value, json};

use crate::embed;
use crate::requests::{
    AliasGetRequest, AliasSetRequest, EmbedRequest, EnsureRequest,
    FilteredRequest, InfoRequest, SearchRequest, SearchTextRequest,
    UpsertRequest, UpsertTextRequest,
};
use crate::store::{SnapshotFile, Store};
use crate::{SNAPSHOT_NAME, VERSION,
    App, err, ok, read_request, send_bytes, send_response,
};

pub(crate) fn route(app: &App, method: &str, path: &str, body: &[u8]) -> Value {
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
        ("POST", "/v1/points/upsert_text") => {
            // PERF-07: callers send text; the embedding is computed inside
            // the owning engine — no vector serialisation crosses the wire.
            let req: UpsertTextRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(_) => return err("INVALID_JSON"),
            };
            let dimension = match app.store.collection_info(&req.collection) {
                Some((_, dim)) => dim,
                None => return err("COLLECTION_MISSING"),
            };
            let points: Vec<(String, Vec<f32>, Value)> = req
                .points
                .into_iter()
                .map(|p| {
                    let vector = embed::embed(&p.text, dimension);
                    (p.id, vector, p.payload)
                })
                .collect();
            match app.store.upsert_points(&req.collection, points) {
                Ok(n) => {
                    app.dirty.store(true, Ordering::Relaxed);
                    ok(json!({"upserted": n}))
                }
                Err(e) => err(&e),
            }
        }
        ("POST", "/v1/search_text") => {
            let req: SearchTextRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(_) => return err("INVALID_JSON"),
            };
            let dimension = match app.store.collection_info(&req.collection) {
                Some((_, dim)) => dim,
                None => return err("COLLECTION_MISSING"),
            };
            let query = embed::embed(&req.text, dimension);
            let top_k = req.top_k.unwrap_or(10).min(256);
            let threshold = req.score_threshold.unwrap_or(0.0);
            match app
                .store
                .search(&req.collection, &query, top_k, threshold, &req.filter)
            {
                Ok(hits) => ok(json!({
                    "hits": hits
                        .into_iter()
                        .map(|(id, score, payload)| json!({
                            "id": id,
                            "score": score,
                            "payload": payload,
                        }))
                        .collect::<Vec<_>>()
                })),
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

pub(crate) fn persist_snapshot(store: &Store, dir: &PathBuf) -> Result<PathBuf, String> {
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let target = dir.join(SNAPSHOT_NAME);
    let tmp = dir.join(format!("{}.tmp", SNAPSHOT_NAME));
    let bytes = bincode::serialize(&store.dump()).map_err(|e| e.to_string())?;
    std::fs::write(&tmp, bytes).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, &target).map_err(|e| e.to_string())?;
    Ok(target)
}

pub(crate) fn load_snapshot(dir: &PathBuf, capacity_hint: usize) -> Option<Store> {
    let path = dir.join(SNAPSHOT_NAME);
    let bytes = std::fs::read(&path).ok()?;
    let dump: SnapshotFile = bincode::deserialize(&bytes).ok()?;
    Store::load(dump, capacity_hint).ok()
}

/// `/v1/embed` — batch text -> concatenated canonical f64-le embedding
/// bytes (application/octet-stream).  The bytes equal
/// `pack_embedding(vector)` per text, so callers persist them straight
/// into the PostgreSQL chunk authority (B61/C56) without ever
/// materialising a float list; vectord re-derives the identical f32
/// vector at upsert_text/search_text time.
pub(crate) fn handle_embed(stream: &mut TcpStream, body: &[u8]) {
    let req: EmbedRequest = match serde_json::from_slice(body) {
        Ok(r) => r,
        Err(_) => {
            send_response(stream, "200 OK", &err("INVALID_JSON"));
            return;
        }
    };
    if req.dimension == 0 || req.dimension > 65536 || req.texts.len() > 512 {
        send_response(stream, "200 OK", &err("INVALID_REQUEST"));
        return;
    }
    let mut out = Vec::with_capacity(req.texts.len() * req.dimension * 8);
    for text in &req.texts {
        for value in embed::embed_f64(text, req.dimension) {
            out.extend_from_slice(&value.to_le_bytes());
        }
    }
    send_bytes(stream, "200 OK", "application/octet-stream", &out);
}

pub(crate) fn handle_connection(mut stream: TcpStream, app: Arc<App>) {
    match read_request(&mut stream) {
        Ok((method, path, body)) => {
            if method == "POST" && path == "/v1/embed" {
                handle_embed(&mut stream, &body);
                return;
            }
            let response = route(&app, &method, &path, &body);
            send_response(&mut stream, "200 OK", &response);
        }
        Err(e) => send_response(&mut stream, "400 Bad Request", &err(&e)),
    }
}

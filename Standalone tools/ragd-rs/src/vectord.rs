//! vectord/v1 client — candidate generation only; authority stays in PG.

use serde_json::{json, Value};

use crate::http;

#[derive(Debug, Clone)]
pub struct Hit {
    pub id: String,
    pub score: f64,
    pub payload: Value,
}

/// Dense candidates for `text`/`vector` restricted to `module_ids`
/// (engine-side pre-filter; the PostgreSQL read barrier re-proves every
/// hit — scope enforcement never depends on this filter).
pub fn search(
    base: &str,
    collection: &str,
    text: Option<&str>,
    vector: Option<Vec<f32>>,
    top_k: usize,
    module_ids: &[String],
) -> Result<Vec<Hit>, String> {
    let filter = json!({
        "must": [{"key": "module_id", "match": {"any": module_ids}}]
    });
    let (path, body) = match (text, vector) {
        (Some(t), _) => (
            "/v1/search_text",
            json!({
                "collection": collection,
                "text": t,
                "top_k": top_k,
                "filter": filter,
            }),
        ),
        (None, Some(v)) => (
            "/v1/search",
            json!({
                "collection": collection,
                "vector": v,
                "top_k": top_k,
                "filter": filter,
            }),
        ),
        (None, None) => return Err("MISSING_QUERY".to_string()),
    };
    let resp = http::post_json(base, path, &body)?;
    if resp.get("ok").and_then(Value::as_bool) != Some(true) {
        let code = resp
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("UPSTREAM_ERROR");
        return Err(format!("vectord:{}", code));
    }
    let hits = resp
        .get("hits")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter_map(|h| {
            Some(Hit {
                id: h.get("id")?.as_str()?.to_string(),
                score: h.get("score").and_then(Value::as_f64).unwrap_or(0.0),
                payload: h.get("payload").cloned().unwrap_or(Value::Null),
            })
        })
        .collect();
    Ok(hits)
}

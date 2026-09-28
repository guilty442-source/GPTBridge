//! vectord/v1 request DTOs — wire shapes only, no behaviour.

use serde::Deserialize;
use serde_json::Value;

use crate::store::Filter;

#[derive(Deserialize)]
pub(crate) struct EnsureRequest {
    pub name: String,
    pub dimension: usize,
}

#[derive(Deserialize)]
pub(crate) struct AliasSetRequest {
    pub alias: String,
    pub collection: String,
}

#[derive(Deserialize)]
pub(crate) struct AliasGetRequest {
    pub alias: String,
}

#[derive(Deserialize)]
pub(crate) struct PointIn {
    pub id: String,
    pub vector: Vec<f32>,
    #[serde(default)]
    pub payload: Value,
}

#[derive(Deserialize)]
pub(crate) struct UpsertRequest {
    pub collection: String,
    pub points: Vec<PointIn>,
}

#[derive(Deserialize)]
pub(crate) struct SearchRequest {
    pub collection: String,
    pub vector: Vec<f32>,
    #[serde(default)]
    pub top_k: Option<usize>,
    #[serde(default)]
    pub score_threshold: Option<f32>,
    #[serde(default)]
    pub filter: Filter,
}

#[derive(Deserialize)]
pub(crate) struct FilteredRequest {
    pub collection: String,
    #[serde(default)]
    pub filter: Filter,
}

#[derive(Deserialize)]
pub(crate) struct InfoRequest {
    pub name: String,
}

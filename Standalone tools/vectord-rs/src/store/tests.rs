use super::*;

use serde_json::json;

fn store_with_points() -> Store {
    let store = Store::new(1024);
    store.ensure_collection("coll", 4).unwrap();
    let points = vec![
        ("a".to_string(), vec![1.0, 0.0, 0.0, 0.0], json!({"module_id": "m1", "resource_id": "r1"})),
        ("b".to_string(), vec![0.0, 1.0, 0.0, 0.0], json!({"module_id": "m2", "resource_id": "r2"})),
        ("c".to_string(), vec![0.9, 0.1, 0.0, 0.0], json!({"module_id": "m1", "resource_id": "r3"})),
    ];
    store.upsert_points("coll", points).unwrap();
    store
}

#[test]
fn scoped_search_only_returns_scoped_module() {
    let store = store_with_points();
    let filter: Filter = serde_json::from_value(json!({
        "must": [{"key": "module_id", "match": {"any": ["m1"]}}]
    }))
    .unwrap();
    let hits = store.search("coll", &[1.0, 0.0, 0.0, 0.0], 10, 0.0, &filter).unwrap();
    assert_eq!(hits.len(), 2);
    assert!(hits.iter().all(|(id, _, _)| id != "b"));
}

#[test]
fn delete_tombstones_and_verify_zero() {
    let store = store_with_points();
    let filter: Filter = serde_json::from_value(json!({
        "must": [{"key": "module_id", "match": {"value": "m1"}}],
        "should": [
            {"key": "resource_id", "match": {"value": "r1"}},
            {"key": "document_resource_id", "match": {"value": "r1"}}
        ]
    }))
    .unwrap();
    assert_eq!(store.delete_where("coll", &filter).unwrap(), 1);
    assert_eq!(store.count_where("coll", &filter).unwrap(), 0);
    let scope: Filter = serde_json::from_value(json!({
        "must": [{"key": "module_id", "match": {"any": ["m1"]}}]
    }))
    .unwrap();
    let hits = store.search("coll", &[1.0, 0.0, 0.0, 0.0], 10, 0.0, &scope).unwrap();
    assert_eq!(hits.len(), 1);
}

#[test]
fn snapshot_round_trip() {
    let store = store_with_points();
    let dump = store.dump();
    let bytes = bincode::serialize(&dump).unwrap();
    let loaded: SnapshotFile = bincode::deserialize(&bytes).unwrap();
    let store2 = Store::load(loaded, 1024).unwrap();
    let filter = Filter::default();
    let hits = store2.search("coll", &[0.0, 1.0, 0.0, 0.0], 1, 0.0, &filter).unwrap();
    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0].0, "b");
}

#[test]
fn dimension_mismatch_rejected() {
    let store = Store::new(1024);
    store.ensure_collection("coll", 4).unwrap();
    assert!(store.ensure_collection("coll", 8).is_err());
    let err = store
        .upsert_points("coll", vec![("x".into(), vec![1.0; 8], json!({}))])
        .unwrap_err();
    assert!(err.contains("DIMENSION_MISMATCH"));
}

#[test]
fn exact_match_query_still_returns_other_neighbours() {
    // Reproduce: query identical to a stored vector must not starve the
    // remaining live points out of the top_k window.
    let store = Store::new(1024);
    store.ensure_collection("coll", 8).unwrap();
    store
        .upsert_points(
            "coll",
            vec![
                ("a".into(), [1.0_f32].into_iter().chain([0.0; 7]).collect::<Vec<f32>>(), json!({"module_id": "m1"})),
                ("b".into(), [0.9_f32, 0.1].into_iter().chain([0.0; 6]).collect::<Vec<f32>>(), json!({"module_id": "m2"})),
            ],
        )
        .unwrap();
    let hits = store
        .search("coll", &[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], 10, -1.0, &Filter::default())
        .unwrap();
    assert_eq!(hits.len(), 2, "hits: {:?}", hits.iter().map(|h| &h.0).collect::<Vec<_>>());
}

#[test]
fn alias_resolves_to_collection() {
    let store = store_with_points();
    store.set_alias("alias", "coll");
    assert_eq!(store.get_alias("alias").as_deref(), Some("coll"));
    let hits = store
        .search("alias", &[1.0, 0.0, 0.0, 0.0], 5, 0.0, &Filter::default())
        .unwrap();
    assert_eq!(hits.len(), 3);
}

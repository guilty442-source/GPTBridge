//! Cached public entry points for the size dashboard.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use super::inventory::build_inventory;
use super::measure::folder_size;
use super::{
    now_ms, size_cache, CACHE_TTL_MS, MAIN_SYSTEM_DEPENDENCY_DIRECTORIES,
};

pub fn platform_tool_sizes(workspace_root: &Path, force_refresh: bool) -> serde_json::Value {
    let resolved_root = workspace_root
        .canonicalize()
        .unwrap_or_else(|_| workspace_root.to_path_buf());
    let now = now_ms();
    {
        let cache = size_cache().lock().unwrap();
        if !force_refresh {
            if let (Some((expires, tools)), Some(root)) = (&cache.inventory, &cache.inventory_root)
            {
                if *expires > now && *root == resolved_root {
                    return tools.clone();
                }
            }
        }
    }
    let tools = serde_json::Value::Array(build_inventory(&resolved_root));
    let mut cache = size_cache().lock().unwrap();
    cache.inventory = Some((now + CACHE_TTL_MS, tools.clone()));
    cache.inventory_root = Some(resolved_root);
    tools
}

pub fn main_system_size(workspace_root: &Path, force_refresh: bool) -> serde_json::Value {
    let resolved_root = workspace_root
        .canonicalize()
        .unwrap_or_else(|_| workspace_root.to_path_buf());
    let now = now_ms();
    {
        let cache = size_cache().lock().unwrap();
        if !force_refresh {
            if let Some((expires, value)) = &cache.main_system {
                if *expires > now {
                    return value.clone();
                }
            }
        }
    }
    let main_root = resolved_root.join("main-system");
    let excluded: HashSet<String> = MAIN_SYSTEM_DEPENDENCY_DIRECTORIES
        .iter()
        .map(|s| s.to_string())
        .collect();
    let size = folder_size(&main_root, &excluded, false);
    let mut dep_bytes = 0u64;
    let mut dep_files = 0u64;
    for dir in MAIN_SYSTEM_DEPENDENCY_DIRECTORIES {
        let dep = folder_size(&main_root.join(dir), &HashSet::new(), false);
        dep_bytes += dep.bytes;
        dep_files += dep.file_count;
    }
    let value = serde_json::json!({
        "folder_path": main_root.to_string_lossy(),
        "project_size_bytes": size.bytes,
        "file_count": size.file_count,
        "dependency_size_bytes": dep_bytes,
        "dependency_file_count": dep_files,
        "total_size_bytes": size.bytes + dep_bytes,
        "total_file_count": size.file_count + dep_files,
    });
    size_cache().lock().unwrap().main_system = Some((now + CACHE_TTL_MS, value.clone()));
    value
}

pub fn shared_layer_size(workspace_root: &Path, force_refresh: bool) -> serde_json::Value {
    let resolved_root = workspace_root
        .canonicalize()
        .unwrap_or_else(|_| workspace_root.to_path_buf());
    let now = now_ms();
    {
        let cache = size_cache().lock().unwrap();
        if !force_refresh {
            if let Some((expires, value)) = &cache.shared_layer {
                if *expires > now {
                    return value.clone();
                }
            }
        }
    }
    let shared_root = resolved_root.join("shared-layer");
    let size = folder_size(&shared_root, &HashSet::new(), false);
    let value = serde_json::json!({
        "folder_path": shared_root.to_string_lossy(),
        "project_size_bytes": size.bytes,
        "file_count": size.file_count,
    });
    size_cache().lock().unwrap().shared_layer = Some((now + CACHE_TTL_MS, value.clone()));
    value
}

fn collect_measured_roots(
    tools: &serde_json::Value,
    main_system: &serde_json::Value,
    shared_layer: &serde_json::Value,
) -> Vec<(PathBuf, u64, u64)> {
    let mut measured_roots: Vec<(PathBuf, u64, u64)> = Vec::new();
    let push_root =
        |roots: &mut Vec<(PathBuf, u64, u64)>, value: &serde_json::Value, bytes_key: &str| {
            if let Some(p) = value["folder_path"].as_str() {
                roots.push((
                    PathBuf::from(p),
                    value[bytes_key].as_u64().unwrap_or(0),
                    value["file_count"].as_u64().unwrap_or(0),
                ));
            }
        };
    push_root(&mut measured_roots, main_system, "total_size_bytes");
    push_root(&mut measured_roots, shared_layer, "project_size_bytes");
    if let Some(list) = tools.as_array() {
        for tool in list {
            push_root(&mut measured_roots, tool, "project_size_bytes");
        }
    }
    measured_roots
}

fn sum_workspace_entries(
    resolved_root: &Path,
    measured_roots: &[(PathBuf, u64, u64)],
) -> Option<(u64, u64)> {
    let mut bytes = 0u64;
    let mut file_count = 0u64;
    let entries = fs::read_dir(resolved_root).ok()?;
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(file_type) = entry.file_type() else {
            continue;
        };
        if file_type.is_symlink() {
            continue;
        }
        if file_type.is_dir() {
            let known = measured_roots
                .iter()
                .find(|(root, _, _)| *root == path)
                .map(|(_, b, f)| (*b, *f));
            let (b, f) = match known {
                Some(v) => v,
                None => {
                    let size = folder_size(&path, &HashSet::new(), false);
                    (size.bytes, size.file_count)
                }
            };
            bytes = bytes.saturating_add(b);
            file_count += f;
        } else if file_type.is_file() {
            if let Ok(meta) = entry.metadata() {
                bytes = bytes.saturating_add(meta.len());
                file_count += 1;
            }
        }
    }
    Some((bytes, file_count))
}

pub fn workspace_size(
    workspace_root: &Path,
    tools: &serde_json::Value,
    main_system: &serde_json::Value,
    shared_layer: &serde_json::Value,
    force_refresh: bool,
) -> serde_json::Value {
    let resolved_root = workspace_root
        .canonicalize()
        .unwrap_or_else(|_| workspace_root.to_path_buf());
    let now = now_ms();
    {
        let cache = size_cache().lock().unwrap();
        if !force_refresh {
            if let Some((expires, value)) = &cache.workspace {
                if *expires > now {
                    return value.clone();
                }
            }
        }
    }
    let measured_roots = collect_measured_roots(tools, main_system, shared_layer);
    let Some((bytes, file_count)) = sum_workspace_entries(&resolved_root, &measured_roots) else {
        return serde_json::json!({
            "folder_path": resolved_root.to_string_lossy(),
            "project_size_bytes": 0,
            "file_count": 0,
            "error": "workspace-unreadable",
        });
    };
    let value = serde_json::json!({
        "folder_path": resolved_root.to_string_lossy(),
        "project_size_bytes": bytes,
        "file_count": file_count,
    });
    size_cache().lock().unwrap().workspace = Some((now + CACHE_TTL_MS, value.clone()));
    value
}

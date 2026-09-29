//! Manifest discovery + tool inventory construction.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use crate::native::paths::is_path_inside;

use super::measure::{folder_size, list_direct_child_folders, ToolSizeBreakdown};

fn declares_independent_tool_card(manifest: &serde_json::Value) -> bool {
    manifest["main_system_independent_tool"].as_bool() == Some(true)
        && manifest["companion_tool"].as_bool() != Some(true)
        && manifest["hidden_from_toolbox"].as_bool() != Some(true)
}

fn valid_tool_id(id: &str) -> bool {
    !id.is_empty()
        && id
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
}

fn collect_candidate_folders(resolved_root: &Path) -> Vec<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    let mut seen: HashSet<PathBuf> = HashSet::new();
    let mut push = |folder: PathBuf| {
        let manifest = folder.join("manifest.json");
        if seen.insert(manifest.clone()) {
            candidates.push(folder);
        }
    };
    for folder in list_direct_child_folders(resolved_root) {
        push(folder);
    }
    let tools_root = resolved_root.join("Standalone tools");
    for host in list_direct_child_folders(&tools_root) {
        push(host.clone());
        for nested in list_direct_child_folders(&host) {
            push(nested.clone());
            for deeper in list_direct_child_folders(&nested) {
                push(deeper);
            }
        }
    }
    candidates
}

fn resolve_code_path(folder: &Path, manifest: &serde_json::Value) -> PathBuf {
    let raw_entry = manifest["runtime"]["entry"]
        .as_str()
        .or_else(|| manifest["entry"].as_str())
        .unwrap_or_default()
        .trim()
        .to_string();
    if raw_entry.is_empty() {
        return folder.to_path_buf();
    }
    let resolved = folder.join(&raw_entry);
    if is_path_inside(folder, &resolved) {
        resolved
    } else {
        folder.to_path_buf()
    }
}

pub(super) fn build_inventory(resolved_root: &Path) -> Vec<serde_json::Value> {
    let mut tools = Vec::new();
    for folder in collect_candidate_folders(resolved_root) {
        let folder = folder.canonicalize().unwrap_or(folder);
        if !is_path_inside(resolved_root, &folder) {
            continue;
        }
        let manifest_path = folder.join("manifest.json");
        let Ok(raw) = fs::read_to_string(&manifest_path) else {
            continue;
        };
        let Ok(manifest) = serde_json::from_str::<serde_json::Value>(&raw) else {
            continue;
        };
        if !declares_independent_tool_card(&manifest) {
            continue;
        }
        let id = manifest["id"]
            .as_str()
            .unwrap_or_default()
            .trim()
            .to_string();
        let folder_name = folder
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_default();
        if !valid_tool_id(&id) || id != folder_name {
            continue;
        }
        let code_path = resolve_code_path(&folder, &manifest);
        let size = folder_size(&folder, &HashSet::new(), true);
        tools.push(serde_json::json!({
            "id": id,
            "folder_path": folder.to_string_lossy(),
            "manifest_path": manifest_path.to_string_lossy(),
            "code_path": code_path.to_string_lossy(),
            "project_size_bytes": size.bytes,
            "file_count": size.file_count,
            "size_breakdown": size
                .breakdown
                .map(|b| b.to_json())
                .unwrap_or_else(|| ToolSizeBreakdown::default().to_json()),
        }));
    }
    tools
}

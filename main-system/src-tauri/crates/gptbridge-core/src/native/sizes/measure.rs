//! Folder-size measurement primitives.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use super::{TOOL_CACHE_SEGMENTS, TOOL_RUNTIME_ROOTS, TOOL_USER_DATA_RUNTIME_ROOTS};

#[derive(Clone, Copy, Default)]
pub(super) struct CategorySize {
    pub size_bytes: u64,
    pub file_count: u64,
}

#[derive(Clone, Copy, Default)]
pub(super) struct ToolSizeBreakdown {
    pub program: CategorySize,
    pub runtime: CategorySize,
    pub user_data: CategorySize,
    pub cache: CategorySize,
    pub backups: CategorySize,
}

impl ToolSizeBreakdown {
    pub(super) fn to_json(&self) -> serde_json::Value {
        let cat = |c: &CategorySize| serde_json::json!({"size_bytes": c.size_bytes, "file_count": c.file_count});
        serde_json::json!({
            "program": cat(&self.program),
            "runtime": cat(&self.runtime),
            "user_data": cat(&self.user_data),
            "cache": cat(&self.cache),
            "backups": cat(&self.backups),
        })
    }
}

pub(super) fn classify_tool_file(relative: &str) -> &'static str {
    let segments: Vec<String> = relative
        .split(['\\', '/'])
        .filter(|s| !s.is_empty())
        .map(|s| s.to_lowercase())
        .collect();
    let file_name = segments.last().cloned().unwrap_or_default();
    let root = segments.first().cloned().unwrap_or_default();
    let runtime_section = if root == "runtime" {
        segments.get(1).cloned().unwrap_or_default()
    } else {
        String::new()
    };

    if segments.iter().any(|s| s == "backup" || s == "backups")
        || segments.iter().any(|s| s.ends_with("_backups"))
        || file_name.ends_with(".bak")
        || file_name.ends_with(".backup")
    {
        return "backups";
    }
    if segments
        .iter()
        .any(|s| TOOL_CACHE_SEGMENTS.contains(&s.as_str()))
    {
        return "cache";
    }
    if root == "data"
        || (root == "runtime"
            && (TOOL_USER_DATA_RUNTIME_ROOTS.contains(&runtime_section.as_str())
                || runtime_section.starts_with("test-self-training")))
    {
        return "user_data";
    }
    if TOOL_RUNTIME_ROOTS.contains(&root.as_str()) || root == "runtime" {
        return "runtime";
    }
    "program"
}

#[derive(Default)]
pub(super) struct FolderSize {
    pub bytes: u64,
    pub file_count: u64,
    pub breakdown: Option<ToolSizeBreakdown>,
}

pub(super) fn folder_size(
    folder: &Path,
    excluded_root_dirs: &HashSet<String>,
    with_breakdown: bool,
) -> FolderSize {
    let mut result = FolderSize::default();
    if with_breakdown {
        result.breakdown = Some(ToolSizeBreakdown::default());
    }
    let resolved_root = folder
        .canonicalize()
        .unwrap_or_else(|_| folder.to_path_buf());
    let mut pending = vec![folder.to_path_buf()];

    while let Some(current) = pending.pop() {
        let Ok(entries) = fs::read_dir(&current) else {
            continue;
        };
        let at_root = current.canonicalize().unwrap_or_else(|_| current.clone()) == resolved_root;
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            let path = entry.path();
            let Ok(meta) = entry.metadata() else {
                continue;
            };
            if meta.is_symlink() {
                continue;
            }
            if meta.is_dir() {
                if at_root && excluded_root_dirs.contains(&name) {
                    continue;
                }
                pending.push(path);
                continue;
            }
            if !meta.is_file() {
                continue;
            }
            result.bytes = result.bytes.saturating_add(meta.len());
            result.file_count += 1;
            if let Some(breakdown) = result.breakdown.as_mut() {
                let rel = pathdiff(&resolved_root, &path);
                let category = classify_tool_file(&rel);
                let slot = match category {
                    "backups" => &mut breakdown.backups,
                    "cache" => &mut breakdown.cache,
                    "user_data" => &mut breakdown.user_data,
                    "runtime" => &mut breakdown.runtime,
                    _ => &mut breakdown.program,
                };
                slot.size_bytes = slot.size_bytes.saturating_add(meta.len());
                slot.file_count += 1;
            }
        }
    }
    result
}

fn pathdiff(base: &Path, path: &Path) -> String {
    path.strip_prefix(base)
        .map(|r| r.to_string_lossy().to_string())
        .unwrap_or_else(|_| path.to_string_lossy().to_string())
}

pub(super) fn list_direct_child_folders(root: &Path) -> Vec<PathBuf> {
    fs::read_dir(root)
        .map(|entries| {
            entries
                .flatten()
                .filter(|e| {
                    e.metadata()
                        .map(|m| m.is_dir() && !m.is_symlink())
                        .unwrap_or(false)
                })
                .map(|e| e.path())
                .collect()
        })
        .unwrap_or_default()
}

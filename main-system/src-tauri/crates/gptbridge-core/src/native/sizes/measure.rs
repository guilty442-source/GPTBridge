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

/// Case-insensitive segment equality with full Unicode-fold parity to the
/// retired ``to_lowercase() ==`` comparison — ASCII fast path has zero
/// allocation, non-ASCII falls back to the fold.
fn seg_eq(seg: &str, target: &str) -> bool {
    seg.eq_ignore_ascii_case(target)
        || (!seg.is_ascii() && seg.to_lowercase() == target)
}

fn seg_ends_with(seg: &str, suffix: &str) -> bool {
    if seg.len() < suffix.len() {
        return false;
    }
    seg.as_bytes()[seg.len() - suffix.len()..]
        .eq_ignore_ascii_case(suffix.as_bytes())
        || (!seg.is_ascii() && seg.to_lowercase().ends_with(suffix))
}

fn seg_starts_with(seg: &str, prefix: &str) -> bool {
    if seg.len() < prefix.len() {
        return false;
    }
    seg.as_bytes()[..prefix.len()].eq_ignore_ascii_case(prefix.as_bytes())
        || (!seg.is_ascii() && seg.to_lowercase().starts_with(prefix))
}

/// Per-file category — allocation-free on the ASCII path (the previous
/// version built a lowercased ``Vec<String>`` of every path segment for
/// every file in the tree).
pub(super) fn classify_tool_file(relative: &str) -> &'static str {
    let mut root = "";
    let mut runtime_section = "";
    let mut file_name = "";
    let mut has_backup_segment = false;
    let mut has_cache_segment = false;
    for (index, segment) in relative
        .split(['\\', '/'])
        .filter(|s| !s.is_empty())
        .enumerate()
    {
        match index {
            0 => root = segment,
            1 => runtime_section = segment,
            _ => {}
        }
        file_name = segment;
        if !has_backup_segment {
            has_backup_segment = seg_eq(segment, "backup")
                || seg_eq(segment, "backups")
                || seg_ends_with(segment, "_backups");
        }
        if !has_cache_segment {
            has_cache_segment = TOOL_CACHE_SEGMENTS
                .iter()
                .any(|s| seg_eq(segment, s));
        }
    }

    if has_backup_segment
        || seg_ends_with(file_name, ".bak")
        || seg_ends_with(file_name, ".backup")
    {
        return "backups";
    }
    if has_cache_segment {
        return "cache";
    }
    let root_is_runtime = seg_eq(root, "runtime");
    if seg_eq(root, "data")
        || (root_is_runtime
            && (TOOL_USER_DATA_RUNTIME_ROOTS
                .iter()
                .any(|s| seg_eq(runtime_section, s))
                || seg_starts_with(runtime_section, "test-self-training")))
    {
        return "user_data";
    }
    if TOOL_RUNTIME_ROOTS.iter().any(|s| seg_eq(root, s)) || root_is_runtime {
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
    // pending is a LIFO stack seeded with the root; only the first pop is
    // the root, so the root check is a plain path compare — the previous
    // per-directory canonicalize() paid one filesystem syscall per dir.
    let mut pending = vec![folder.to_path_buf()];

    while let Some(current) = pending.pop() {
        let Ok(entries) = fs::read_dir(&current) else {
            continue;
        };
        let at_root = current.as_path() == folder;
        for entry in entries.flatten() {
            // file_type() comes from the directory listing itself (no
            // extra syscall on Windows); metadata() is only needed for
            // the file length below.
            let Ok(file_type) = entry.file_type() else {
                continue;
            };
            if file_type.is_symlink() {
                continue;
            }
            let path = entry.path();
            if file_type.is_dir() {
                if at_root {
                    let name = entry.file_name();
                    if name
                        .to_str()
                        .map(|n| excluded_root_dirs.contains(n))
                        .unwrap_or(false)
                    {
                        continue;
                    }
                }
                pending.push(path);
                continue;
            }
            if !file_type.is_file() {
                continue;
            }
            let Ok(meta) = entry.metadata() else {
                continue;
            };
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
                    e.file_type()
                        .map(|t| t.is_dir() && !t.is_symlink())
                        .unwrap_or(false)
                })
                .map(|e| e.path())
                .collect()
        })
        .unwrap_or_default()
}

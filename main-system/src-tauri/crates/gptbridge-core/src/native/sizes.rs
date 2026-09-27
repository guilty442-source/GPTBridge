//! sizes.rs — port of src-ui/main/platform-tool-sizes.ts + platformToolSizeTypes.
//!
//! Folder inventory of the governed workspace for the UI's size dashboard:
//! per-tool size breakdown, main-system project vs dependency split,
//! shared-layer, and the workspace rollup.  Results cache for 30 s.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::OnceLock;

use crate::native::paths::is_path_inside;

const CACHE_TTL_MS: u64 = 30_000;

const MAIN_SYSTEM_DEPENDENCY_DIRECTORIES: [&str; 2] = [".venv", "node_modules"];

const TOOL_RUNTIME_ROOTS: [&str; 7] =
    [".venv", "build", "dist", "env", "node_modules", "release", "venv"];

const TOOL_CACHE_SEGMENTS: [&str; 14] = [
    ".cache", ".pytest_cache", ".ruff_cache", "__pycache__", "browser-profile",
    "browser-profiles", "cache", "caches", "code cache", "edge-profile",
    "electron-user-data", "gpu cache", "temp", "tmp",
];

const TOOL_USER_DATA_RUNTIME_ROOTS: [&str; 4] = ["data", "recovery", "settings", "state"];

#[derive(Clone, Copy, Default)]
struct CategorySize {
    size_bytes: u64,
    file_count: u64,
}

#[derive(Clone, Copy, Default)]
struct ToolSizeBreakdown {
    program: CategorySize,
    runtime: CategorySize,
    user_data: CategorySize,
    cache: CategorySize,
    backups: CategorySize,
}

impl ToolSizeBreakdown {
    fn to_json(&self) -> serde_json::Value {
        let cat = |c: &CategorySize| {
            serde_json::json!({"size_bytes": c.size_bytes, "file_count": c.file_count})
        };
        serde_json::json!({
            "program": cat(&self.program),
            "runtime": cat(&self.runtime),
            "user_data": cat(&self.user_data),
            "cache": cat(&self.cache),
            "backups": cat(&self.backups),
        })
    }
}

fn classify_tool_file(relative: &str) -> &'static str {
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
struct FolderSize {
    bytes: u64,
    file_count: u64,
    breakdown: Option<ToolSizeBreakdown>,
}

fn folder_size(folder: &Path, excluded_root_dirs: &HashSet<String>, with_breakdown: bool) -> FolderSize {
    let mut result = FolderSize::default();
    if with_breakdown {
        result.breakdown = Some(ToolSizeBreakdown::default());
    }
    let resolved_root = folder.canonicalize().unwrap_or_else(|_| folder.to_path_buf());
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

fn list_direct_child_folders(root: &Path) -> Vec<PathBuf> {
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

struct SizeCache {
    inventory: Option<(u64, serde_json::Value)>,
    main_system: Option<(u64, serde_json::Value)>,
    shared_layer: Option<(u64, serde_json::Value)>,
    workspace: Option<(u64, serde_json::Value)>,
    inventory_root: Option<PathBuf>,
}

fn size_cache() -> &'static Mutex<SizeCache> {
    static CACHE: OnceLock<Mutex<SizeCache>> = OnceLock::new();
    CACHE.get_or_init(|| {
        Mutex::new(SizeCache {
            inventory: None,
            main_system: None,
            shared_layer: None,
            workspace: None,
            inventory_root: None,
        })
    })
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn build_inventory(resolved_root: &Path) -> Vec<serde_json::Value> {
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

    let mut tools = Vec::new();
    for folder in candidates {
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
        let id = manifest["id"].as_str().unwrap_or_default().trim().to_string();
        let folder_name = folder
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_default();
        if !valid_tool_id(&id) || id != folder_name {
            continue;
        }
        let raw_entry = manifest["runtime"]["entry"]
            .as_str()
            .or_else(|| manifest["entry"].as_str())
            .unwrap_or_default()
            .trim()
            .to_string();
        let code_path = if raw_entry.is_empty() {
            folder.clone()
        } else {
            let resolved = folder.join(&raw_entry);
            if is_path_inside(&folder, &resolved) {
                resolved
            } else {
                folder.clone()
            }
        };
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

pub fn platform_tool_sizes(workspace_root: &Path, force_refresh: bool) -> serde_json::Value {
    let resolved_root = workspace_root
        .canonicalize()
        .unwrap_or_else(|_| workspace_root.to_path_buf());
    let now = now_ms();
    {
        let cache = size_cache().lock().unwrap();
        if !force_refresh {
            if let (Some((expires, tools)), Some(root)) =
                (&cache.inventory, &cache.inventory_root)
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
    let mut measured_roots: Vec<(PathBuf, u64, u64)> = Vec::new();
    let push_root = |roots: &mut Vec<(PathBuf, u64, u64)>, value: &serde_json::Value, bytes_key: &str| {
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

    let mut bytes = 0u64;
    let mut file_count = 0u64;
    let Ok(entries) = fs::read_dir(&resolved_root) else {
        return serde_json::json!({
            "folder_path": resolved_root.to_string_lossy(),
            "project_size_bytes": 0,
            "file_count": 0,
            "error": "workspace-unreadable",
        });
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(meta) = entry.metadata() else {
            continue;
        };
        if meta.is_symlink() {
            continue;
        }
        if meta.is_dir() {
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
        } else if meta.is_file() {
            bytes = bytes.saturating_add(meta.len());
            file_count += 1;
        }
    }
    let value = serde_json::json!({
        "folder_path": resolved_root.to_string_lossy(),
        "project_size_bytes": bytes,
        "file_count": file_count,
    });
    size_cache().lock().unwrap().workspace = Some((now + CACHE_TTL_MS, value.clone()));
    value
}

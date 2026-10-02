//! tools — governed tool registry and lifecycle.
//!
//! Scans ``<workspace>/Standalone tools/<tool-id>/manifest.json`` and
//! serves the toolbox_* command surface.  Spawn policy follows the
//! retired Python ``ToolboxService`` contract: ``runtime.native_entry``
//! (a tool-root-bound .exe) is spawned directly with the governed
//! environment, the runtime must publish ``/health`` on its allocated
//! IPC port before the UI host is launched, and retired ``python``
//! entries fail closed with ``TOOL_RUNTIME_RETIRED``.
//!
//! B94 decomposition: the registry core + ``toolbox_list_tools`` live in
//! this module; identity resolution, environment construction, runtime
//! entry/probing, UI-host launch and the start/stop lifecycle live in
//! the sibling submodules.

use std::collections::HashMap;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use serde_json::{json, Value};

use gptbridge_core::native::paths;

mod env;
mod identity;
mod lifecycle;
pub(crate) mod reaper;
mod runtime;
mod ui;

pub use lifecycle::{force_close_tool, start_tool, stop_tool};

pub(super) struct RunningTool {
    pub runtime_child: Option<Child>,
    pub runtime_port: u16,
    pub shutdown_token: String,
    pub ui_child: Option<Child>,
}

pub(super) struct RunningRegistry {
    pub tools: HashMap<String, RunningTool>,
}

pub(super) fn running() -> &'static Mutex<RunningRegistry> {
    static RUNNING: std::sync::OnceLock<Mutex<RunningRegistry>> =
        std::sync::OnceLock::new();
    RUNNING.get_or_init(|| Mutex::new(RunningRegistry { tools: HashMap::new() }))
}

pub(super) fn tools_root() -> PathBuf {
    paths::path_library().workspace_root.join("Standalone tools")
}

pub(super) fn workspace_root() -> PathBuf {
    paths::path_library().workspace_root.clone()
}

/// Manifest-discovery parity with the retired
/// ``ToolboxService._build_tool_dir_index``: the tools tree is scanned
/// recursively (``rglob`` semantics) so nested tool roots such as
/// ``local-model/model-dialogue`` resolve to their real directory.
/// Build/runtime noise directories are pruned; depth is capped.
const PRUNE_DIRS: [&str; 10] = [
    "runtime", "target", "node_modules", ".git", "bin", "obj", "dist",
    "publish", "__pycache__", "locales",
];
const MAX_MANIFEST_DEPTH: u8 = 5;

fn scan_manifests(dir: &std::path::Path, depth: u8, out: &mut Vec<(PathBuf, Value)>) {
    if depth > MAX_MANIFEST_DEPTH {
        return;
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let Ok(ftype) = entry.file_type() else {
            continue;
        };
        if ftype.is_dir() {
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if PRUNE_DIRS.contains(&name.as_ref()) {
                continue;
            }
            scan_manifests(&entry.path(), depth + 1, out);
        } else if entry.file_name() == "manifest.json" {
            let Ok(raw) = std::fs::read_to_string(entry.path()) else {
                continue;
            };
            if let Ok(manifest) = serde_json::from_str::<Value>(&raw) {
                if let Some(parent) = entry.path().parent() {
                    out.push((parent.to_path_buf(), manifest));
                }
            }
        }
    }
}

/// All discovered manifests under the tools tree: (tool_dir, manifest).
fn discover_manifests() -> Vec<(PathBuf, Value)> {
    let mut found = Vec::new();
    scan_manifests(&tools_root(), 0, &mut found);
    // The xingcheng enclave lives at the workspace root since its
    // separation from local-model; its institution manifest must stay
    // discoverable for identity and tool_dir resolution.
    scan_manifests(&workspace_root().join("xingcheng"), 0, &mut found);
    found.sort_by(|a, b| a.0.cmp(&b.0));
    found
}

/// Whether the tool's governed runtime is currently live in this
/// backend's registry (started through ``toolbox_start_tool`` and not
/// yet stopped).  Used by the status payload's native-model field.
pub fn governed_tool_running(tool_id: &str) -> bool {
    running()
        .lock()
        .map(|registry| registry.tools.contains_key(tool_id))
        .unwrap_or(false)
}

/// Whether the tool is installed and launchable through the governed
/// native entry: manifest present, not disabled, ``runtime.native_entry``
/// configured.  Availability is about the artifact, not liveness.
pub fn governed_tool_launchable(tool_id: &str) -> bool {
    load_manifest(tool_id)
        .map(|manifest| {
            manifest["enabled"].as_bool().unwrap_or(true)
                && manifest["runtime"]["native_entry"]
                    .as_str()
                    .map(|entry| !entry.trim().is_empty())
                    .unwrap_or(false)
        })
        .unwrap_or(false)
}

pub(super) fn load_manifest(tool_id: &str) -> Option<Value> {
    let path = tools_root().join(tool_id).join("manifest.json");
    if let Ok(raw) = std::fs::read_to_string(&path) {
        if let Ok(manifest) = serde_json::from_str::<Value>(&raw) {
            return Some(manifest);
        }
    }
    // Nested tool roots (``_tool_directory_for_id`` manifest-cache parity).
    discover_manifests()
        .into_iter()
        .map(|(_, manifest)| manifest)
        .find(|m| m["id"].as_str() == Some(tool_id))
}

pub(super) fn tool_dir(tool_id: &str) -> PathBuf {
    let flat = tools_root().join(tool_id);
    if flat.join("manifest.json").is_file() {
        return flat;
    }
    for (dir, manifest) in discover_manifests() {
        if manifest["id"].as_str() == Some(tool_id) {
            return dir;
        }
    }
    flat
}

/// ``_declares_independent_tool_card`` + ``_tool_record_is_active``
/// parity: a manifest owns a toolbox card only when it declares itself
/// an independent tool and is not a companion/hidden/disabled/retired
/// record.
fn declares_tool_card(manifest: &Value) -> bool {
    let independent = manifest["main_system_independent_tool"].as_bool() == Some(true)
        || manifest["independent_tool"].as_bool() == Some(true);
    if !independent {
        return false;
    }
    if manifest["companion_tool"].as_bool() == Some(true)
        || manifest["hidden_from_toolbox"].as_bool() == Some(true)
        || manifest["enabled"].as_bool() == Some(false)
    {
        return false;
    }
    manifest["lifecycle"]["status"]
        .as_str()
        .map(|s| !s.trim().eq_ignore_ascii_case("retired"))
        .unwrap_or(true)
}

fn valid_tool_id(tool_id: &str) -> bool {
    !tool_id.is_empty()
        && tool_id.len() <= 64
        && tool_id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        && tool_id != "main-system"
}

pub(super) fn spawn_hidden(mut command: Command) -> Result<Child, String> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    command.spawn().map_err(|e| format!("SPAWN_FAILED:{e}"))
}

/// The renderer's hydration contract (ui/toolbox/tools/runtimeState.js)
/// consumes the display/launchability fields the retired Python
/// ``ToolboxService`` emitted — recover them from the manifest plus
/// filesystem truth instead of letting every card degrade to its id.
fn list_tool_entry(dir: &PathBuf, tool_id: &str, manifest: &Value, running_tool: bool) -> Value {
    // Locale parity with ``_manifest_to_record``: ``name``/``description``
    // resolve through ``name_key``/``description_key`` against
    // ``locales/zh-TW.json`` (localized override wins over the raw manifest
    // field, which in turn wins over the bare tool id).
    let locale = std::fs::read_to_string(dir.join("locales").join("zh-TW.json"))
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .unwrap_or(Value::Null);
    let localized = |key_field: &str| -> Option<String> {
        let key = manifest[key_field].as_str()?.trim();
        if key.is_empty() {
            return None;
        }
        locale[key]
            .as_str()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
    };
    let name = localized("name_key")
        .or_else(|| manifest["name"].as_str().map(str::to_string))
        .or_else(|| manifest["product"].as_str().map(str::to_string))
        .unwrap_or_else(|| tool_id.to_string());
    let description = localized("description_key")
        .or_else(|| manifest["description"].as_str().map(str::to_string))
        .or_else(|| manifest["product"].as_str().map(str::to_string));
    let executable_path = manifest["executable"]["path"]
        .as_str()
        .map(|rel| dir.join(rel));
    let executable_exists = executable_path
        .as_ref()
        .map(|p| p.is_file())
        .unwrap_or(false);
    let native_entry_exists = manifest["runtime"]["native_entry"]
        .as_str()
        .map(|rel| dir.join(rel).is_file())
        .unwrap_or(false);
    // Launchability truth: a tool is usable when at least one governed
    // runtime lane resolves to a real artifact.  ``retired-python`` runtimes
    // keep failing closed until a native host lands.
    let runtime_available = executable_exists || native_entry_exists;
    let runtime_mode = manifest["distribution"]["mode"]
        .as_str()
        .or_else(|| manifest["launch"]["mode"].as_str());
    let automatic_runtime_mode = match manifest["launch"]["selection"]
        .as_str()
    {
        Some("automatic") => manifest["launch"]["primary"]
            .as_str()
            .map(|primary| {
                if primary.contains("executable") {
                    "executable"
                } else {
                    "governed-source"
                }
            }),
        _ => None,
    };
    let code_path = {
        let src = dir.join("src");
        if src.is_dir() {
            src.to_string_lossy().to_string()
        } else {
            String::new()
        }
    };
    json!({
        "id": tool_id,
        "name": name,
        "description": description,
        "name_key": manifest["name_key"],
        "description_key": manifest["description_key"],
        "version": manifest["version"],
        "display_version": manifest["display_version"],
        "independent_tool": manifest["independent_tool"]
            .as_bool()
            .or_else(|| manifest["main_system_independent_tool"].as_bool()),
        "status": if running_tool { "running" } else { "stopped" },
        "launch": manifest["launch"],
        "runtime_type": manifest["runtime"]["type"],
        "runtime_mode": runtime_mode,
        "automatic_runtime_mode": automatic_runtime_mode,
        "runtime_available": runtime_available,
        "executable_path": executable_path
            .map(|p| p.to_string_lossy().to_string()),
        "executable_exists": executable_exists,
        "folder_path": dir.to_string_lossy().to_string(),
        "manifest_path": dir.join("manifest.json").to_string_lossy().to_string(),
        "code_path": code_path,
        "has_custom_ui": manifest["has_custom_ui"],
        "window_only": manifest["window_only"],
        "data_boundary": {
            "standalone": manifest["main_system_independent_tool"],
            "code_scope": manifest["permissions"]["code_scope"],
            "database_scope": manifest["permissions"]["database_scope"],
        },
        "enabled": manifest["enabled"],
    })
}

/// toolbox_list_tools — enumerate manifests with lifecycle truth.
/// Discovery is recursive (nested tool roots included); only manifests
/// declaring an independent-tool card are listed.
pub fn list_tools() -> Value {
    let mut tools = Vec::new();
    for (dir, manifest) in discover_manifests() {
        if !declares_tool_card(&manifest) {
            continue;
        }
        let tool_id = manifest["id"]
            .as_str()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .or_else(|| {
                dir.file_name().map(|n| n.to_string_lossy().to_string())
            })
            .unwrap_or_default();
        if tool_id.is_empty() {
            continue;
        }
        let running_tool = running()
            .lock()
            .map(|m| m.tools.contains_key(&tool_id))
            .unwrap_or(false);
        tools.push(list_tool_entry(&dir, &tool_id, &manifest, running_tool));
    }
    json!({ "ok": true, "tools": tools })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Nested ``local-model/model-dialogue`` must list as an independent
    /// tool card with its ``locales/zh-TW.json`` name — the retired Python
    /// ``ToolboxService`` contract the renderer hydrates against.
    #[test]
    fn model_dialogue_lists_with_localized_name() {
        let result = list_tools();
        let tools = result["tools"].as_array().expect("tools array");
        let entry = tools
            .iter()
            .find(|t| t["id"].as_str() == Some("model-dialogue"))
            .expect("model-dialogue card missing from toolbox_list_tools");
        let name = entry["name"].as_str().unwrap_or_default();
        assert!(
            name.contains("對話"),
            "expected localized zh-TW name, got {name:?}"
        );
    }
}

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

pub(super) fn load_manifest(tool_id: &str) -> Option<Value> {
    let path = tools_root().join(tool_id).join("manifest.json");
    serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()
}

pub(super) fn tool_dir(tool_id: &str) -> PathBuf {
    tools_root().join(tool_id)
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
fn list_tool_entry(tool_id: &str, manifest: &Value, running_tool: bool) -> Value {
    let dir = tool_dir(tool_id);
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
        "name": manifest["product"].as_str().unwrap_or(tool_id),
        "description": manifest["product"],
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
pub fn list_tools() -> Value {
    let mut tools = Vec::new();
    if let Ok(entries) = std::fs::read_dir(tools_root()) {
        for entry in entries.flatten() {
            let tool_id = entry.file_name().to_string_lossy().to_string();
            let Some(manifest) = load_manifest(&tool_id) else {
                continue;
            };
            if manifest["enabled"].as_bool() == Some(false) {
                continue;
            }
            let running_tool = running()
                .lock()
                .map(|m| m.tools.contains_key(&tool_id))
                .unwrap_or(false);
            tools.push(list_tool_entry(&tool_id, &manifest, running_tool));
        }
    }
    json!({ "ok": true, "tools": tools })
}

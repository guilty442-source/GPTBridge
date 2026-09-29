//! UI host launch (``_launch_source_ui`` parity).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};

use serde_json::Value;

use gptbridge_core::security;

use super::env::governed_env;
use super::{spawn_hidden, workspace_root};

fn tool_codename(tool_id: &str) -> String {
    match tool_id {
        "main-system" => "CENTRAL",
        "governance_rule" => "SOVEREIGN",
        "shared-layer" => "CONDUIT",
        "ai-assistant" => "STEWARD",
        "ai-collaboration" => "ENVOY",
        "model-dialogue" => "DIALOGUE",
        "file-sorter" => "SORTER",
        "xingcheng" => "NEBULA",
        "investment-mobile" => "MOBILE",
        "vaultly" => "VAULT",
        other => return other.to_uppercase(),
    }
    .to_string()
}

/// Registered native window surface binary (``native-ui-surfaces.json``),
/// falling back to the Tauri shell in ``--tool-window`` mode.
fn resolve_ui_shell(tool_id: &str) -> Option<(PathBuf, Option<PathBuf>)> {
    let root = workspace_root();
    let registry_path = root
        .join("main-system")
        .join("config")
        .join("native-ui-surfaces.json");
    if let Ok(raw) = std::fs::read_to_string(&registry_path) {
        if let Ok(registry) = serde_json::from_str::<Value>(&raw) {
            if let Some(binary) = registry["surfaces"][tool_id]["binary"].as_str() {
                /* Path-bearing entries (native C++ surfaces) resolve from
                 * the workspace root; bare names probe the cargo target
                 * dir under release then debug. */
                if binary.contains('/') || binary.contains('\\') {
                    let candidate = root.join(binary);
                    if candidate.is_file() {
                        return Some((candidate, None));
                    }
                } else {
                    for profile in ["release", "debug"] {
                        let candidate = root
                            .join("main-system")
                            .join("src-tauri")
                            .join("target")
                            .join(profile)
                            .join(binary);
                        if candidate.is_file() {
                            return Some((candidate, None));
                        }
                    }
                }
                /* Optional fallback surface (e.g. the egui host while a
                 * native C++ binary is not built yet). */
                if let Some(fallback) =
                    registry["surfaces"][tool_id]["fallback_binary"].as_str()
                {
                    for profile in ["release", "debug"] {
                        let candidate = root
                            .join("main-system")
                            .join("src-tauri")
                            .join("target")
                            .join(profile)
                            .join(fallback);
                        if candidate.is_file() {
                            return Some((candidate, None));
                        }
                    }
                }
            }
        }
    }
    let renderer_entry = root
        .join("main-system")
        .join("dist-ui")
        .join("independent-tools")
        .join(tool_id)
        .join("renderer")
        .join("index.html");
    for profile in ["release", "debug"] {
        let shell = root
            .join("main-system")
            .join("src-tauri")
            .join("target")
            .join(profile)
            .join("gptbridge-shell.exe");
        if shell.is_file() {
            return Some((shell, Some(renderer_entry)));
        }
    }
    None
}

pub(super) fn launch_source_ui(
    tool_id: &str,
    tool_root: &Path,
    manifest: &Value,
    runtime_env: &HashMap<String, String>,
    governed_id: &str,
) -> Result<Child, String> {
    let Some((shell, renderer_entry)) = resolve_ui_shell(tool_id) else {
        return Err("SOURCE_UI_UNAVAILABLE".into());
    };
    let port = runtime_env
        .get("GPTBRIDGE_IPC_PORT")
        .cloned()
        .unwrap_or_default();
    let session = runtime_env
        .get("GPTBRIDGE_IPC_SESSION_TOKEN")
        .cloned()
        .unwrap_or_default();
    let instance = security::workspace_instance_id();
    let ws_url = format!("ws://127.0.0.1:{port}/?token={session}&instance={instance}");

    let window = &manifest["window"];
    let mut env = governed_env(tool_id, tool_root, false);
    env.insert("GPTBRIDGE_SOURCE_UI_TOOL_ID".into(), tool_id.into());
    env.insert(
        "GPTBRIDGE_SOURCE_UI_WORKSPACE_ROOT".into(),
        workspace_root().to_string_lossy().into(),
    );
    env.insert(
        "GPTBRIDGE_SOURCE_UI_TOOL_ROOT".into(),
        tool_root.to_string_lossy().into(),
    );
    if let Some(renderer) = renderer_entry {
        env.insert(
            "GPTBRIDGE_SOURCE_UI_RENDERER_ENTRY".into(),
            renderer.to_string_lossy().into(),
        );
    }
    env.insert("GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL".into(), ws_url);
    env.insert("GPTBRIDGE_SOURCE_UI_CODENAME".into(), tool_codename(tool_id));
    env.insert(
        "GPTBRIDGE_SOURCE_UI_TITLE".into(),
        manifest["display_name"]
            .as_str()
            .unwrap_or(tool_id)
            .to_string(),
    );
    env.insert(
        "GPTBRIDGE_SOURCE_UI_VERSION".into(),
        manifest["version"].as_str().unwrap_or_default().to_string(),
    );
    env.insert(
        "GPTBRIDGE_SOURCE_UI_WIDTH".into(),
        window["width"].as_f64().unwrap_or(1440.0).to_string(),
    );
    env.insert(
        "GPTBRIDGE_SOURCE_UI_HEIGHT".into(),
        window["height"].as_f64().unwrap_or(920.0).to_string(),
    );
    env.insert(
        "GPTBRIDGE_SOURCE_UI_MIN_WIDTH".into(),
        window["minWidth"].as_f64().unwrap_or(1120.0).to_string(),
    );
    env.insert(
        "GPTBRIDGE_SOURCE_UI_MIN_HEIGHT".into(),
        window["minHeight"].as_f64().unwrap_or(760.0).to_string(),
    );
    env.insert(
        "GPTBRIDGE_GOVERNED_RUNTIME_TOOL_ID".into(),
        governed_id.into(),
    );

    let mut command = Command::new(&shell);
    command
        .arg("--tool-window")
        .arg(format!("--tool-id={tool_id}"))
        .current_dir(workspace_root().join("main-system"))
        .envs(env);
    spawn_hidden(command)
}

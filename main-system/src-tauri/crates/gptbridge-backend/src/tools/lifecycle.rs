//! toolbox_start_tool / stop / force_close lifecycle.

use std::process::Command;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use gptbridge_core::ipc::http;

use super::env::source_runtime_env;
use super::identity::{governed_runtime_tool_id, runtime_owner_tool_id};
use super::runtime::{
    await_runtime_ready, has_governed_source_runtime, resolve_native_entry,
};
use super::ui::launch_source_ui;
use super::{
    load_manifest, running, spawn_hidden, tool_dir, valid_tool_id, RunningTool,
};

const TOOL_OPEN_BUDGET: Duration = Duration::from_secs(5);
const GRACEFUL_STOP_WAIT: Duration = Duration::from_secs(3);

/// toolbox_start_tool — governed start path.
///
/// Contract: resolve the runtime owner → governed identity → native
/// runtime entry; spawn it with the governed env; wait for its health
/// endpoint; then spawn the tool-window UI host bound to that session.
/// Every failure is fail-closed with the Python contract's error codes.
pub fn start_tool(tool_id: &str) -> Value {
    if !valid_tool_id(tool_id) {
        return json!({ "ok": false, "tool_id": tool_id, "error_code": "TOOL_ID_INVALID" });
    }
    {
        let map = running().lock().unwrap_or_else(|e| e.into_inner());
        if map.tools.contains_key(tool_id) {
            return json!({ "ok": true, "tool_id": tool_id, "status": "already-running" });
        }
    }
    let Some(manifest) = load_manifest(tool_id) else {
        return json!({ "ok": false, "tool_id": tool_id, "error_code": "TOOL_UNKNOWN" });
    };

    let owner_id = match runtime_owner_tool_id(tool_id, &manifest) {
        Ok(id) => id,
        Err(e) => {
            return json!({ "ok": false, "tool_id": tool_id, "error_code": e });
        }
    };
    let owner_root = tool_dir(&owner_id);
    let Some(owner_manifest) = load_manifest(&owner_id) else {
        return json!({ "ok": false, "tool_id": tool_id, "error_code": "TOOL_UNKNOWN" });
    };
    let governed_id = match governed_runtime_tool_id(&owner_id, &owner_root) {
        Ok(id) => id,
        Err(e) => {
            return json!({ "ok": false, "tool_id": tool_id, "error_code": e });
        }
    };

    if !has_governed_source_runtime(&owner_manifest) {
        return json!({
            "ok": false,
            "tool_id": tool_id,
            "error_code": "SOURCE_RUNTIME_UNAVAILABLE",
        });
    }
    let native_entry = match resolve_native_entry(&owner_root, &owner_manifest) {
        Ok(p) => p,
        Err(e) => {
            return json!({ "ok": false, "tool_id": tool_id, "error_code": e });
        }
    };

    let runtime_env = match source_runtime_env(&owner_id, &owner_root, &governed_id) {
        Ok(env) => env,
        Err(e) => {
            return json!({ "ok": false, "tool_id": tool_id, "error_code": e });
        }
    };
    let port: u16 = runtime_env["GPTBRIDGE_IPC_PORT"].parse().unwrap_or(0);
    let shutdown_token = runtime_env["GPTBRIDGE_SHUTDOWN_TOKEN"].clone();

    let mut command = Command::new(&native_entry);
    command
        .current_dir(&owner_root)
        .envs(&runtime_env);
    let runtime_child = match spawn_hidden(command) {
        Ok(c) => c,
        Err(e) => {
            return json!({ "ok": false, "tool_id": tool_id, "error_code": e });
        }
    };

    // Health gate: the runtime must publish the governed contract inside
    // the open budget; otherwise the spawn is torn down fail-closed.
    let deadline = Instant::now() + TOOL_OPEN_BUDGET;
    if !await_runtime_ready(port, &governed_id, deadline) {
        let mut child = runtime_child;
        let _ = child.kill();
        let _ = child.wait();
        return json!({
            "ok": false,
            "tool_id": tool_id,
            "error_code": "SOURCE_RUNTIME_NOT_READY",
            "message": "Governed source runtime did not become ready",
        });
    }

    // UI host launch — absence of a window surface is non-fatal for the
    // runtime session but is reported truthfully.
    let ui_child = launch_source_ui(tool_id, &owner_root, &owner_manifest, &runtime_env, &governed_id).ok();
    let ui_pid = ui_child.as_ref().map(|c| c.id());

    {
        let mut map = running().lock().unwrap_or_else(|e| e.into_inner());
        map.tools.insert(
            tool_id.to_string(),
            RunningTool {
                runtime_child: Some(runtime_child),
                runtime_port: port,
                shutdown_token,
                ui_child,
            },
        );
    }
    json!({
        "ok": true,
        "tool_id": tool_id,
        "status": "running",
        "ui_mode": "governed-source-ui",
        "ui_pid": ui_pid,
    })
}

/// Graceful stop: authenticated ``/shutdown`` then a bounded wait.
pub fn stop_tool(tool_id: &str) -> Value {
    let mut map = running().lock().unwrap_or_else(|e| e.into_inner());
    let Some(mut tool) = map.tools.remove(tool_id) else {
        return json!({ "ok": false, "tool_id": tool_id, "error_code": "TOOL_NOT_RUNNING" });
    };
    if !tool.shutdown_token.is_empty() && tool.runtime_port > 0 {
        let _ = http::get(
            "127.0.0.1",
            tool.runtime_port,
            "/shutdown",
            &[("X-GPTBridge-Shutdown-Token", tool.shutdown_token.as_str())],
            Duration::from_millis(1500),
        );
    }
    let deadline = Instant::now() + GRACEFUL_STOP_WAIT;
    if let Some(child) = tool.runtime_child.as_mut() {
        while Instant::now() < deadline {
            match child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) => std::thread::sleep(Duration::from_millis(100)),
                Err(_) => break,
            }
        }
        if child.try_wait().ok().and_then(|s| s).is_none() {
            let _ = child.kill();
        }
        let _ = child.wait();
    }
    if let Some(ui) = tool.ui_child.as_mut() {
        let _ = ui.kill();
        let _ = ui.wait();
    }
    json!({ "ok": true, "tool_id": tool_id, "status": "stopped" })
}

/// toolbox_force_close_tool — terminate the runtime + UI immediately.
pub fn force_close_tool(tool_id: &str) -> Value {
    let mut map = running().lock().unwrap_or_else(|e| e.into_inner());
    match map.tools.remove(tool_id) {
        Some(mut tool) => {
            if let Some(ui) = tool.ui_child.as_mut() {
                let _ = ui.kill();
                let _ = ui.wait();
            }
            if let Some(child) = tool.runtime_child.as_mut() {
                let _ = child.kill();
                let _ = child.wait();
            }
            json!({ "ok": true, "tool_id": tool_id, "status": "stopped" })
        }
        None => json!({ "ok": false, "tool_id": tool_id, "error_code": "TOOL_NOT_RUNNING" }),
    }
}

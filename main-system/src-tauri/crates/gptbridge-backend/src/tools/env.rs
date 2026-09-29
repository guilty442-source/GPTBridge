//! Governed environment construction.

use std::collections::HashMap;
use std::net::TcpListener;
use std::path::Path;

use super::workspace_root;

/// Parent-environment keys allowed to flow into a governed tool process
/// (Windows essentials + PATH); everything else is dropped.
const ENV_PASSTHROUGH: [&str; 12] = [
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "OS",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS",
];

fn random_hex32() -> String {
    let mut buf = [0u8; 32];
    let _ = getrandom::getrandom(&mut buf);
    hex::encode(buf)
}

fn allocate_loopback_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .map_err(|e| format!("PORT_ALLOCATION_FAILED:{e}"))?;
    listener
        .local_addr()
        .map(|a| a.port())
        .map_err(|e| format!("PORT_ALLOCATION_FAILED:{e}"))
}

/// Base governed tool environment — mirrors ``_tool_environment``.
pub(super) fn governed_env(
    tool_id: &str,
    tool_root: &Path,
    start_hidden: bool,
) -> HashMap<String, String> {
    let mut env: HashMap<String, String> = std::env::vars()
        .filter(|(k, _)| ENV_PASSTHROUGH.contains(&k.to_uppercase().as_str()))
        .collect();

    let data_root = tool_root.join("runtime");
    let cache_root = data_root.join("cache").join("companions").join(tool_id);
    let temp_root = workspace_root()
        .join("main-system")
        .join("runtime")
        .join("temp")
        .join("tools")
        .join(tool_id);
    let _ = std::fs::create_dir_all(&cache_root);
    let _ = std::fs::create_dir_all(&temp_root);

    env.insert("GPTBRIDGE_PROJECT_ROOT".into(), tool_root.to_string_lossy().into());
    env.insert("GPTBRIDGE_STANDALONE_TOOL_ID".into(), tool_id.into());
    env.insert("GPTBRIDGE_TOOL_ID".into(), tool_id.into());
    env.insert("GPTBRIDGE_TOOL_DIR".into(), tool_root.to_string_lossy().into());
    env.insert("GPTBRIDGE_TOOL_DATA_ROOT".into(), data_root.to_string_lossy().into());
    env.insert(
        "GPTBRIDGE_TOOL_SETTINGS_ROOT".into(),
        data_root.join("settings").to_string_lossy().into(),
    );
    env.insert(
        "GPTBRIDGE_TOOL_DATABASE_ROOT".into(),
        data_root.join("state").to_string_lossy().into(),
    );
    env.insert("GPTBRIDGE_TOOL_CACHE_ROOT".into(), cache_root.to_string_lossy().into());
    env.insert("GPTBRIDGE_TOOL_TEMP_ROOT".into(), temp_root.to_string_lossy().into());
    env.insert("TEMP".into(), temp_root.to_string_lossy().into());
    env.insert("TMP".into(), temp_root.to_string_lossy().into());
    env.insert("TMPDIR".into(), temp_root.to_string_lossy().into());
    env.insert(
        "GPTBRIDGE_GOVERNANCE_PROJECT_ROOT".into(),
        workspace_root().to_string_lossy().into(),
    );
    env.remove("GPTBRIDGE_MANAGED_STORAGE_ROOT");
    env.remove("GPTBRIDGE_SYSTEM_RESCUE_STORAGE_AUTHORITY");
    if start_hidden {
        env.insert("GPTBRIDGE_START_HIDDEN".into(), "1".into());
    } else {
        env.remove("GPTBRIDGE_START_HIDDEN");
    }
    env
}

/// ``_source_runtime_environment`` — governed env + IPC credentials.
pub(super) fn source_runtime_env(
    tool_id: &str,
    tool_root: &Path,
    governed_id: &str,
) -> Result<HashMap<String, String>, String> {
    let mut env = governed_env(tool_id, tool_root, true);
    env.insert("GPTBRIDGE_GOVERNED_RUNTIME_TOOL_ID".into(), governed_id.into());
    env.insert("GPTBRIDGE_IPC_PORT".into(), allocate_loopback_port()?.to_string());
    env.insert("GPTBRIDGE_IPC_SESSION_TOKEN".into(), random_hex32());
    env.insert("GPTBRIDGE_SHUTDOWN_TOKEN".into(), random_hex32());
    Ok(env)
}

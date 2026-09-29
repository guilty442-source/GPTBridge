//! Tool-window configuration: env contract + packaged-backend layout.

use std::path::PathBuf;
use std::sync::OnceLock;

use gptbridge_core::native::paths::is_path_inside;

use super::ToolConfig;

fn random_hex(bytes: usize) -> String {
    let mut buf = vec![0u8; bytes];
    let _ = getrandom::getrandom(&mut buf);
    hex::encode(buf)
}

fn valid_tool_id(id: &str) -> bool {
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    let re = RE.get_or_init(|| regex::Regex::new(r"^[a-z0-9][a-z0-9_-]{1,63}$").unwrap());
    re.is_match(id)
}

fn env_num(name: &str, fallback: f64) -> f64 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.trim().parse::<f64>().ok())
        .filter(|v| v.is_finite() && *v > 0.0)
        .unwrap_or(fallback)
}

/// Electron ``validateConfiguration`` parity: ws URL must be a loopback
/// ws:// endpoint carrying a 64-hex session token and a 24-hex workspace
/// instance id.
fn parse_websocket_url(raw: &str) -> Option<(String, String, u16)> {
    let url = tauri::Url::parse(raw).ok()?;
    if url.scheme() != "ws" {
        return None;
    }
    if url.host_str()? != "127.0.0.1" {
        return None;
    }
    let port = url.port()?;
    if !(1024..=65535).contains(&port) {
        return None;
    }
    let mut token = None;
    let mut instance = None;
    for (k, v) in url.query_pairs() {
        match k.as_ref() {
            "token" => token = Some(v.to_string()),
            "instance" => instance = Some(v.to_string()),
            _ => {}
        }
    }
    let token = token?;
    let instance = instance?;
    let hex64 = token.len() == 64 && token.chars().all(|c| c.is_ascii_hexdigit());
    let hex24 = instance.len() == 24 && instance.chars().all(|c| c.is_ascii_hexdigit());
    if !hex64 || !hex24 {
        return None;
    }
    Some((token, instance, port))
}

fn resolved(name: &str) -> Result<PathBuf, String> {
    let raw = std::env::var(name).unwrap_or_default().trim().to_string();
    if raw.is_empty() {
        return Err(format!("CONFIG_INVALID:{name}"));
    }
    let path = PathBuf::from(raw);
    Ok(path.canonicalize().unwrap_or(path))
}

/// The ``GPTBRIDGE_SOURCE_UI_*`` environment contract — identical
/// validation to the Electron host's ``validateConfiguration``.
pub(super) fn load_env_config() -> Result<ToolConfig, String> {
    let tool_id = std::env::var("GPTBRIDGE_SOURCE_UI_TOOL_ID")
        .unwrap_or_default()
        .trim()
        .to_string();
    if !valid_tool_id(&tool_id) {
        return Err("CONFIG_INVALID:tool_id".to_string());
    }
    let workspace_root = resolved("GPTBRIDGE_SOURCE_UI_WORKSPACE_ROOT")?;
    let tool_root = resolved("GPTBRIDGE_SOURCE_UI_TOOL_ROOT")?;
    let cache_root = std::env::var("GPTBRIDGE_TOOL_CACHE_ROOT")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| tool_root.join("runtime").join("cache"));
    let cache_root = cache_root
        .canonicalize()
        .unwrap_or_else(|_| cache_root.clone());
    let renderer_entry = resolved("GPTBRIDGE_SOURCE_UI_RENDERER_ENTRY")?;
    let websocket_url = std::env::var("GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL")
        .unwrap_or_default()
        .trim()
        .to_string();

    if !is_path_inside(&workspace_root, &tool_root) {
        return Err("CONFIG_INVALID:tool_root".to_string());
    }
    if !is_path_inside(&workspace_root, &cache_root) {
        return Err("CONFIG_INVALID:cache_root".to_string());
    }
    if !is_path_inside(&workspace_root, &renderer_entry) || !renderer_entry.is_file() {
        return Err("CONFIG_INVALID:renderer_entry".to_string());
    }
    let Some((token, _instance, _port)) = parse_websocket_url(&websocket_url) else {
        return Err("CONFIG_INVALID:websocket_url".to_string());
    };

    Ok(ToolConfig {
        title: std::env::var("GPTBRIDGE_SOURCE_UI_TITLE")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .unwrap_or_else(|| tool_id.clone()),
        tool_version: std::env::var("GPTBRIDGE_SOURCE_UI_VERSION")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .unwrap_or_else(|| "1.0.0".to_string()),
        width: env_num("GPTBRIDGE_SOURCE_UI_WIDTH", 1440.0),
        height: env_num("GPTBRIDGE_SOURCE_UI_HEIGHT", 920.0),
        min_width: env_num("GPTBRIDGE_SOURCE_UI_MIN_WIDTH", 1120.0),
        min_height: env_num("GPTBRIDGE_SOURCE_UI_MIN_HEIGHT", 760.0),
        runtime_mode: "governed-source",
        start_hidden: std::env::var("GPTBRIDGE_START_HIDDEN").ok().as_deref() == Some("1"),
        tool_id,
        workspace_root,
        tool_root,
        cache_root,
        renderer_entry,
        websocket_url,
        backend_token: token,
    })
}

/// Packaged layout: the exe sits at ``<tool>/dist/<name>.exe`` with
/// ``resources/app/manifest.json`` carrying the standalone descriptor the
/// Electron template consumed.  The shell derives the same contract;
/// B166 retires the bundled-Python backend lane (``backend_entry`` /
/// ``python_runtime`` manifest keys are historical, never executed).
pub(super) fn load_packaged_config() -> Result<ToolConfig, String> {
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.to_path_buf()))
        .ok_or_else(|| "CONFIG_INVALID:exe_dir".to_string())?;
    let app_dir = exe_dir.join("resources").join("app");
    let manifest_path = app_dir.join("manifest.json");
    let manifest_text = std::fs::read_to_string(&manifest_path)
        .map_err(|_| "CONFIG_INVALID:manifest.json".to_string())?;
    let manifest: serde_json::Value = serde_json::from_str(&manifest_text)
        .map_err(|_| "CONFIG_INVALID:manifest_parse".to_string())?;

    let tool_id = manifest["id"]
        .as_str()
        .unwrap_or_default()
        .trim()
        .to_string();
    if !valid_tool_id(&tool_id) {
        return Err("CONFIG_INVALID:tool_id".to_string());
    }
    let standalone = &manifest["standalone"];
    let backend_port = std::env::var("GPTBRIDGE_IPC_PORT")
        .ok()
        .and_then(|v| v.parse::<u16>().ok())
        .or_else(|| standalone["backend_port"].as_u64().map(|p| p as u16))
        .filter(|p| (1024..=65535).contains(p))
        .unwrap_or(8765);
    let tool_root = std::env::var("GPTBRIDGE_TOOL_DIR")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .map(PathBuf::from)
        .or_else(|| exe_dir.parent().map(|p| p.to_path_buf()))
        .unwrap_or_else(|| exe_dir.clone());
    let cache_root = std::env::var("GPTBRIDGE_TOOL_CACHE_ROOT")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| tool_root.join("runtime").join("cache"));
    let renderer_entry = app_dir.join("renderer").join("index.html");
    if !renderer_entry.is_file() {
        return Err("CONFIG_INVALID:renderer_entry".to_string());
    }

    let session_token = random_hex(32);
    let instance = random_hex(12);
    let websocket_url =
        format!("ws://127.0.0.1:{backend_port}/?token={session_token}&instance={instance}");
    let window = &manifest["window"];

    Ok(ToolConfig {
        title: std::env::var("GPTBRIDGE_SOURCE_UI_TITLE")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .or_else(|| manifest["display_name"].as_str().map(|s| s.to_string()))
            .unwrap_or_else(|| tool_id.clone()),
        tool_version: manifest["version"].as_str().unwrap_or("1.0.0").to_string(),
        width: window["width"].as_f64().unwrap_or(1440.0),
        height: window["height"].as_f64().unwrap_or(920.0),
        min_width: window["minWidth"].as_f64().unwrap_or(1120.0),
        min_height: window["minHeight"].as_f64().unwrap_or(760.0),
        runtime_mode: "standalone",
        start_hidden: std::env::var("GPTBRIDGE_START_HIDDEN").ok().as_deref() == Some("1"),
        tool_id,
        workspace_root: app_dir.clone(),
        tool_root,
        cache_root,
        renderer_entry,
        websocket_url,
        backend_token: session_token.clone(),
    })
}

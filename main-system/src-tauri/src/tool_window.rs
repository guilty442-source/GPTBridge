//! tool_window.rs — ``--tool-window`` process mode.
//!
//! Rust/Tauri replacement for the retired Electron tool UI host
//! (``scripts/source-tool-ui-host/main.cjs``; codex A618/A621/A625: the
//! governed desktop host owns tool windows, Electron is MIGRATION_ONLY).
//!
//! Contract parity with the Electron host:
//!   - configuration arrives through the ``GPTBRIDGE_SOURCE_UI_*``
//!     environment contract and is validated identically (tool id shape,
//!     workspace containment, renderer existence, ws URL/token/instance
//!     shape); an invalid contract exits with code 1
//!   - a standalone window with tool-provided geometry/title hosts the
//!     tool's renderer entry via file:// navigation with the
//!     ``window.electron``/``window.gptBridge`` preload shim injected
//!   - embedded-browser sessions live in dedicated ``--embedded-worker``
//!     processes reparented under this window; the token-guarded loopback
//!     bridge is published at
//!     ``<tool-root>/runtime/ipc/tool-window-browser-bridge.json``
//!   - a packaged tool (``resources/app/manifest.json`` next to the exe)
//!     falls back to the packaged layout: the shell spawns the bundled
//!     Python backend itself and derives the session contract
//!   - minimize/hide detaches sessions, resize re-clamps, close tears
//!     down sessions and removes the bridge state file

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::sync::OnceLock;
use std::time::Duration;

use tauri::{Manager, WebviewUrl};

use crate::embedded;
use crate::http_util;
use crate::paths::is_path_inside;
use crate::tool_bridge;

const BACKEND_READY_TIMEOUT: Duration = Duration::from_secs(30);
const SHUTDOWN_DEADLINE_MS: u64 = 8_000;

fn tool_log(event: &str, detail: &str) {
    // println!/eprintln! panic on broken pipes — diagnostics must never be
    // fatal to a window host.
    use std::io::Write;
    let _ = writeln!(
        std::io::stdout().lock(),
        "[tool-ui-host] {event} {detail}"
    );
    let _ = std::io::stdout().flush();
}

/// Whether this process was launched as a tool-window host.
pub fn tool_window_requested() -> bool {
    std::env::args().any(|a| a == "--tool-window")
}

pub struct PackagedBackend {
    pub python: PathBuf,
    pub entry: PathBuf,
    pub port: u16,
}

pub struct ToolConfig {
    pub tool_id: String,
    pub workspace_root: PathBuf,
    pub tool_root: PathBuf,
    pub cache_root: PathBuf,
    pub renderer_entry: PathBuf,
    pub websocket_url: String,
    pub backend_token: String,
    pub shutdown_token: String,
    pub tool_version: String,
    pub title: String,
    pub width: f64,
    pub height: f64,
    pub min_width: f64,
    pub min_height: f64,
    pub runtime_mode: &'static str,
    pub start_hidden: bool,
    pub packaged_backend: Option<PackagedBackend>,
}

fn tool_config_cell() -> &'static OnceLock<ToolConfig> {
    static CONFIG: OnceLock<ToolConfig> = OnceLock::new();
    &CONFIG
}

pub fn tool_config() -> &'static ToolConfig {
    tool_config_cell()
        .get()
        .expect("tool config installed before dispatch")
}

fn shutdown_complete() -> &'static AtomicBool {
    static FLAG: OnceLock<AtomicBool> = OnceLock::new();
    FLAG.get_or_init(|| AtomicBool::new(false))
}

fn backend_child_pid() -> &'static Mutex<u32> {
    static PID: OnceLock<Mutex<u32>> = OnceLock::new();
    PID.get_or_init(|| Mutex::new(0))
}

fn random_hex(bytes: usize) -> String {
    let mut buf = vec![0u8; bytes];
    let _ = getrandom::getrandom(&mut buf);
    hex::encode(buf)
}

fn valid_tool_id(id: &str) -> bool {
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    let re = RE.get_or_init(|| {
        regex::Regex::new(r"^[a-z0-9][a-z0-9_-]{1,63}$").unwrap()
    });
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
    let hex24 =
        instance.len() == 24 && instance.chars().all(|c| c.is_ascii_hexdigit());
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
fn load_env_config() -> Result<ToolConfig, String> {
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
        start_hidden: std::env::var("GPTBRIDGE_START_HIDDEN").ok().as_deref()
            == Some("1"),
        tool_id,
        workspace_root,
        tool_root,
        cache_root,
        renderer_entry,
        websocket_url,
        backend_token: token,
        shutdown_token: String::new(),
        packaged_backend: None,
    })
}

/// Packaged layout: the exe sits at ``<tool>/dist/<name>.exe`` with
/// ``resources/app/manifest.json`` carrying the standalone descriptor the
/// Electron template consumed.  The shell derives the same contract and
/// spawns the bundled backend itself.
fn load_packaged_config() -> Result<ToolConfig, String> {
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

    let tool_id = manifest["id"].as_str().unwrap_or_default().trim().to_string();
    if !valid_tool_id(&tool_id) {
        return Err("CONFIG_INVALID:tool_id".to_string());
    }
    let standalone = &manifest["standalone"];
    let backend_entry = standalone["backend_entry"]
        .as_str()
        .unwrap_or("src-core/main.py");
    let backend_port = std::env::var("GPTBRIDGE_IPC_PORT")
        .ok()
        .and_then(|v| v.parse::<u16>().ok())
        .or_else(|| standalone["backend_port"].as_u64().map(|p| p as u16))
        .filter(|p| (1024..=65535).contains(p))
        .unwrap_or(8765);
    let python_rel = standalone["python_runtime"]
        .as_str()
        .unwrap_or("python/python.exe");
    let python = app_dir.join(python_rel.replace('/', "\\"));
    let entry = app_dir.join(backend_entry.replace('/', "\\"));

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
    let websocket_url = format!(
        "ws://127.0.0.1:{backend_port}/?token={session_token}&instance={instance}"
    );
    let window = &manifest["window"];

    Ok(ToolConfig {
        title: std::env::var("GPTBRIDGE_SOURCE_UI_TITLE")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .or_else(|| {
                manifest["display_name"]
                    .as_str()
                    .map(|s| s.to_string())
            })
            .unwrap_or_else(|| tool_id.clone()),
        tool_version: manifest["version"]
            .as_str()
            .unwrap_or("1.0.0")
            .to_string(),
        width: window["width"].as_f64().unwrap_or(1440.0),
        height: window["height"].as_f64().unwrap_or(920.0),
        min_width: window["minWidth"].as_f64().unwrap_or(1120.0),
        min_height: window["minHeight"].as_f64().unwrap_or(760.0),
        runtime_mode: "standalone",
        start_hidden: std::env::var("GPTBRIDGE_START_HIDDEN").ok().as_deref()
            == Some("1"),
        tool_id,
        workspace_root: app_dir.clone(),
        tool_root,
        cache_root,
        renderer_entry,
        websocket_url,
        backend_token: session_token.clone(),
        shutdown_token: random_hex(32),
        packaged_backend: if entry.is_file() && python.is_file() {
            Some(PackagedBackend {
                python,
                entry,
                port: backend_port,
            })
        } else {
            None
        },
    })
}

fn backend_healthy(port: u16) -> bool {
    http_util::get(
        "127.0.0.1",
        port,
        "/health",
        &[],
        Duration::from_millis(500),
    )
    .map(|r| r.status == 200)
    .unwrap_or(false)
}

/// Spawn the packaged Python backend (template ``ensureBackendStarted``
/// parity — the lean path: reuse a healthy listener, otherwise spawn the
/// bundled interpreter with the governed IPC env and wait for /health).
fn spawn_packaged_backend(config: &ToolConfig) -> Result<u32, String> {
    let Some(backend) = &config.packaged_backend else {
        return Ok(0);
    };
    if backend_healthy(backend.port) {
        tool_log("backend.reuse", &config.tool_id);
        return Ok(0);
    }

    let mut command = std::process::Command::new(&backend.python);
    command
        .args(["-B", "-s", "-E", "-X", "utf8"])
        .arg(&backend.entry)
        .current_dir(&config.tool_root)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONNOUSERSITE", "1")
        .env(
            "GPTBRIDGE_GOVERNANCE_PROJECT_ROOT",
            std::env::var("GPTBRIDGE_PROJECT_ROOT")
                .unwrap_or_else(|_| config.workspace_root.to_string_lossy().into_owned()),
        )
        .env("GPTBRIDGE_TOOL_DIR", &config.tool_root)
        .env(
            "GPTBRIDGE_TOOL_DATA_ROOT",
            std::env::var("GPTBRIDGE_TOOL_DATA_ROOT").unwrap_or_else(|_| {
                config.tool_root.join("runtime").to_string_lossy().into_owned()
            }),
        )
        .env(
            "GPTBRIDGE_IPC_STATE_ROOT",
            config.tool_root.join("runtime").join("ipc"),
        )
        .env("GPTBRIDGE_IPC_SESSION_TOKEN", &config.backend_token)
        .env("GPTBRIDGE_SHUTDOWN_TOKEN", &config.shutdown_token)
        .env("GPTBRIDGE_IPC_PORT", backend.port.to_string())
        .env("GPTBRIDGE_TOOL_ID", &config.tool_id)
        .env_remove("GPTBRIDGE_PYTHON")
        .env_remove("GPTBRIDGE_ALLOW_SYSTEM_PYTHON")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let child = command
        .spawn()
        .map_err(|e| format!("BACKEND_PROCESS_SPAWN_FAILED:{e}"))?;
    let pid = child.id();
    drop(child);

    let deadline = std::time::Instant::now() + BACKEND_READY_TIMEOUT;
    while std::time::Instant::now() < deadline {
        if backend_healthy(backend.port) {
            tool_log("backend.ready", &config.tool_id);
            return Ok(pid);
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Err("BACKEND_STARTUP_TIMEOUT".to_string())
}

/// Per-tool single instance (Electron ``requestSingleInstanceLock``
/// parity): a second launch for the same tool exits instead of stacking a
/// duplicate window.
#[cfg(windows)]
fn acquire_tool_mutex(tool_id: &str) -> bool {
    use windows_sys::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS};
    use windows_sys::Win32::System::Threading::CreateMutexW;
    let name: Vec<u16> = format!("Local\\gptbridge-tool-window-{tool_id}")
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect();
    unsafe {
        let handle = CreateMutexW(std::ptr::null(), 0, name.as_ptr());
        if handle.is_null() {
            return true;
        }
        if GetLastError() == ERROR_ALREADY_EXISTS {
            return false;
        }
        // HANDLE is a raw pointer — holding the local binding keeps the
        // kernel mutex open for the process lifetime.
        let _ = handle;
        true
    }
}

#[cfg(not(windows))]
fn acquire_tool_mutex(_tool_id: &str) -> bool {
    true
}

/// Preload parity with ``preload.cjs``: ``window.electron.invoke`` +
/// ``onEvent`` (the single whitelisted event channel is
/// ``embedded-browser:event``) and the ``window.gptBridge`` helper surface.
const TOOL_PRELOAD_SHIM: &str = r#"
(function () {
  'use strict';
  var invoke = function (channel) {
    var args = Array.prototype.slice.call(arguments, 1);
    return window.__TAURI__.core.invoke('gptbridge_invoke', {
      channel: channel,
      args: args
    }).then(function (r) { return r; });
  };
  var onEvent = function (channel, callback) {
    if (channel !== 'embedded-browser:event' || typeof callback !== 'function') {
      return function () {};
    }
    var pending = window.__TAURI__.event.listen(channel, function (event) {
      callback(event.payload);
    });
    return function () { pending.then(function (unlisten) { unlisten(); }); };
  };
  window.electron = { invoke: invoke, onEvent: onEvent };
  window.gptBridge = {
    standaloneTool: false,
    selectFolder: function () { return invoke('dialog:select-folder'); },
    validateFolder: function (candidate) {
      return invoke('dialog:validate-folder', candidate || '');
    },
    createFile: function (defaultPath) {
      return invoke('dialog:create-file', defaultPath || '');
    },
    openFile: function (defaultPath) {
      return invoke('dialog:open-file', defaultPath || '');
    },
    openPath: function (payload) { return invoke('app:open-path', payload); },
    onEmbeddedBrowserEvent: function (callback) {
      return onEvent('embedded-browser:event', callback);
    }
  };
})();
"#;

fn renderer_file_url(entry: &Path) -> tauri::Url {
    tauri::Url::from_file_path(entry)
        .unwrap_or_else(|_| "about:blank".parse().unwrap())
}

/// Renderer hot-reload parity: poll the entry mtime; a rebuilt renderer
/// reloads the window in place (Electron ``fs.watchFile`` + debounced
/// ``reloadIgnoringCache``; the host-file hot-restart has no compiled
/// equivalent — a rebuilt binary is delivered by the launcher).
fn start_renderer_watch(app: tauri::AppHandle, entry: PathBuf) {
    std::thread::spawn(move || {
        let mut last = mtime(&entry);
        loop {
            std::thread::sleep(Duration::from_millis(500));
            if shutdown_complete().load(Ordering::SeqCst) {
                return;
            }
            let current = mtime(&entry);
            if current != last && last != 0 && current != 0 {
                last = current;
                // Small debounce window — a build may write the entry more
                // than once.
                std::thread::sleep(Duration::from_millis(300));
                if let Some(window) = app.get_window("tool") {
                    if let Some(webview) = crate::find_webview(&window, "tool") {
                        if let Ok(url) = webview.url() {
                            let _ = webview.navigate(url);
                        } else {
                            let _ = webview.eval("window.location.reload()");
                        }
                        tool_log("renderer.hot-reload", "");
                    }
                }
            } else {
                last = current;
            }
        }
    });
}

fn mtime(path: &std::path::Path) -> u64 {
    std::fs::metadata(path)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn shutdown(app: &tauri::AppHandle) {
    if shutdown_complete().swap(true, Ordering::SeqCst) {
        return;
    }
    embedded::close_all_sessions(app);
    tool_bridge::stop_tool_bridge();
    let pid = *backend_child_pid().lock().unwrap();
    if pid != 0 {
        // Packaged backend spawned by this host — bounded tree kill so the
        // window never leaves a detached backend behind.
        let (tx, rx) = std::sync::mpsc::channel::<()>();
        std::thread::spawn(move || {
            let _ = std::process::Command::new("taskkill")
                .args(["/PID", &pid.to_string(), "/T", "/F"])
                .output();
            let _ = tx.send(());
        });
        let _ = rx.recv_timeout(Duration::from_millis(SHUTDOWN_DEADLINE_MS));
    }
    tool_log("tool.window-closed", "");
}

/// Entry point for ``--tool-window`` mode.  Owns the process forever;
/// never falls through to the main-shell path.
pub fn run() -> i32 {
    let config = match load_env_config().or_else(|_| load_packaged_config()) {
        Ok(config) => config,
        Err(message) => {
            tool_log("config.invalid", &message);
            return 1;
        }
    };
    if !acquire_tool_mutex(&config.tool_id) {
        tool_log("instance.duplicate", &config.tool_id);
        return 0;
    }

    // Per-tool WebView2 profile isolation (Electron setPath('userData')/
    // sessionData/disk-cache parity) — MUST be set before any webview
    // initialises, otherwise the tool window collides with the main
    // shell's user-data folder.
    let user_data = config.cache_root.join("source-ui-user-data");
    let session_data = config.cache_root.join("session-data");
    let disk_cache = config.cache_root.join("disk-cache");
    for dir in [&user_data, &session_data, &disk_cache] {
        let _ = std::fs::create_dir_all(dir);
    }
    std::env::set_var("WEBVIEW2_USER_DATA_FOLDER", &user_data);
    std::env::set_var("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--disable-gpu-vsync");

    embedded::configure_host(embedded::HostConfig {
        window_label: "tool",
        state_dir: config.tool_root.join("runtime").join("ipc"),
        log_dir: config.tool_root.join("runtime").join("logs"),
        worker_data_root: config.cache_root.join("embedded-webview"),
        worker_events: true,
    });

    match spawn_packaged_backend(&config) {
        Ok(pid) => {
            *backend_child_pid().lock().unwrap() = pid;
            if pid != 0 {
                tool_log("backend.spawned", &config.tool_id);
            }
        }
        Err(message) => {
            tool_log("backend.start-failed", &message);
            return 1;
        }
    }

    let renderer_entry = config.renderer_entry.clone();
    let width = config.width;
    let height = config.height;
    let min_width = config.min_width;
    let min_height = config.min_height;
    let title = config.title.clone();
    let start_hidden = config.start_hidden;
    let _ = tool_config_cell().set(config);

    let result = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![crate::gptbridge_invoke])
        .setup(move |app| {
            let window = tauri::window::WindowBuilder::new(app, "tool")
                .title(&title)
                .inner_size(width, height)
                .min_inner_size(min_width, min_height)
                .visible(false)
                .background_color(
                    "#0b0f17"
                        .parse()
                        .unwrap_or(tauri::window::Color(11, 15, 23, 255)),
                )
                .build()?;

            let initial_size = window
                .inner_size()
                .ok()
                .filter(|s| s.width > 0 && s.height > 0)
                .unwrap_or(tauri::PhysicalSize::new(
                    width.round() as u32,
                    height.round() as u32,
                ));
            embedded::record_content_size(
                initial_size,
                window.scale_factor().unwrap_or(1.0),
            );

            window.add_child(
                tauri::webview::WebviewBuilder::new(
                    "tool",
                    WebviewUrl::External(renderer_file_url(&renderer_entry)),
                )
                .auto_resize()
                .initialization_script(TOOL_PRELOAD_SHIM),
                tauri::LogicalPosition::new(0, 0),
                initial_size,
            )?;

            tool_bridge::start_tool_bridge(&app.handle());
            start_renderer_watch(app.handle().clone(), renderer_entry);
            if !start_hidden {
                let _ = window.show();
                let _ = window.set_focus();
            }
            tool_log("window.ready", &tool_config().tool_id);
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() != "tool" {
                return;
            }
            match event {
                tauri::WindowEvent::Resized(size) => {
                    if size.width == 0 || size.height == 0 {
                        // SIZE_MINIMIZED — detach every visible session.
                        embedded::hide_all_sessions(&window.app_handle());
                        return;
                    }
                    embedded::record_content_size(
                        *size,
                        window.scale_factor().unwrap_or(1.0),
                    );
                    embedded::on_window_resized(&window.app_handle());
                }
                tauri::WindowEvent::CloseRequested { .. } => {
                    embedded::close_all_sessions(&window.app_handle());
                }
                _ => {}
            }
        })
        .build(tauri::generate_context!());

    match result {
        Ok(app) => {
            app.run(|app, event| {
                if let tauri::RunEvent::ExitRequested { .. } = event {
                    shutdown(app);
                }
            });
            0
        }
        Err(e) => {
            tool_log("tauri.build-failed", &format!("{e}"));
            3
        }
    }
}

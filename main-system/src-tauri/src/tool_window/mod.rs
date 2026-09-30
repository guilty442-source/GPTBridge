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
//!     falls back to the packaged layout: the shell derives the session
//!     contract from the manifest (B166: no bundled Python backend lane
//!     exists — retired with the Python fleet)
//!   - minimize/hide detaches sessions, resize re-clamps, close tears
//!     down sessions and removes the bridge state file

mod backend;
mod config;
mod window;

use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::OnceLock;

pub use window::run;

pub(super) fn tool_log(event: &str, detail: &str) {
    eprintln!("[tool-window] {event}: {detail}");
}

pub fn tool_window_requested() -> bool {
    std::env::args().any(|a| a == "--tool-window")
}
pub struct ToolConfig {
    pub tool_id: String,
    pub workspace_root: PathBuf,
    pub tool_root: PathBuf,
    pub cache_root: PathBuf,
    pub renderer_entry: PathBuf,
    pub websocket_url: String,
    pub backend_token: String,
    pub tool_version: String,
    pub title: String,
    pub width: f64,
    pub height: f64,
    pub min_width: f64,
    pub min_height: f64,
    pub runtime_mode: &'static str,
    pub start_hidden: bool,
}

pub(super) fn tool_config_cell() -> &'static OnceLock<ToolConfig> {
    static CONFIG: OnceLock<ToolConfig> = OnceLock::new();
    &CONFIG
}

pub fn tool_config() -> &'static ToolConfig {
    tool_config_cell()
        .get()
        .expect("tool config installed before dispatch")
}

pub(super) fn shutdown_complete() -> &'static AtomicBool {
    static FLAG: OnceLock<AtomicBool> = OnceLock::new();
    FLAG.get_or_init(|| AtomicBool::new(false))
}

//! sizes — port of src-ui/main/platform-tool-sizes.ts + platformToolSizeTypes.
//!
//! Folder inventory of the governed workspace for the UI's size dashboard:
//! per-tool size breakdown, main-system project vs dependency split,
//! shared-layer, and the workspace rollup.  Results cache for 30 s.
//!
//! B94 decomposition: measurement primitives live in ``measure``,
//! manifest discovery in ``inventory``, the cached public entry points
//! in ``api``; this module keeps the shared constants and cache state.

use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::OnceLock;

mod api;
mod inventory;
mod measure;

#[cfg(test)]
mod tests;

pub use api::{
    main_system_size, platform_tool_sizes, shared_layer_size, workspace_size,
};

const CACHE_TTL_MS: u64 = 30_000;

const MAIN_SYSTEM_DEPENDENCY_DIRECTORIES: [&str; 1] = ["node_modules"];

const TOOL_RUNTIME_ROOTS: [&str; 5] = [
    "build",
    "dist",
    "env",
    "node_modules",
    "release",
];

const TOOL_CACHE_SEGMENTS: [&str; 11] = [
    ".cache",
    "browser-profile",
    "browser-profiles",
    "cache",
    "caches",
    "code cache",
    "edge-profile",
    "electron-user-data",
    "gpu cache",
    "temp",
    "tmp",
];

const TOOL_USER_DATA_RUNTIME_ROOTS: [&str; 4] = ["data", "recovery", "settings", "state"];

pub(super) struct SizeCache {
    pub inventory: Option<(u64, serde_json::Value)>,
    pub main_system: Option<(u64, serde_json::Value)>,
    pub shared_layer: Option<(u64, serde_json::Value)>,
    pub workspace: Option<(u64, serde_json::Value)>,
    pub inventory_root: Option<PathBuf>,
}

pub(super) fn size_cache() -> &'static Mutex<SizeCache> {
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

pub(super) fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

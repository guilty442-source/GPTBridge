//! GPTBridge governed desktop shell — Tauri host replacing the retired
//! Electron runtime (codex A618/A625: Rust/Tauri desktop host; A621:
//! Electron MIGRATION_ONLY).
//!
//! UI-stack layering (refactor target):
//!   gptbridge-core  — Rust core: app/state/security/ipc/lifecycle/native
//!   desktop_shell   — Tauri Desktop Shell: windows, single-instance, watchers
//!   webview_host    — Tauri WebView Host: embedded-browser session workers
//!   js_bridge       — Tauri JS Bridge: preload shim, channel whitelist, IPC
//!
//! Contract parity with the retired src-ui/main/index.ts:
//!   - single-instance; a second launch re-focuses and re-checks the managed
//!     backend instead of starting a duplicate stack
//!   - window first, backend in the background (boot_core supervises main.py)
//!   - complete-close: closing the last window stops embedded sessions, the
//!     loopback bridge, watchers, and the managed backend

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod desktop_shell;
mod js_bridge;
mod webview_host;

fn main() {
    // Helper-process mode: a dedicated worker hosting exactly one embedded
    // browser session (see webview_host/worker.rs — first-controller-only
    // WebView2 reliability contract).
    if let Some(args) = webview_host::worker::worker_args() {
        std::process::exit(webview_host::worker::run(args));
    }

    desktop_shell::run();
}

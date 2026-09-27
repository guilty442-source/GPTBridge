//! GPTBridge governed desktop shell — Tauri host replacing the retired
//! Electron runtime (codex A618/A625: Rust/Tauri desktop host; A621:
//! Electron MIGRATION_ONLY).
//!
//! Contract parity with src-ui/main/index.ts:
//!   - single-instance; a second launch re-focuses and re-checks the managed
//!     backend instead of starting a duplicate stack
//!   - window first, backend in the background (boot_core supervises main.py)
//!   - complete-close: closing the last window stops embedded sessions, the
//!     loopback bridge, watchers, and the managed backend

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;
mod bridge;
mod commands;
mod embedded;
mod embedded_worker;
mod http_util;
mod metrics;
mod paths;
mod session;
mod sizes;
mod slo;
mod tool_bridge;
mod tool_dispatch;
mod tool_window;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use std::time::Duration;

use tauri::{Manager, WebviewUrl};

/// Find a child webview inside a window by label.
pub fn find_webview(window: &tauri::Window, label: &str) -> Option<tauri::Webview> {
    window
        .webviews()
        .into_iter()
        .find(|w| w.label() == label)
}

const SHUTDOWN_DEADLINE_MS: u64 = 15_000;

/// Preload whitelist parity — identical to preload.ts allowedInvokeChannels.
const ALLOWED_CHANNELS: [&str; 25] = [
    "app:get-status",
    "app:get-perf-slo",
    "app:get-backend-session",
    "app:restart",
    "app:restart-backend",
    "app:ensure-backend-started",
    "app:get-platform-tool-sizes",
    "app:reload-window",
    "app:reload-window-hard",
    "app:get-ui-zoom",
    "app:set-ui-zoom",
    "app:open-path",
    "dialog:select-folder",
    "dialog:create-file",
    "dialog:open-file",
    "embedded-browser:create",
    "embedded-browser:navigate",
    "embedded-browser:execute",
    "embedded-browser:show",
    "embedded-browser:hide",
    "embedded-browser:close",
    "embedded-browser:resize",
    "embedded-browser:list",
    "embedded-browser:url",
    "embedded-browser:close-module",
];

fn report(event: &str, payload: serde_json::Value) {
    // println! panics on a broken pipe — the shell can outlive the
    // launcher-provided stdout handle, so a failed write must never be
    // fatal.  Diagnostics stay best-effort.
    use std::io::Write;
    let _ = writeln!(
        std::io::stdout().lock(),
        "[Main System] {event} {payload}"
    );
    let _ = std::io::stdout().flush();
}

/// The renderer-visible contract injected into every webview before scripts
/// run — replaces the Electron preload bridge.  Channel dispatch goes
/// through the single governed ``gptbridge_invoke`` command which re-checks
/// the whitelist server-side.
const PRELOAD_SHIM: &str = r#"
(function () {
  'use strict';
  var invoke = function (channel) {
    var args = Array.prototype.slice.call(arguments, 1);
    return window.__TAURI__.core.invoke('gptbridge_invoke', {
      channel: channel,
      args: args
    }).then(function (r) { return r; });
  };
  window.electron = { invoke: invoke };
  window.gptBridge = {
    selectFolder: function () { return invoke('dialog:select-folder'); },
    createFile: function (defaultPath) {
      return invoke('dialog:create-file', defaultPath || '');
    },
    openFile: function (defaultPath) {
      return invoke('dialog:open-file', defaultPath || '');
    },
    openPath: function (payload) { return invoke('app:open-path', payload); },
    restartApp: function () { return invoke('app:restart'); },
    restartBackend: function () { return invoke('app:restart-backend'); },
    ensureBackendStarted: function () { return invoke('app:ensure-backend-started'); }
  };
  // Reload shortcuts (Electron before-input-event parity).
  window.addEventListener('keydown', function (event) {
    var key = (event.key || '').toLowerCase();
    var reload = key === 'f5' || ((event.ctrlKey || event.metaKey) && key === 'r');
    if (!reload) return;
    event.preventDefault();
    invoke(event.shiftKey ? 'app:reload-window-hard' : 'app:reload-window');
  });
})();
"#;

#[tauri::command]
async fn gptbridge_invoke(
    app: tauri::AppHandle,
    channel: String,
    args: serde_json::Value,
) -> Result<serde_json::Value, String> {
    // Tool-window mode has its own whitelist and dispatch surface
    // (source-tool-ui-host contract, tool_dispatch.rs).
    if tool_window::tool_window_requested() {
        return tauri::async_runtime::spawn_blocking(move || {
            tauri::async_runtime::block_on(tool_dispatch::dispatch(app, &channel, args))
        })
        .await
        .map_err(|e| format!("dispatch join failed: {e}"));
    }
    if !ALLOWED_CHANNELS.contains(&channel.as_str()) {
        return Ok(serde_json::json!({
            "ok": false,
            "message": format!("Blocked IPC channel: {channel}")
        }));
    }
    // Electron handlers receive the first positional argument as payload;
    // the shim forwards the full array.
    let payload = if args.is_array() {
        args.get(0).cloned().unwrap_or(serde_json::Value::Null)
    } else {
        args
    };
    // dispatch does sync-only work (worker spawn up to 25s, loopback HTTP
    // up to 15s) — run it on the blocking pool so a slow embedded-browser
    // op never occupies an async-runtime thread.
    tauri::async_runtime::spawn_blocking(move || {
        tauri::async_runtime::block_on(commands::dispatch(app, &channel, payload))
    })
    .await
    .map_err(|e| format!("dispatch join failed: {e}"))
}

fn manage_backend() -> bool {
    std::env::var("GPTBRIDGE_MANAGE_BACKEND").ok().as_deref() == Some("1")
}

fn shutdown_complete() -> &'static AtomicBool {
    static FLAG: OnceLock<AtomicBool> = OnceLock::new();
    FLAG.get_or_init(|| AtomicBool::new(false))
}

/// Complete-close contract (index.ts shutdownApplication): close embedded
/// sessions, stop the bridge, stop watchers, then stop the managed backend —
/// bounded so a stalled graceful stop never leaves a detached orphan.
fn shutdown_application(app: &tauri::AppHandle) {
    if shutdown_complete().swap(true, Ordering::SeqCst) {
        return;
    }
    embedded::close_all_sessions(app);
    bridge::stop_embedded_browser_bridge();
    if manage_backend() {
        // Backend shutdown is awaited but bounded — a stalled graceful stop
        // must never leave the UI running as a detached orphan.
        let (tx, rx) = std::sync::mpsc::channel::<()>();
        std::thread::spawn(move || {
            backend::stop_backend();
            let _ = tx.send(());
        });
        let _ = rx.recv_timeout(Duration::from_millis(SHUTDOWN_DEADLINE_MS));
    }
    report("main.ui-shutdown", serde_json::json!({}));
}

/// Renderer hot-reload parity: poll dist-ui entry mtimes every 2 s; a rebuilt
/// renderer reloads the window in place (the main-bundle relaunch path has no
/// native equivalent — a rebuilt Rust binary is delivered by the launcher).
fn start_renderer_watch(app: tauri::AppHandle) {
    let renderer_html = paths::path_library().renderer_entry_html.clone();
    std::thread::spawn(move || {
        let mut last = mtime(&renderer_html);
        loop {
            std::thread::sleep(Duration::from_millis(2_000));
            if shutdown_complete().load(Ordering::SeqCst) {
                return;
            }
            let current = mtime(&renderer_html);
            if current != last && last != 0 && current != 0 {
                last = current;
                if let Some(window) = app.get_window("main") {
                    if let Some(webview) = find_webview(&window, "main") {
                        let _ = webview.eval("window.location.reload()");
                        report("renderer.hot-reload", serde_json::json!({}));
                    }
                }
            } else {
                last = current;
            }
        }
    });
}

/// Watchdog: if the main webview never finished its initial navigation
/// (WebView2 can race UDF contention after a force-killed peer instance and
/// strand the view on about:blank), reload it once — mirrors Electron's
/// did-fail-load recovery path.
fn start_load_watchdog(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(8_000));
        if shutdown_complete().load(Ordering::SeqCst) {
            return;
        }
        let Some(window) = app.get_window("main") else { return };
        let Some(webview) = find_webview(&window, "main") else { return };
        match webview.url() {
            Ok(url) if url.as_str() == "about:blank" || url.as_str().is_empty() => {
                report("webview.load-watchdog.reload", serde_json::json!({}));
                if webview.eval("window.location.reload()").is_err() {
                    let _ = webview
                        .navigate("http://tauri.localhost/index.html".parse().unwrap());
                }
            }
            _ => {}
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

fn main() {
    // Helper-process mode: a dedicated worker hosting exactly one embedded
    // browser session (see embedded_worker.rs — first-controller-only
    // WebView2 reliability contract).
    if let Some(args) = embedded_worker::worker_args() {
        std::process::exit(embedded_worker::run(args));
    }

    // Tool-window host mode: the governed custom-tool window owner that
    // replaces the retired Electron source-tool-ui-host (A621).
    if tool_window::tool_window_requested() {
        std::process::exit(tool_window::run());
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // Second-instance contract: re-check the managed backend and
            // focus the existing window instead of starting a new stack.
            if manage_backend() {
                backend::ensure_backend_started();
            }
            if let Some(window) = app.get_window("main") {
                if window.is_minimized().unwrap_or(false) {
                    let _ = window.unminimize();
                }
                let _ = window.show();
                let _ = window.set_focus();
            } else {
                let _ = create_main_window(app);
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![gptbridge_invoke])
        .setup(|app| {
            report(
                "bootstrap.start",
                serde_json::json!({
                    "isPackaged": paths::is_packaged(),
                    "shouldManageBackend": manage_backend(),
                    "workspaceRoot": paths::path_library().workspace_root,
                }),
            );

            create_main_window(&app.handle())?;
            start_renderer_watch(app.handle().clone());
            start_load_watchdog(app.handle().clone());
            // The loopback bridge publishes the embedded-browser session
            // store for tool UIs/backends (A44/E30 + A49/E35).
            bridge::start_embedded_browser_bridge(&app.handle());
            report("window.ready", serde_json::json!({}));

            // Backend startup runs in the background and does not block the
            // UI (A60: the launcher only spawns boot_core; boot_core starts
            // and supervises main.py per A61 ordering).
            if manage_backend() {
                std::thread::spawn(backend::start_backend);
            }
            report("bootstrap.ready", serde_json::json!({}));
            Ok(())
        })
        .on_window_event(|window, event| {
            // Session windows (embedded-*) share this handler — scope the
            // main-window contracts to the main window only.
            if window.label() != "main" {
                return;
            }
            match event {
                tauri::WindowEvent::Resized(size) => {
                    if size.width == 0 || size.height == 0 {
                        // Windows minimize arrives as a 0x0 Resized
                        // (SIZE_MINIMIZED) — the Electron 'minimize'/'hide'
                        // contract detaches every visible session so no
                        // embedded view can linger over unrelated UI.
                        embedded::hide_all_sessions(&window.app_handle());
                        return;
                    }
                    embedded::record_content_size(*size, window.scale_factor().unwrap_or(1.0));
                    embedded::on_window_resized(&window.app_handle());
                    commands::apply_adaptive_zoom(window);
                }
                tauri::WindowEvent::CloseRequested { .. } => {
                    embedded::close_all_sessions(&window.app_handle());
                }
                _ => {}
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build GPTBridge shell")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                shutdown_application(app);
            }
        });
}

/// Create the main window: a plain Window hosting an auto-resizing "main"
/// child webview — mirrors the Electron BrowserWindow + preload contract and
/// leaves room for embedded-browser sibling webviews.
fn create_main_window(app: &tauri::AppHandle) -> Result<tauri::Window, tauri::Error> {
    if let Some(existing) = app.get_window("main") {
        let _ = existing.set_focus();
        return Ok(existing);
    }

    let dev_url = std::env::var("GPTBRIDGE_RENDERER_DEV_URL")
        .ok()
        .filter(|v| !v.trim().is_empty());
    let webview_url = match dev_url {
        Some(url) => WebviewUrl::External(
            url.parse().unwrap_or_else(|_| "http://localhost:5173".parse().unwrap()),
        ),
        None => WebviewUrl::App("index.html".into()),
    };

    // Electron titleBarStyle:'hidden' + titleBarOverlay has no Windows
    // equivalent in Tauri — the native caption stays (cosmetic deviation;
    // the renderer's overlay-height CSS is inert).
    let window = tauri::window::WindowBuilder::new(app, "main")
        .title("GPTBridge")
        .inner_size(1400.0, 900.0)
        .min_inner_size(1100.0, 720.0)
        .visible(false)
        .build()?;

    // A freshly-built hidden window may report a 0x0 inner size — seed the
    // geometry cache from the requested logical size instead so bounds
    // clamps have a truthful viewport until the first real Resized event.
    let initial_size = window
        .inner_size()
        .ok()
        .filter(|s| s.width > 0 && s.height > 0)
        .unwrap_or(tauri::PhysicalSize::new(1400, 900));
    embedded::record_content_size(initial_size, window.scale_factor().unwrap_or(1.0));
    let _webview = window.add_child(
        tauri::webview::WebviewBuilder::new("main", webview_url)
            .auto_resize()
            .initialization_script(PRELUDE_SCRIPT),
        tauri::LogicalPosition::new(0, 0),
        initial_size,
    )?;
    commands::apply_adaptive_zoom(&window);
    let _ = window.show();
    let _ = window.set_focus();
    Ok(window)
}

const PRELUDE_SCRIPT: &str = PRELOAD_SHIM;

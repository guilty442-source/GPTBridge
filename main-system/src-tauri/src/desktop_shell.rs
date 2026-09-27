//! desktop_shell.rs — Tauri Desktop Shell layer.
//!
//! Owns the window topology (single "main" Window + auto-resizing "main"
//! child webview), the single-instance contract, renderer hot-reload and
//! load watchdogs, and the complete-close shutdown contract.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use std::time::Duration;

use tauri::{Manager, WebviewUrl};

use gptbridge_core::app::{self, manage_backend};
use gptbridge_core::lifecycle;
use gptbridge_core::native::paths;

use crate::js_bridge;
use crate::webview_host;

const SHUTDOWN_DEADLINE_MS: u64 = 15_000;

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
    webview_host::close_all_sessions(app);
    js_bridge::loopback::stop_embedded_browser_bridge();
    if manage_backend() {
        // Backend shutdown is awaited but bounded — a stalled graceful stop
        // must never leave the UI running as a detached orphan.
        let (tx, rx) = std::sync::mpsc::channel::<()>();
        std::thread::spawn(move || {
            lifecycle::stop_backend();
            let _ = tx.send(());
        });
        let _ = rx.recv_timeout(Duration::from_millis(SHUTDOWN_DEADLINE_MS));
    }
    app::report("main.ui-shutdown", serde_json::json!({}));
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
                    if let Some(webview) = webview_host::find_webview(&window, "main") {
                        let _ = webview.eval("window.location.reload()");
                        app::report("renderer.hot-reload", serde_json::json!({}));
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
        let Some(webview) = webview_host::find_webview(&window, "main") else { return };
        match webview.url() {
            Ok(url) if url.as_str() == "about:blank" || url.as_str().is_empty() => {
                app::report("webview.load-watchdog.reload", serde_json::json!({}));
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
    webview_host::record_content_size(initial_size, window.scale_factor().unwrap_or(1.0));
    let _webview = window.add_child(
        tauri::webview::WebviewBuilder::new("main", webview_url)
            .auto_resize()
            .initialization_script(js_bridge::PRELOAD_SHIM),
        tauri::LogicalPosition::new(0, 0),
        initial_size,
    )?;
    js_bridge::channels::apply_adaptive_zoom(&window);
    let _ = window.show();
    let _ = window.set_focus();
    Ok(window)
}

/// Tauri application entry — builds the governed shell and runs the event
/// loop.  Never returns while the application session is live.
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // Second-instance contract: re-check the managed backend and
            // focus the existing window instead of starting a new stack.
            if manage_backend() {
                lifecycle::ensure_backend_started();
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
        .invoke_handler(tauri::generate_handler![js_bridge::gptbridge_invoke])
        .setup(|app| {
            app::report(
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
            js_bridge::loopback::start_embedded_browser_bridge(&app.handle());
            app::report("window.ready", serde_json::json!({}));

            // Backend startup runs in the background and does not block the
            // UI (A60: the launcher only spawns boot_core; boot_core starts
            // and supervises main.py per A61 ordering).
            if manage_backend() {
                std::thread::spawn(lifecycle::start_backend);
            }
            app::report("bootstrap.ready", serde_json::json!({}));
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
                        webview_host::hide_all_sessions(&window.app_handle());
                        return;
                    }
                    webview_host::record_content_size(*size, window.scale_factor().unwrap_or(1.0));
                    webview_host::on_window_resized(&window.app_handle());
                    js_bridge::channels::apply_adaptive_zoom(window);
                }
                tauri::WindowEvent::CloseRequested { .. } => {
                    webview_host::close_all_sessions(&window.app_handle());
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

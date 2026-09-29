//! Tool-window host: renderer navigation, session reparent, shutdown.

use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::time::Duration;

use tauri::{Manager, WebviewUrl};

use crate::tool_bridge;
use crate::webview_host as embedded;

use super::backend::{TOOL_PRELOAD_SHIM, acquire_tool_mutex, spawn_packaged_backend};
use super::config::{load_env_config, load_packaged_config};
use super::{
    SHUTDOWN_DEADLINE_MS, backend_child_pid, shutdown_complete,
    tool_config, tool_config_cell, tool_log,
};

fn renderer_file_url(entry: &Path) -> tauri::Url {
    tauri::Url::from_file_path(entry).unwrap_or_else(|_| "about:blank".parse().unwrap())
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
                    if let Some(webview) = crate::webview_host::find_webview(&window, "tool") {
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
    std::env::set_var(
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
        "--disable-gpu-vsync",
    );

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
        .invoke_handler(tauri::generate_handler![crate::js_bridge::gptbridge_invoke])
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
            embedded::record_content_size(initial_size, window.scale_factor().unwrap_or(1.0));

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
                    embedded::record_content_size(*size, window.scale_factor().unwrap_or(1.0));
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

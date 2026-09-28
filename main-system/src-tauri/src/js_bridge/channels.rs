//! channels.rs — channel dispatch parity with the retired
//! src-ui/main/ipcHandlers.ts + preload.ts whitelist.  The renderer shim
//! calls the single ``gptbridge_invoke`` command with the channel name and
//! the argument array.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::sync::OnceLock;

use tauri::{AppHandle, Manager};

use gptbridge_core::app::{manage_backend, PRODUCT_VERSION};
use gptbridge_core::ipc;
use gptbridge_core::lifecycle;
use gptbridge_core::native::paths::is_path_inside;
use gptbridge_core::native::{metrics, paths, sizes};
use gptbridge_core::state::perf_slo;

use crate::webview_host;
const MIN_UI_ZOOM: f64 = 0.85;
const MAX_UI_ZOOM: f64 = 1.3;

/// UI zoom preference — the renderer-visible zoom multiplies this preferred
/// factor with the adaptive viewport scale (same contract as ipcHandlers).
fn ui_zoom() -> &'static AtomicU64 {
    static ZOOM: OnceLock<AtomicU64> = OnceLock::new();
    ZOOM.get_or_init(|| AtomicU64::new(1.0f64.to_bits()))
}

pub fn current_ui_zoom() -> f64 {
    f64::from_bits(ui_zoom().load(Ordering::SeqCst))
}

fn set_ui_zoom(value: f64) {
    ui_zoom().store(value.to_bits(), Ordering::SeqCst);
}

fn clamp_ui_zoom(value: f64) -> f64 {
    value.clamp(MIN_UI_ZOOM, MAX_UI_ZOOM)
}

// ---------------------------------------------------------------------------
// Adaptive zoom (adaptiveZoom.ts port)
// ---------------------------------------------------------------------------

const MIN_VIEWPORT_SCALE: f64 = 0.72;
const MAX_VIEWPORT_SCALE: f64 = 1.5;
const MIN_EFFECTIVE_ZOOM: f64 = 0.62;
const MAX_EFFECTIVE_ZOOM: f64 = 1.6;

fn zoom_profile() -> &'static Mutex<Option<(f64, f64)>> {
    static PROFILE: OnceLock<Mutex<Option<(f64, f64)>>> = OnceLock::new();
    PROFILE.get_or_init(|| Mutex::new(None))
}

pub fn calculate_adaptive_zoom(
    width: f64,
    height: f64,
    reference_width: f64,
    reference_height: f64,
    preferred: f64,
) -> f64 {
    let width = width.max(1.0);
    let height = height.max(1.0);
    let reference_width = reference_width.max(1.0);
    let reference_height = reference_height.max(1.0);
    let preferred = if preferred.is_finite() && preferred > 0.0 {
        preferred
    } else {
        1.0
    };
    let viewport_scale = (width / reference_width)
        .min(height / reference_height)
        .clamp(MIN_VIEWPORT_SCALE, MAX_VIEWPORT_SCALE);
    let effective = (viewport_scale * preferred).clamp(MIN_EFFECTIVE_ZOOM, MAX_EFFECTIVE_ZOOM);
    (effective * 1000.0).round() / 1000.0
}

pub fn apply_adaptive_zoom(window: &tauri::Window) {
    // Use the event-fed content-size cache — querying inner_size/
    // scale_factor here takes tao's window_state lock, which is an AB-BA
    // hazard when this runs inside a Resized dispatch.
    let Some((width, height)) = webview_host::current_content_size() else {
        return;
    };
    let logical = tauri::LogicalSize { width, height };
    let mut profile = zoom_profile().lock().unwrap();
    if profile.is_none() {
        *profile = Some((logical.width, logical.height));
    }
    let (ref_w, ref_h) = profile.unwrap();
    let factor = calculate_adaptive_zoom(
        logical.width,
        logical.height,
        ref_w,
        ref_h,
        current_ui_zoom(),
    );
    drop(profile);
    if let Some(webview) = webview_host::find_webview(window, "main") {
        let _ = webview.set_zoom(factor);
    }
}

// ---------------------------------------------------------------------------
// Channel dispatch
// ---------------------------------------------------------------------------

fn str_arg(args: &serde_json::Value, key: &str) -> String {
    args.get(key)
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string()
}

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn main_window(app: &AppHandle) -> Option<tauri::Window> {
    app.get_window("main")
}

pub async fn dispatch(app: AppHandle, channel: &str, args: serde_json::Value) -> serde_json::Value {
    match channel {
        "app:get-status" => {
            let runtime = lifecycle::backend_runtime_info();
            let packaged = paths::is_packaged();
            serde_json::json!({
                "isPackaged": packaged,
                "version": PRODUCT_VERSION,
                "backendStatus": runtime["status"],
                "backendManaged": manage_backend(),
                "backendReady": runtime["ready"],
                "backendStartupMs": runtime["startupMs"],
                "backendMessage": runtime["message"],
                "environment": if packaged { "production" } else { "source" },
                "sourceProduction": !packaged,
                "systemReady": if manage_backend() {
                    runtime["ready"].as_bool() == Some(true)
                } else {
                    runtime["status"].as_str() != Some("error")
                },
                "bootTimestamp": now_ms(),
                "systemMetrics": metrics::get_system_metrics(),
            })
        }
        "app:get-perf-slo" => perf_slo::get_perf_slo(&paths::path_library().workspace_root),
        "app:ensure-backend-started" => {
            if !manage_backend() {
                return serde_json::json!({
                    "ok": false,
                    "managed": false,
                    "backendStatus": lifecycle::get_backend_status().as_str(),
                    "message": "backend manager is disabled by GPTBRIDGE_MANAGE_BACKEND=0",
                });
            }
            let status = lifecycle::ensure_backend_started();
            serde_json::json!({
                "ok": status != lifecycle::BackendStatus::Error,
                "managed": true,
                "backendStatus": status.as_str(),
            })
        }
        "app:get-backend-session" => ipc::backend_session_descriptor(),
        "app:restart-backend" => {
            if !manage_backend() {
                return serde_json::json!({
                    "ok": false,
                    "managed": false,
                    "backendStatus": lifecycle::get_backend_status().as_str(),
                    "message": "後端目前由外部 dev 腳本管理，無法由桌面 shell 單獨重啟。",
                });
            }
            let status = lifecycle::restart_backend();
            serde_json::json!({
                "ok": status != lifecycle::BackendStatus::Error,
                "managed": true,
                "backendStatus": status.as_str(),
            })
        }
        "app:restart" => {
            app.restart();
        }
        "app:get-platform-tool-sizes" => {
            let force = args["forceRefresh"].as_bool() == Some(true);
            let root = paths::path_library().workspace_root.clone();
            let tools = sizes::platform_tool_sizes(&root, force);
            let main_system = sizes::main_system_size(&root, force);
            let shared_layer = sizes::shared_layer_size(&root, force);
            let workspace =
                sizes::workspace_size(&root, &tools, &main_system, &shared_layer, force);
            serde_json::json!({
                "ok": true,
                "tools": tools,
                "main_system": main_system,
                "shared_layer": shared_layer,
                "workspace": workspace,
                "source": "governed-local-folder-inventory",
            })
        }
        "app:reload-window" => {
            match main_window(&app).and_then(|w| webview_host::find_webview(&w, "main")) {
                Some(webview) => {
                    let _ = webview.eval("window.location.reload()");
                    serde_json::json!({"ok": true})
                }
                None => serde_json::json!({"ok": false}),
            }
        }
        "app:reload-window-hard" => {
            match main_window(&app).and_then(|w| webview_host::find_webview(&w, "main")) {
                Some(webview) => {
                    // Hard reload: bypass caches via a fresh navigation to the
                    // current URL — closest available semantics under WebView2.
                    if let Ok(url) = webview.url() {
                        let _ = webview.navigate(url);
                    } else {
                        let _ = webview.eval("window.location.reload()");
                    }
                    serde_json::json!({"ok": true})
                }
                None => serde_json::json!({"ok": false}),
            }
        }
        "app:get-ui-zoom" => serde_json::json!({"ok": true, "factor": current_ui_zoom()}),
        "app:set-ui-zoom" => {
            let factor = args["factor"].as_f64().unwrap_or(1.0);
            if !factor.is_finite() || factor <= 0.0 {
                return serde_json::json!({"ok": false, "message": "Invalid zoom factor"});
            }
            set_ui_zoom(clamp_ui_zoom(factor));
            if let Some(window) = main_window(&app) {
                apply_adaptive_zoom(&window);
            }
            serde_json::json!({"ok": true, "factor": current_ui_zoom()})
        }
        "app:open-path" => {
            let raw = str_arg(&args, "path");
            let base = str_arg(&args, "basePath");
            let relative = str_arg(&args, "relativePath");
            let mode = if args["mode"].as_str() == Some("reveal") {
                "reveal"
            } else {
                "open"
            };
            let workspace_root = paths::path_library().workspace_root.clone();
            let mut target = if raw.is_empty() {
                std::path::PathBuf::new()
            } else {
                std::path::PathBuf::from(&raw)
            };
            if !base.is_empty() && !relative.is_empty() {
                let resolved_base = std::path::PathBuf::from(&base);
                let resolved_target = resolved_base.join(&relative);
                if !is_path_inside(&resolved_base, &resolved_target) {
                    return serde_json::json!({"ok": false, "message": "Path is outside the selected folder"});
                }
                target = resolved_target;
            }
            if target.as_os_str().is_empty() {
                return serde_json::json!({"ok": false, "message": "Missing path"});
            }
            if !is_path_inside(&workspace_root, &target) {
                return serde_json::json!({"ok": false, "message": "Path is outside the project workspace"});
            }
            if !target.exists() {
                return serde_json::json!({"ok": false, "message": "File no longer exists"});
            }
            let result = if mode == "reveal" {
                opener::reveal(&target).map_err(|e| e.to_string())
            } else {
                opener::open(&target).map_err(|e| e.to_string())
            };
            match result {
                Ok(()) => serde_json::json!({"ok": true}),
                Err(e) => serde_json::json!({"ok": false, "message": e}),
            }
        }
        "dialog:select-folder" => {
            let (tx, rx) = std::sync::mpsc::channel::<String>();
            tauri_plugin_dialog::DialogExt::dialog(&app)
                .file()
                .pick_folder(move |path| {
                    let _ = tx.send(path.map(|p| p.to_string()).unwrap_or_default());
                });
            serde_json::Value::String(rx.recv().unwrap_or_default())
        }
        "dialog:create-file" => {
            let default_path = args.as_str().unwrap_or_default().to_string();
            let (tx, rx) = std::sync::mpsc::channel::<String>();
            let mut builder = tauri_plugin_dialog::DialogExt::dialog(&app).file();
            builder = builder.add_filter("Code", &["py", "json", "md", "txt"]);
            builder = builder.add_filter("All Files", &["*"]);
            if !default_path.is_empty() {
                builder = builder.set_file_name(&default_path);
            }
            builder.save_file(move |path| {
                let _ = tx.send(path.map(|p| p.to_string()).unwrap_or_default());
            });
            serde_json::Value::String(rx.recv().unwrap_or_default())
        }
        "dialog:open-file" => {
            let default_path = args.as_str().unwrap_or_default().to_string();
            let (tx, rx) = std::sync::mpsc::channel::<String>();
            let mut builder = tauri_plugin_dialog::DialogExt::dialog(&app).file();
            builder = builder
                .add_filter("Structured Data", &["csv", "tsv", "json", "xlsx", "xls"])
                .add_filter("Code", &["py", "json", "md", "txt"])
                .add_filter("All Files", &["*"]);
            if !default_path.is_empty() {
                builder = builder.set_file_name(&default_path);
            }
            builder.pick_file(move |path| {
                let _ = tx.send(path.map(|p| p.to_string()).unwrap_or_default());
            });
            serde_json::Value::String(rx.recv().unwrap_or_default())
        }
        "embedded-browser:create" => webview_host::create_session(
            &app,
            str_arg(&args, "id"),
            str_arg(&args, "ownerModule"),
            str_arg(&args, "url"),
            args.get("bounds").and_then(|b| {
                Some(webview_host::BrowserBounds {
                    x: b.get("x")?.as_f64()?,
                    y: b.get("y")?.as_f64()?,
                    width: b.get("width")?.as_f64()?,
                    height: b.get("height")?.as_f64()?,
                })
            }),
        ),
        "embedded-browser:navigate" => {
            webview_host::navigate_session(&app, &str_arg(&args, "id"), &str_arg(&args, "url"))
        }
        "embedded-browser:execute" => {
            match webview_host::execute_script(
                &app,
                &str_arg(&args, "id"),
                &str_arg(&args, "script"),
            ) {
                Ok(result) => serde_json::json!({"ok": true, "result": result}),
                Err(message) => serde_json::json!({"ok": false, "message": message}),
            }
        }
        "embedded-browser:show" => webview_host::show_session(&app, &str_arg(&args, "id")),
        "embedded-browser:hide" => webview_host::hide_session(&app, &str_arg(&args, "id")),
        "embedded-browser:close" => webview_host::close_session(&app, &str_arg(&args, "id")),
        "embedded-browser:resize" => {
            let b = args.get("bounds").cloned().unwrap_or_default();
            webview_host::resize_session(
                &app,
                &str_arg(&args, "id"),
                webview_host::BrowserBounds {
                    x: b["x"].as_f64().unwrap_or(0.0),
                    y: b["y"].as_f64().unwrap_or(0.0),
                    width: b["width"].as_f64().unwrap_or(0.0),
                    height: b["height"].as_f64().unwrap_or(0.0),
                },
            )
        }
        "embedded-browser:list" => webview_host::list_sessions(),
        "embedded-browser:url" => {
            let url = webview_host::session_url(&app, &str_arg(&args, "id"));
            serde_json::json!({"ok": url.is_some(), "url": url})
        }
        "embedded-browser:close-module" => serde_json::json!({
            "closed": webview_host::close_module_sessions(&app, &str_arg(&args, "ownerModule"))
        }),
        _ => serde_json::json!({"ok": false, "message": format!("Blocked IPC channel: {channel}")}),
    }
}

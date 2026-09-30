//! tool_dispatch.rs — channel dispatch parity with
//! ``scripts/source-tool-ui-host/main.cjs`` + ``preload.cjs`` for
//! ``--tool-window`` mode.  The renderer shim calls the single
//! ``gptbridge_invoke`` command with the channel name and the argument
//! array, exactly like the main window's shim.

use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::OnceLock;

use tauri::AppHandle;

use crate::tool_window;
use crate::webview_host as embedded;
use gptbridge_core::native::paths::is_path_inside;

/// Preload whitelist parity — identical to source-tool-ui-host
/// preload.cjs ``allowedInvokeChannels``.
pub const TOOL_ALLOWED_CHANNELS: [&str; 22] = [
    "app:ensure-backend-started",
    "app:get-backend-session",
    "app:open-path",
    "dialog:select-folder",
    "dialog:validate-folder",
    "dialog:create-file",
    "dialog:open-file",
    "embedded-browser:create",
    "embedded-browser:dom-op",
    "embedded-browser:navigate",
    "embedded-browser:execute",
    "embedded-browser:show",
    "embedded-browser:hide",
    "embedded-browser:close",
    "embedded-browser:resize",
    "embedded-browser:reload",
    "embedded-browser:go-back",
    "embedded-browser:go-forward",
    "embedded-browser:state",
    "embedded-browser:list",
    "embedded-browser:url",
    "embedded-browser:close-module",
];

/// User-granted directory/file capabilities — paths the user picked in a
/// dialog may be opened once via ``app:open-path`` even when they sit
/// outside the workspace (Electron ``openPathCapabilities`` parity: the
/// capability is consumed on use).
fn open_path_capabilities() -> &'static Mutex<HashSet<PathBuf>> {
    static CAPS: OnceLock<Mutex<HashSet<PathBuf>>> = OnceLock::new();
    CAPS.get_or_init(|| Mutex::new(HashSet::new()))
}

fn str_arg(args: &serde_json::Value, key: &str) -> String {
    args.get(key)
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string()
}

fn pick_folder(app: &AppHandle) -> String {
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    tauri_plugin_dialog::DialogExt::dialog(app)
        .file()
        .pick_folder(move |path| {
            let _ = tx.send(path.map(|p| p.to_string()).unwrap_or_default());
        });
    rx.recv().unwrap_or_default()
}

fn save_file(app: &AppHandle, default_path: &str) -> String {
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    let mut builder = tauri_plugin_dialog::DialogExt::dialog(app).file();
    if !default_path.is_empty() {
        builder = builder.set_file_name(default_path);
    }
    builder.save_file(move |path| {
        let _ = tx.send(path.map(|p| p.to_string()).unwrap_or_default());
    });
    rx.recv().unwrap_or_default()
}

fn pick_file(app: &AppHandle, default_path: &str) -> String {
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    let mut builder = tauri_plugin_dialog::DialogExt::dialog(app).file();
    if !default_path.is_empty() {
        builder = builder.set_file_name(default_path);
    }
    builder.pick_file(move |path| {
        let _ = tx.send(path.map(|p| p.to_string()).unwrap_or_default());
    });
    rx.recv().unwrap_or_default()
}

/// Grant open-path capability for a user-picked path (Electron:
/// ``openPathCapabilities.add(path.resolve(selected))``).
fn grant_capability(path: &str) {
    if path.is_empty() {
        return;
    }
    open_path_capabilities()
        .lock()
        .unwrap()
        .insert(PathBuf::from(path));
}

pub async fn dispatch(app: AppHandle, channel: &str, args: serde_json::Value) -> serde_json::Value {
    if !TOOL_ALLOWED_CHANNELS.contains(&channel) {
        return serde_json::json!({
            "ok": false,
            "message": format!("Blocked IPC channel: {channel}")
        });
    }
    let payload = if args.is_array() {
        args.get(0).cloned().unwrap_or(serde_json::Value::Null)
    } else {
        args
    };
    let args = &payload;
    let config = tool_window::tool_config();

    match channel {
        "app:ensure-backend-started" => serde_json::json!({
            "ok": true,
            "managed": true,
            "runtimeMode": config.runtime_mode,
        }),
        "app:get-backend-session" => serde_json::json!({
            "token": config.backend_token,
            "websocketUrl": config.websocket_url,
            "backendVersion": config.tool_version,
            "protocolVersion": 1,
            "runtimeMode": config.runtime_mode,
        }),
        "app:open-path" => {
            let raw = str_arg(args, "path");
            let base = str_arg(args, "basePath");
            let relative = str_arg(args, "relativePath");
            let target = if !raw.is_empty() {
                PathBuf::from(&raw)
            } else {
                let resolved_base = if base.is_empty() {
                    config.workspace_root.clone()
                } else {
                    PathBuf::from(&base)
                };
                resolved_base.join(&relative)
            };
            let granted = open_path_capabilities().lock().unwrap().remove(&target);
            if !is_path_inside(&config.workspace_root, &target) && !granted {
                return serde_json::json!({
                    "ok": false,
                    "message": "Path is outside the application workspace"
                });
            }
            let result = if args["mode"].as_str() == Some("reveal") {
                opener::reveal(&target).map_err(|e| e.to_string())
            } else {
                opener::open(&target).map_err(|e| e.to_string())
            };
            match result {
                Ok(()) => serde_json::json!({"ok": true, "path": target}),
                Err(e) => serde_json::json!({"ok": false, "message": e}),
            }
        }
        "dialog:select-folder" => {
            let selected = pick_folder(&app);
            grant_capability(&selected);
            serde_json::Value::String(selected)
        }
        "dialog:validate-folder" => {
            let candidate = args.as_str().unwrap_or_default().trim().to_string();
            let resolved = if PathBuf::from(&candidate).is_absolute() {
                std::fs::canonicalize(&candidate).ok()
            } else {
                None
            };
            match resolved.filter(|p| p.is_dir()) {
                Some(path) => {
                    open_path_capabilities()
                        .lock()
                        .unwrap()
                        .insert(path.clone());
                    serde_json::Value::String(path.to_string_lossy().into_owned())
                }
                None => serde_json::Value::String(String::new()),
            }
        }
        "dialog:create-file" => {
            let default_path = args.as_str().unwrap_or_default();
            let selected = save_file(&app, default_path);
            grant_capability(&selected);
            serde_json::Value::String(selected)
        }
        "dialog:open-file" => {
            let default_path = args.as_str().unwrap_or_default();
            let selected = pick_file(&app, default_path);
            grant_capability(&selected);
            serde_json::Value::String(selected)
        }
        "embedded-browser:create" => embedded::create_session(
            &app,
            str_arg(args, "id"),
            str_arg(args, "ownerModule"),
            str_arg(args, "url"),
            args.get("bounds").and_then(|b| {
                Some(embedded::BrowserBounds {
                    x: b.get("x")?.as_f64()?,
                    y: b.get("y")?.as_f64()?,
                    width: b.get("width")?.as_f64()?,
                    height: b.get("height")?.as_f64()?,
                })
            }),
        ),
        "embedded-browser:navigate" => {
            embedded::navigate_session(&app, &str_arg(args, "id"), &str_arg(args, "url"))
        }
        "embedded-browser:execute" => {
            match embedded::execute_script(&app, &str_arg(args, "id"), &str_arg(args, "script")) {
                Ok(result) => serde_json::json!({"ok": true, "result": result}),
                Err(message) => serde_json::json!({"ok": false, "message": message}),
            }
        }
        "embedded-browser:show" => embedded::show_session(&app, &str_arg(args, "id")),
        "embedded-browser:hide" => embedded::hide_session(&app, &str_arg(args, "id")),
        "embedded-browser:close" => embedded::close_session(&app, &str_arg(args, "id")),
        "embedded-browser:resize" => {
            let b = args.get("bounds").cloned().unwrap_or_default();
            embedded::resize_session(
                &app,
                &str_arg(args, "id"),
                embedded::BrowserBounds {
                    x: b["x"].as_f64().unwrap_or(0.0),
                    y: b["y"].as_f64().unwrap_or(0.0),
                    width: b["width"].as_f64().unwrap_or(0.0),
                    height: b["height"].as_f64().unwrap_or(0.0),
                },
            )
        }
        "embedded-browser:reload" => embedded::reload_session(&app, &str_arg(args, "id")),
        "embedded-browser:go-back" => embedded::go_back_session(&app, &str_arg(args, "id")),
        "embedded-browser:go-forward" => embedded::go_forward_session(&app, &str_arg(args, "id")),
        "embedded-browser:state" => embedded::session_state(&app, &str_arg(args, "id")),
        "embedded-browser:list" => embedded::list_sessions(),
        "embedded-browser:url" => {
            let url = embedded::session_url(&app, &str_arg(args, "id"));
            serde_json::json!({"ok": url.is_some(), "url": url})
        }
        "embedded-browser:close-module" => serde_json::json!({
            "ok": true,
            "closed": embedded::close_module_sessions(&app, &str_arg(args, "ownerModule"))
        }),
        "embedded-browser:dom-op" => dom_op(&app, args),
        _ => serde_json::json!({"ok": false, "message": format!("Blocked IPC channel: {channel}")}),
    }
}

/// Backend-delegated DOM op — composite ``embedded-browser:dom-op``
/// channel consumed by the ai-collaboration window app's
/// ``ai_collab_browser_op`` handler.  Maps the governed op vocabulary
/// (``create``/``navigate``/``exec``/``url``/``close``) onto the
/// webview_host session primitives; result/error shapes mirror the
/// Wails ``BrowserManager.DOMOp`` contract so the Go host's ``opsOK``
/// gate reads either backend identically.
fn dom_op_fail(code: &str, message: &str) -> serde_json::Value {
    serde_json::json!({"ok": false, "error_code": code, "message": message})
}

fn dom_op(app: &AppHandle, args: &serde_json::Value) -> serde_json::Value {
    let op = str_arg(args, "op");
    let sid = str_arg(args, "session_id");
    let url = str_arg(args, "url");
    let script = str_arg(args, "script");
    if sid.is_empty() {
        return dom_op_fail("SESSION_REQUIRED", "session_id is required");
    }
    match op.as_str() {
        "create" => {
            // Backend-delegated sessions stay unbounded until the
            // owning UI shows/resizes them through the regular
            // embedded-browser channels.
            let res = embedded::create_session(
                app,
                sid.clone(),
                tool_window::tool_config().tool_id.clone(),
                url,
                None,
            );
            if res["ok"].as_bool().unwrap_or(false) {
                serde_json::json!({"ok": true, "id": sid, "backend": "tauri-webview2"})
            } else {
                dom_op_fail(
                    "SESSION_CREATE_FAILED",
                    res["message"].as_str().unwrap_or("session create failed"),
                )
            }
        }
        "navigate" => {
            if url.is_empty() {
                return dom_op_fail("INVALID_URL", "url is required");
            }
            let res = embedded::navigate_session(app, &sid, &url);
            if res["ok"].as_bool().unwrap_or(false) {
                serde_json::json!({
                    "ok": true,
                    "url": embedded::session_url(app, &sid).unwrap_or_default(),
                })
            } else {
                dom_op_fail(
                    res["message"].as_str().unwrap_or("NAVIGATE_FAILED"),
                    res["message"].as_str().unwrap_or("navigation failed"),
                )
            }
        }
        "exec" => {
            if script.is_empty() {
                return dom_op_fail("INVALID_SCRIPT", "script is required");
            }
            match embedded::execute_script(app, &sid, &script) {
                Ok(result) => serde_json::json!({"ok": true, "result": result}),
                Err(message) => dom_op_fail(&message, &message),
            }
        }
        "url" => match embedded::session_url(app, &sid) {
            Some(live) => serde_json::json!({"ok": true, "url": live}),
            None => dom_op_fail("SESSION_NOT_FOUND", "session not found"),
        },
        "close" => {
            if embedded::session_url(app, &sid).is_none() {
                return dom_op_fail("SESSION_NOT_FOUND", "session not found");
            }
            embedded::close_session(app, &sid)
        }
        _ => dom_op_fail("UNSUPPORTED_OP", &format!("unsupported op: {op}")),
    }
}

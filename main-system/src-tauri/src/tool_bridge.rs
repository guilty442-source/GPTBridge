//! tool_bridge.rs — tool-window loopback bridge.
//!
//! Parity with the Electron tool host's ``startToolWindowBridge`` —
//! ``main.cjs`` served ``POST /invoke`` on 127.0.0.1 with a per-launch
//! token published to
//! ``<tool-root>/runtime/ipc/tool-window-browser-bridge.json`` so the
//! tool's own governed backend drives the embedded browser views hosted
//! inside THIS window (the main-system bridge is never used for
//! tool-window sessions — it cannot display or share this window's
//! state).
//!
//! ``POST /event`` receives navigation/loading pushes from the
//! ``--embedded-worker`` helper processes and re-emits them into the
//! renderer as ``embedded-browser:event`` (the Electron host forwarded
//! BrowserView webContents events directly).

use std::net::TcpListener;
use std::sync::atomic::{AtomicU16, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use tauri::{AppHandle, Emitter};

use crate::governor_budget;
use crate::js_bridge::loopback::{read_request, respond, Request};
use crate::tool_window;
use crate::webview_host as embedded;
use gptbridge_core::security::constant_time_eq;

const BRIDGE_HOST: &str = "127.0.0.1";
const TOKEN_HEADER: &str = "x-gptbridge-bridge-token";

static BRIDGE_PORT: AtomicU16 = AtomicU16::new(0);

fn bridge_token_cell() -> &'static Mutex<String> {
    static TOKEN: OnceLock<Mutex<String>> = OnceLock::new();
    TOKEN.get_or_init(|| Mutex::new(String::new()))
}

pub fn bridge_port() -> u16 {
    BRIDGE_PORT.load(Ordering::SeqCst)
}

pub fn bridge_token() -> String {
    bridge_token_cell().lock().unwrap().clone()
}

fn state_path() -> std::path::PathBuf {
    tool_window::tool_config()
        .tool_root
        .join("runtime")
        .join("ipc")
        .join("tool-window-browser-bridge.json")
}

fn iso_now() -> String {
    gptbridge_core::app::iso_now()
}

fn publish_state(port: u16, token: &str) {
    let target = state_path();
    if let Some(parent) = target.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let body = serde_json::json!({
        "host": BRIDGE_HOST,
        "port": port,
        "token": token,
        "pid": std::process::id(),
        "tool_id": tool_window::tool_config().tool_id,
        "kind": "tool-window",
        "started_at": iso_now(),
    });
    let _ = std::fs::write(
        &target,
        serde_json::to_string_pretty(&body).unwrap_or_default(),
    );
}

fn remove_state() {
    let _ = std::fs::remove_file(state_path());
}

fn str_arg(args: &serde_json::Value, key: &str) -> String {
    args.get(key)
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string()
}

fn dispatch_channel(app: &AppHandle, channel: &str, args: &serde_json::Value) -> serde_json::Value {
    match channel {
        "embedded-browser:create" => embedded::create_session(
            app,
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
            embedded::navigate_session(app, &str_arg(args, "id"), &str_arg(args, "url"))
        }
        "embedded-browser:execute" => {
            match embedded::execute_script(app, &str_arg(args, "id"), &str_arg(args, "script")) {
                Ok(result) => serde_json::json!({"ok": true, "result": result}),
                Err(message) => serde_json::json!({"ok": false, "message": message}),
            }
        }
        "embedded-browser:show" => embedded::show_session(app, &str_arg(args, "id")),
        "embedded-browser:hide" => embedded::hide_session(app, &str_arg(args, "id")),
        "embedded-browser:close" => embedded::close_session(app, &str_arg(args, "id")),
        "embedded-browser:resize" => {
            let b = args.get("bounds").cloned().unwrap_or_default();
            embedded::resize_session(
                app,
                &str_arg(args, "id"),
                embedded::BrowserBounds {
                    x: b["x"].as_f64().unwrap_or(0.0),
                    y: b["y"].as_f64().unwrap_or(0.0),
                    width: b["width"].as_f64().unwrap_or(0.0),
                    height: b["height"].as_f64().unwrap_or(0.0),
                },
            )
        }
        "embedded-browser:reload" => embedded::reload_session(app, &str_arg(args, "id")),
        "embedded-browser:go-back" => embedded::go_back_session(app, &str_arg(args, "id")),
        "embedded-browser:go-forward" => embedded::go_forward_session(app, &str_arg(args, "id")),
        "embedded-browser:state" => embedded::session_state(app, &str_arg(args, "id")),
        "embedded-browser:list" => embedded::list_sessions(),
        "embedded-browser:url" => {
            let url = embedded::session_url(app, &str_arg(args, "id"));
            serde_json::json!({"ok": url.is_some(), "url": url})
        }
        "embedded-browser:close-module" => serde_json::json!({
            "ok": true,
            "closed": embedded::close_module_sessions(app, &str_arg(args, "ownerModule"))
        }),
        _ => serde_json::json!({"ok": false, "message": "BRIDGE_CHANNEL_UNKNOWN"}),
    }
}

fn authed(expected: &str, request: &Request) -> bool {
    let provided = request
        .headers
        .iter()
        .find(|(n, _)| n == TOKEN_HEADER)
        .map(|(_, v)| v.clone())
        .unwrap_or_default();
    !provided.is_empty()
        && provided.len() == expected.len()
        && constant_time_eq(provided.as_bytes(), expected.as_bytes())
}

fn handle_connection(app: &AppHandle, mut stream: std::net::TcpStream) {
    let Some(request) = read_request(&mut stream) else {
        respond(
            &mut stream,
            400,
            serde_json::json!({"ok": false, "message": "BRIDGE_REQUEST_INVALID"}),
        );
        return;
    };
    let expected = bridge_token();

    // Worker event push: a session worker reports navigation/loading state
    // so the bridge can emit embedded-browser:event into the renderer.
    if request.method == "POST" && request.path == "/event" {
        if !authed(&expected, &request) {
            respond(
                &mut stream,
                403,
                serde_json::json!({"ok": false, "message": "BRIDGE_TOKEN_INVALID"}),
            );
            return;
        }
        if let Ok(payload) = serde_json::from_slice::<serde_json::Value>(&request.body) {
            let _ = app.emit_to("tool", "embedded-browser:event", payload);
        }
        respond(&mut stream, 200, serde_json::json!({"ok": true}));
        return;
    }

    if request.method != "POST" || request.path != "/invoke" {
        respond(
            &mut stream,
            404,
            serde_json::json!({"ok": false, "message": "NOT_FOUND"}),
        );
        return;
    }
    if !authed(&expected, &request) {
        respond(
            &mut stream,
            403,
            serde_json::json!({"ok": false, "message": "BRIDGE_TOKEN_INVALID"}),
        );
        return;
    }
    let payload: serde_json::Value = match serde_json::from_slice(&request.body) {
        Ok(v) => v,
        Err(_) => {
            respond(
                &mut stream,
                400,
                serde_json::json!({"ok": false, "message": "BRIDGE_BODY_INVALID"}),
            );
            return;
        }
    };
    let channel = payload["channel"].as_str().unwrap_or_default().to_string();
    if channel.is_empty() {
        respond(
            &mut stream,
            404,
            serde_json::json!({"ok": false, "message": "BRIDGE_CHANNEL_UNKNOWN"}),
        );
        return;
    }
    let args = payload
        .get("args")
        .cloned()
        .filter(|v| v.is_object())
        .unwrap_or_else(|| serde_json::json!({}));
    let result = dispatch_channel(app, &channel, &args);
    respond(&mut stream, 200, result);
}

/// Start the tool-window bridge (idempotent) and publish its endpoint file
/// for the tool's governed backend.
pub fn start_tool_bridge(app: &AppHandle) {
    if bridge_port() != 0 {
        return;
    }
    let token = {
        let mut buf = [0u8; 32];
        let _ = getrandom::getrandom(&mut buf);
        hex::encode(buf)
    };
    *bridge_token_cell().lock().unwrap() = token.clone();
    let Ok(listener) = TcpListener::bind(format!("{BRIDGE_HOST}:0")) else {
        return;
    };
    let Ok(addr) = listener.local_addr() else {
        return;
    };
    let port = addr.port();
    BRIDGE_PORT.store(port, Ordering::SeqCst);
    publish_state(port, &token);

    // bounded-concurrency/v1: a governor-sized worker pool drains a
    // bounded pending queue; a full queue rejects with HTTP 503 —
    // never a thread per connection.
    let workers = governor_budget::resolve_workers("network", 2, 8);
    // Declared B16 latency envelope for a queued connection.
    const PENDING_DEADLINE: Duration = Duration::from_millis(2000);
    let pending = governor_budget::bounded_conn_pool(
        workers,
        32,
        PENDING_DEADLINE,
        app.clone(),
        |app, stream| handle_connection(app, stream),
    );
    std::thread::spawn(move || {
        for incoming in listener.incoming() {
            match incoming {
                Ok(stream) => {
                    use std::sync::mpsc::TrySendError;
                    match pending.try_send((stream, std::time::Instant::now())) {
                        Ok(()) => {}
                        Err(TrySendError::Full((mut s, _)))
                        | Err(TrySendError::Disconnected((mut s, _))) => {
                            respond(
                                &mut s,
                                503,
                                serde_json::json!({
                                    "ok": false,
                                    "message": "BRIDGE_CAPACITY_EXHAUSTED",
                                }),
                            );
                        }
                    }
                }
                Err(_) => std::thread::sleep(Duration::from_millis(50)),
            }
        }
    });
}

/// Stop the bridge: remove the published state file so stale endpoints are
/// never consumed by a later tool backend.
pub fn stop_tool_bridge() {
    remove_state();
    BRIDGE_PORT.store(0, Ordering::SeqCst);
    *bridge_token_cell().lock().unwrap() = String::new();
}

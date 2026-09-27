//! bridge.rs — port of src-ui/main/embedded-browser-bridge.ts.
//!
//! Token-guarded loopback HTTP endpoint owned by this process so tool UIs and
//! tool Python backends reach the embedded-browser session store without a
//! second browser stack.  Never leaves 127.0.0.1; the per-launch token is
//! published to the runtime state file.

use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::atomic::{AtomicU16, Ordering};
use std::sync::{Mutex, OnceLock};

use tauri::AppHandle;

use crate::embedded;
use crate::paths;

const BRIDGE_HOST: &str = "127.0.0.1";
const TOKEN_HEADER: &str = "x-gptbridge-bridge-token";
const MAX_BODY_BYTES: usize = 1_048_576;

static BRIDGE_PORT: AtomicU16 = AtomicU16::new(0);
static APP_HANDLE: OnceLock<AppHandle> = OnceLock::new();

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
    paths::path_library()
        .workspace_root
        .join("main-system")
        .join("runtime")
        .join("state")
        .join("embedded-browser-bridge.json")
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
        "started_at": iso_now(),
    });
    let _ = std::fs::write(&target, serde_json::to_string_pretty(&body).unwrap_or_default());
}

fn iso_now() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    // Civil-from-days conversion (Howard Hinnant's algorithm).
    let days = secs.div_euclid(86_400);
    let secs_of_day = secs.rem_euclid(86_400);
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    let (h, mi, s) = (secs_of_day / 3600, (secs_of_day % 3600) / 60, secs_of_day % 60);
    format!("{y:04}-{m:02}-{d:02}T{h:02}:{mi:02}:{s:02}Z")
}

fn remove_state() {
    let _ = std::fs::remove_file(state_path());
}

fn respond(stream: &mut std::net::TcpStream, status: u16, payload: serde_json::Value) {
    let body = serde_json::to_string(&payload).unwrap_or_else(|_| "{}".to_string());
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        _ => "Error",
    };
    let head = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(body.as_bytes());
}

struct Request {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

fn read_request(stream: &mut std::net::TcpStream) -> Option<Request> {
    stream
        .set_read_timeout(Some(std::time::Duration::from_secs(5)))
        .ok()?;
    let mut raw = Vec::with_capacity(4096);
    let mut buf = [0u8; 8192];
    let header_end;
    loop {
        match stream.read(&mut buf) {
            Ok(0) => return None,
            Ok(n) => {
                raw.extend_from_slice(&buf[..n]);
                if raw.len() > MAX_BODY_BYTES + 8192 {
                    return None;
                }
            }
            Err(_) => return None,
        }
        if let Some(pos) = raw
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
        {
            header_end = pos;
            break;
        }
    }
    let head_text = String::from_utf8_lossy(&raw[..header_end]).to_string();
    let mut lines = head_text.lines();
    let request_line = lines.next()?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next()?.to_string();
    let path = parts.next()?.to_string();
    let mut headers = Vec::new();
    let mut content_length = 0usize;
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            let name = name.trim().to_ascii_lowercase();
            let value = value.trim().to_string();
            if name == "content-length" {
                content_length = value.parse().unwrap_or(0);
            }
            headers.push((name, value));
        }
    }
    if content_length > MAX_BODY_BYTES {
        return None;
    }
    let mut body = raw[header_end + 4..].to_vec();
    while body.len() < content_length {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => body.extend_from_slice(&buf[..n]),
            Err(_) => break,
        }
    }
    body.truncate(content_length);
    Some(Request {
        method,
        path,
        headers,
        body,
    })
}

fn dispatch_channel(app: &AppHandle, channel: &str, args: &serde_json::Value) -> serde_json::Value {
    let str_arg = |key: &str| -> String {
        args.get(key).and_then(|v| v.as_str()).unwrap_or_default().to_string()
    };
    match channel {
        "embedded-browser:create" => embedded::create_session(
            app,
            str_arg("id"),
            str_arg("ownerModule"),
            str_arg("url"),
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
            embedded::navigate_session(app, &str_arg("id"), &str_arg("url"))
        }
        // Fail-closed: a backend-driven bridge call must never display a
        // view over the main system window (tool windows host their own).
        "embedded-browser:show" => serde_json::json!({
            "ok": false,
            "message": "EMBEDDED_BROWSER_SHOW_REQUIRES_TOOL_WINDOW"
        }),
        "embedded-browser:execute" => {
            match embedded::execute_script(app, &str_arg("id"), &str_arg("script")) {
                Ok(result) => serde_json::json!({"ok": true, "result": result}),
                Err(message) => serde_json::json!({"ok": false, "message": message}),
            }
        }
        "embedded-browser:hide" => embedded::hide_session(app, &str_arg("id")),
        "embedded-browser:close" => embedded::close_session(app, &str_arg("id")),
        "embedded-browser:resize" => {
            let b = args.get("bounds").cloned().unwrap_or_default();
            embedded::resize_session(
                app,
                &str_arg("id"),
                embedded::BrowserBounds {
                    x: b["x"].as_f64().unwrap_or(0.0),
                    y: b["y"].as_f64().unwrap_or(0.0),
                    width: b["width"].as_f64().unwrap_or(0.0),
                    height: b["height"].as_f64().unwrap_or(0.0),
                },
            )
        }
        "embedded-browser:list" => embedded::list_sessions(),
        "embedded-browser:url" => {
            let url = embedded::session_url(app, &str_arg("id"));
            serde_json::json!({"ok": url.is_some(), "url": url})
        }
        "embedded-browser:close-module" => serde_json::json!({
            "ok": true,
            "closed": embedded::close_module_sessions(app, &str_arg("ownerModule"))
        }),
        _ => serde_json::json!({"ok": false, "message": "BRIDGE_CHANNEL_UNKNOWN"}),
    }
}

fn handle_connection(app: &AppHandle, mut stream: std::net::TcpStream) {
    let Some(request) = read_request(&mut stream) else {
        respond(&mut stream, 400, serde_json::json!({"ok": false, "message": "BRIDGE_REQUEST_INVALID"}));
        return;
    };

    // Execute-result delivery: fire-and-forget simple request posted by the
    // eval wrapper inside an embedded webview.  Token travels in the first
    // body line so the request stays preflight-free.
    if request.method == "POST" && request.path == "/__exec_result" {
        let body_text = String::from_utf8_lossy(&request.body).to_string();
        let mut parts = body_text.splitn(2, '\n');
        let token = parts.next().unwrap_or("");
        let json_part = parts.next().unwrap_or("");
        let expected = bridge_token();
        if token.len() == expected.len() && token == expected {
            if let Ok(payload) = serde_json::from_str::<serde_json::Value>(json_part) {
                let request_id = payload["request_id"].as_str().unwrap_or_default().to_string();
                embedded::deliver_exec_result(&request_id, payload);
            }
        }
        respond(&mut stream, 200, serde_json::json!({"ok": true}));
        return;
    }

    if request.method != "POST" || request.path != "/invoke" {
        respond(&mut stream, 404, serde_json::json!({"ok": false, "message": "NOT_FOUND"}));
        return;
    }
    let provided = request
        .headers
        .iter()
        .find(|(n, _)| n == TOKEN_HEADER)
        .map(|(_, v)| v.clone())
        .unwrap_or_default();
    let expected = bridge_token();
    let authorized = !provided.is_empty()
        && provided.len() == expected.len()
        && constant_time_eq(provided.as_bytes(), expected.as_bytes());
    if !authorized {
        respond(&mut stream, 403, serde_json::json!({"ok": false, "message": "BRIDGE_TOKEN_INVALID"}));
        return;
    }
    let payload: serde_json::Value = match serde_json::from_slice(&request.body) {
        Ok(v) => v,
        Err(_) => {
            respond(&mut stream, 400, serde_json::json!({"ok": false, "message": "BRIDGE_BODY_INVALID"}));
            return;
        }
    };
    let channel = payload["channel"].as_str().unwrap_or_default().to_string();
    if channel.is_empty() {
        respond(&mut stream, 404, serde_json::json!({"ok": false, "message": "BRIDGE_CHANNEL_UNKNOWN"}));
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

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b.iter()).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

pub fn start_embedded_browser_bridge(app: &AppHandle) {
    if bridge_port() != 0 {
        return;
    }
    let _ = APP_HANDLE.set(app.clone());
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

    let app = app.clone();
    std::thread::spawn(move || {
        for incoming in listener.incoming() {
            match incoming {
                Ok(stream) => {
                    let app = app.clone();
                    std::thread::spawn(move || handle_connection(&app, stream));
                }
                Err(_) => std::thread::sleep(std::time::Duration::from_millis(50)),
            }
        }
    });
}

pub fn stop_embedded_browser_bridge() {
    remove_state();
    BRIDGE_PORT.store(0, Ordering::SeqCst);
    *bridge_token_cell().lock().unwrap() = String::new();
}

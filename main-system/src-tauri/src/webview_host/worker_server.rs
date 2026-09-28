//! worker_server.rs — the embedded worker's token-guarded loopback server.
//!
//! Serves the session-lifecycle op set the parent shell proxies through
//! (navigate/bounds/show/hide/eval/url/ping/close) plus the preflight-free
//! ``/__result`` postback that carries eval completion values back.

use std::io::{Read, Write};
use std::time::Duration;

use tauri::Manager;

use super::worker::{self, on_main};
use super::worker_eval;

const TOKEN_HEADER: &str = "x-gptbridge-worker-token";
const MAX_BODY_BYTES: usize = 1_048_576;

pub(super) fn respond(stream: &mut std::net::TcpStream, status: u16, payload: serde_json::Value) {
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
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
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
        if let Some(pos) = raw.windows(4).position(|w| w == b"\r\n\r\n") {
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

fn authed(request: &Request) -> bool {
    let expected = worker::token_cell().lock().unwrap().clone();
    let provided = request
        .headers
        .iter()
        .find(|(n, _)| n == TOKEN_HEADER)
        .map(|(_, v)| v.clone())
        .unwrap_or_default();
    !provided.is_empty() && provided == expected
}

/// Serve one accepted connection (each on its own thread).
pub(super) fn handle_connection(mut stream: std::net::TcpStream) {
    let Some(request) = read_request(&mut stream) else {
        respond(
            &mut stream,
            400,
            serde_json::json!({"ok": false, "message": "REQUEST_INVALID"}),
        );
        return;
    };

    // Eval-result postback (preflight-free text/plain body; token in first line).
    if request.method == "POST" && request.path == "/__result" {
        let body_text = String::from_utf8_lossy(&request.body).to_string();
        let mut parts = body_text.splitn(2, '\n');
        let token = parts.next().unwrap_or("");
        let json_part = parts.next().unwrap_or("");
        let expected = worker::token_cell().lock().unwrap().clone();
        if token.len() == expected.len() && token == expected {
            if let Ok(payload) = serde_json::from_str::<serde_json::Value>(json_part) {
                let rid = payload["request_id"]
                    .as_str()
                    .unwrap_or_default()
                    .to_string();
                let tx = worker::pending_cell().lock().unwrap().remove(&rid);
                if let Some(tx) = tx {
                    let _ = tx.send(payload);
                }
            }
        }
        respond(&mut stream, 200, serde_json::json!({"ok": true}));
        return;
    }

    if !authed(&request) {
        respond(
            &mut stream,
            403,
            serde_json::json!({"ok": false, "message": "TOKEN_INVALID"}),
        );
        return;
    }

    let body: serde_json::Value =
        serde_json::from_slice(&request.body).unwrap_or_else(|_| serde_json::json!({}));

    let result = match (request.method.as_str(), request.path.as_str()) {
        ("GET", "/ping") => serde_json::json!({"ok": true}),
        ("GET", "/url") => on_main(|app| {
            let url = app
                .get_webview_window("session")
                .and_then(|w| w.url().ok().map(|u| u.to_string()))
                .unwrap_or_default();
            serde_json::json!({"ok": true, "url": url})
        }),
        ("POST", "/navigate") => {
            let url = body["url"].as_str().unwrap_or_default().to_string();
            on_main(move |app| {
                let parsed = url.parse::<tauri::Url>();
                match (parsed, app.get_webview_window("session")) {
                    (Ok(u), Some(w)) => match w.navigate(u) {
                        Ok(()) => serde_json::json!({"ok": true}),
                        Err(e) => serde_json::json!({"ok": false, "message": format!("{e}")}),
                    },
                    (Err(e), _) => {
                        serde_json::json!({"ok": false, "message": format!("INVALID_URL:{e}")})
                    }
                    (_, None) => serde_json::json!({"ok": false, "message": "WINDOW_NOT_FOUND"}),
                }
            })
        }
        ("POST", "/bounds") => {
            let x = body["x"].as_f64().unwrap_or(0.0);
            let y = body["y"].as_f64().unwrap_or(0.0);
            let w = body["width"].as_f64().unwrap_or(1.0);
            let h = body["height"].as_f64().unwrap_or(1.0);
            on_main(move |app| match app.get_webview_window("session") {
                Some(win) => {
                    let _ = win.set_position(tauri::PhysicalPosition::new(
                        x.round() as i32,
                        y.round() as i32,
                    ));
                    let _ =
                        win.set_size(tauri::PhysicalSize::new(w.round() as u32, h.round() as u32));
                    serde_json::json!({"ok": true})
                }
                None => serde_json::json!({"ok": false, "message": "WINDOW_NOT_FOUND"}),
            })
        }
        ("POST", "/show") => on_main(|app| match app.get_webview_window("session") {
            Some(w) => {
                let _ = w.show();
                let _ = w.set_focus();
                serde_json::json!({"ok": true})
            }
            None => serde_json::json!({"ok": false, "message": "WINDOW_NOT_FOUND"}),
        }),
        ("POST", "/hide") => on_main(|app| match app.get_webview_window("session") {
            Some(w) => {
                let _ = w.hide();
                serde_json::json!({"ok": true})
            }
            None => serde_json::json!({"ok": false, "message": "WINDOW_NOT_FOUND"}),
        }),
        ("POST", "/eval") => {
            let script = body["script"].as_str().unwrap_or_default().to_string();
            worker_eval::eval_script(&script)
        }
        ("POST", "/reload") => worker_eval::eval_script("location.reload()"),
        ("POST", "/back") => {
            let can = {
                let nav = worker::nav_state().lock().unwrap();
                nav.cursor > 0
            };
            if !can {
                serde_json::json!({"ok": false, "message": "NAVIGATION_NOT_AVAILABLE"})
            } else {
                let result = worker_eval::eval_script("history.back()");
                if result["ok"] == serde_json::Value::Bool(true) {
                    let url = {
                        let mut nav = worker::nav_state().lock().unwrap();
                        nav.cursor -= 1;
                        nav.history.get(nav.cursor).cloned().unwrap_or_default()
                    };
                    worker::push_event("navigate", &url, serde_json::json!({"url": url}));
                    serde_json::json!({"ok": true})
                } else {
                    result
                }
            }
        }
        ("POST", "/forward") => {
            let can = {
                let nav = worker::nav_state().lock().unwrap();
                nav.cursor + 1 < nav.history.len()
            };
            if !can {
                serde_json::json!({"ok": false, "message": "NAVIGATION_NOT_AVAILABLE"})
            } else {
                let result = worker_eval::eval_script("history.forward()");
                if result["ok"] == serde_json::Value::Bool(true) {
                    let url = {
                        let mut nav = worker::nav_state().lock().unwrap();
                        nav.cursor += 1;
                        nav.history.get(nav.cursor).cloned().unwrap_or_default()
                    };
                    worker::push_event("navigate", &url, serde_json::json!({"url": url}));
                    serde_json::json!({"ok": true})
                } else {
                    result
                }
            }
        }
        ("GET", "/state") => {
            let url = on_main(|app| {
                serde_json::json!({
                    "url": app
                        .get_webview_window("session")
                        .and_then(|w| w.url().ok().map(|u| u.to_string()))
                        .unwrap_or_default(),
                })
            })
            .get("url")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
            let (loading, can_back, can_forward) = {
                let nav = worker::nav_state().lock().unwrap();
                (
                    nav.loading,
                    nav.cursor > 0,
                    nav.cursor + 1 < nav.history.len(),
                )
            };
            serde_json::json!({
                "ok": true,
                "url": url,
                "title": worker::title_cell().lock().unwrap().clone(),
                "loading": loading,
                "canGoBack": can_back,
                "canGoForward": can_forward,
            })
        }
        ("POST", "/close") => {
            respond(&mut stream, 200, serde_json::json!({"ok": true}));
            std::thread::spawn(|| {
                std::thread::sleep(Duration::from_millis(150));
                std::process::exit(0);
            });
            return;
        }
        _ => {
            respond(
                &mut stream,
                404,
                serde_json::json!({"ok": false, "message": "NOT_FOUND"}),
            );
            return;
        }
    };
    respond(&mut stream, 200, result);
}

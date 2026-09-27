//! embedded_worker.rs — dedicated helper process hosting ONE embedded
//! browser session.
//!
//! Why a separate process: this machine's WebView2 runtime wedges any host
//! → controller call once a process owns more than one controller (post-loop
//! creation deadlocks in EBW.dll; even pooled first-N-controller ops stall
//! 5–60 s).  A dedicated process owns exactly one controller — the reliable
//! first-controller path — so each session spawns
//! ``gptbridge-shell.exe --embedded-worker``.
//!
//! The worker creates a frameless ``WebviewWindow``, reparents it under the
//! main window's HWND via ``SetParent`` (WS_CHILD: clipped to the parent
//! client area, moves and hides with it — BrowserView-equivalent
//! containment), then serves a token-guarded loopback endpoint the parent
//! shell proxies session operations through.
//!
//! Exit contract: the worker exits when the parent PID dies, when it
//! receives ``POST /close``, or when its state file is deleted.

use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::Mutex;
use std::sync::OnceLock;
use std::time::Duration;

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

const TOKEN_HEADER: &str = "x-gptbridge-worker-token";
const MAX_BODY_BYTES: usize = 1_048_576;

pub struct WorkerArgs {
    pub session_id: String,
    pub url: String,
    pub parent_hwnd: isize,
    pub parent_pid: u32,
    pub token: String,
    pub state_file: std::path::PathBuf,
}

/// Parse ``--embedded-worker`` argv.  Returns ``None`` when the flag is not
/// present (normal shell mode).
pub fn worker_args() -> Option<WorkerArgs> {
    let args: Vec<String> = std::env::args().collect();
    if !args.iter().any(|a| a == "--embedded-worker") {
        return None;
    }
    let get = |key: &str| -> String {
        args.iter()
            .position(|a| a == key)
            .and_then(|i| args.get(i + 1))
            .cloned()
            .unwrap_or_default()
    };
    Some(WorkerArgs {
        session_id: get("--session-id"),
        url: get("--url"),
        parent_hwnd: get("--parent-hwnd")
            .trim_start_matches("0x")
            .trim_start_matches("0X")
            .parse::<isize>()
            .or_else(|_| {
                isize::from_str_radix(
                    &get("--parent-hwnd")
                        .trim_start_matches("0x")
                        .trim_start_matches("0X"),
                    16,
                )
            })
            .unwrap_or(0),
        parent_pid: get("--parent-pid").parse::<u32>().unwrap_or(0),
        token: get("--token"),
        state_file: std::path::PathBuf::from(get("--state-file")),
    })
}

fn token_cell() -> &'static Mutex<String> {
    static TOKEN: OnceLock<Mutex<String>> = OnceLock::new();
    TOKEN.get_or_init(|| Mutex::new(String::new()))
}

fn pending_cell() -> &'static Mutex<std::collections::HashMap<String, std::sync::mpsc::Sender<serde_json::Value>>> {
    static P: OnceLock<Mutex<std::collections::HashMap<String, std::sync::mpsc::Sender<serde_json::Value>>>> =
        OnceLock::new();
    P.get_or_init(|| Mutex::new(std::collections::HashMap::new()))
}

fn app_handle() -> &'static Mutex<Option<tauri::AppHandle>> {
    static H: OnceLock<Mutex<Option<tauri::AppHandle>>> = OnceLock::new();
    H.get_or_init(|| Mutex::new(None))
}

fn publish_state(path: &std::path::Path, port: u16) {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let body = serde_json::json!({
        "host": "127.0.0.1",
        "port": port,
        "pid": std::process::id(),
    });
    let _ = std::fs::write(path, serde_json::to_string_pretty(&body).unwrap_or_default());
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
        .set_read_timeout(Some(Duration::from_secs(5)))
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
    let expected = token_cell().lock().unwrap().clone();
    let provided = request
        .headers
        .iter()
        .find(|(n, _)| n == TOKEN_HEADER)
        .map(|(_, v)| v.clone())
        .unwrap_or_default();
    !provided.is_empty() && provided == expected
}

fn handle_connection(mut stream: std::net::TcpStream) {
    let Some(request) = read_request(&mut stream) else {
        respond(&mut stream, 400, serde_json::json!({"ok": false, "message": "REQUEST_INVALID"}));
        return;
    };

    // Eval-result postback (preflight-free text/plain body; token in first line).
    if request.method == "POST" && request.path == "/__result" {
        let body_text = String::from_utf8_lossy(&request.body).to_string();
        let mut parts = body_text.splitn(2, '\n');
        let token = parts.next().unwrap_or("");
        let json_part = parts.next().unwrap_or("");
        let expected = token_cell().lock().unwrap().clone();
        if token.len() == expected.len() && token == expected {
            if let Ok(payload) = serde_json::from_str::<serde_json::Value>(json_part) {
                let rid = payload["request_id"].as_str().unwrap_or_default().to_string();
                let tx = pending_cell().lock().unwrap().remove(&rid);
                if let Some(tx) = tx {
                    let _ = tx.send(payload);
                }
            }
        }
        respond(&mut stream, 200, serde_json::json!({"ok": true}));
        return;
    }

    if !authed(&request) {
        respond(&mut stream, 403, serde_json::json!({"ok": false, "message": "TOKEN_INVALID"}));
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
                    (Err(e), _) => serde_json::json!({"ok": false, "message": format!("INVALID_URL:{e}")}),
                    (_, None) => serde_json::json!({"ok": false, "message": "WINDOW_NOT_FOUND"}),
                }
            })
        }
        ("POST", "/bounds") => {
            let x = body["x"].as_f64().unwrap_or(0.0);
            let y = body["y"].as_f64().unwrap_or(0.0);
            let w = body["width"].as_f64().unwrap_or(1.0);
            let h = body["height"].as_f64().unwrap_or(1.0);
            on_main(move |app| {
                match app.get_webview_window("session") {
                    Some(win) => {
                        let _ = win.set_position(tauri::PhysicalPosition::new(
                            x.round() as i32,
                            y.round() as i32,
                        ));
                        let _ = win.set_size(tauri::PhysicalSize::new(
                            w.round() as u32,
                            h.round() as u32,
                        ));
                        serde_json::json!({"ok": true})
                    }
                    None => serde_json::json!({"ok": false, "message": "WINDOW_NOT_FOUND"}),
                }
            })
        }
        ("POST", "/show") => on_main(|app| {
            match app.get_webview_window("session") {
                Some(w) => {
                    let _ = w.show();
                    let _ = w.set_focus();
                    serde_json::json!({"ok": true})
                }
                None => serde_json::json!({"ok": false, "message": "WINDOW_NOT_FOUND"}),
            }
        }),
        ("POST", "/hide") => on_main(|app| {
            match app.get_webview_window("session") {
                Some(w) => {
                    let _ = w.hide();
                    serde_json::json!({"ok": true})
                }
                None => serde_json::json!({"ok": false, "message": "WINDOW_NOT_FOUND"}),
            }
        }),
        ("POST", "/eval") => {
            let script = body["script"].as_str().unwrap_or_default().to_string();
            eval_script(&script)
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
            respond(&mut stream, 404, serde_json::json!({"ok": false, "message": "NOT_FOUND"}));
            return;
        }
    };
    respond(&mut stream, 200, result);
}

/// Dispatch a closure to the worker's own event loop and wait (bounded) for
/// its JSON result.  First-controller ops on the worker's loop are reliable.
fn on_main(
    f: impl FnOnce(&tauri::AppHandle) -> serde_json::Value + Send + 'static,
) -> serde_json::Value {
    let app = match app_handle().lock().unwrap().clone() {
        Some(a) => a,
        None => return serde_json::json!({"ok": false, "message": "APP_NOT_READY"}),
    };
    let (tx, rx) = std::sync::mpsc::channel();
    let app2 = app.clone();
    if app
        .run_on_main_thread(move || {
            let _ = tx.send(f(&app2));
        })
        .is_err()
    {
        return serde_json::json!({"ok": false, "message": "DISPATCH_FAILED"});
    }
    rx.recv_timeout(Duration::from_secs(15))
        .unwrap_or_else(|_| serde_json::json!({"ok": false, "message": "OP_TIMEOUT"}))
}

fn eval_script(script: &str) -> serde_json::Value {
    let request_id = format!("eval-{}", std::process::id());
    let (tx, rx) = std::sync::mpsc::channel::<serde_json::Value>();
    pending_cell()
        .lock()
        .unwrap()
        .insert(request_id.clone(), tx);
    let port = worker_port();
    let token = token_cell().lock().unwrap().clone();
    let wrapper = format!(
        "Promise.resolve().then(function(){{return (function(){{ {script} }})();}}).then(function(r){{try{{var x=new XMLHttpRequest();x.open('POST','http://127.0.0.1:{port}/__result',true);x.setRequestHeader('Content-Type','text/plain');x.send({token_json}+'\\n'+JSON.stringify({{request_id:{rid_json},result:r===undefined?null:r}}));}}catch(e){{}}}}).catch(function(e){{try{{var x=new XMLHttpRequest();x.open('POST','http://127.0.0.1:{port}/__result',true);x.setRequestHeader('Content-Type','text/plain');x.send({token_json}+'\\n'+JSON.stringify({{request_id:{rid_json},error:String(e)}}));}}catch(e2){{}}}});",
        script = script,
        port = port,
        token_json = serde_json::to_string(&token).unwrap_or_default(),
        rid_json = serde_json::to_string(&request_id).unwrap_or_default(),
    );
    let dispatched = on_main(move |app| {
        match app.get_webview_window("session") {
            Some(w) => match w.eval(&wrapper) {
                Ok(()) => serde_json::json!({"ok": true}),
                Err(e) => serde_json::json!({"ok": false, "message": format!("EVAL_FAILED:{e}")}),
            },
            None => serde_json::json!({"ok": false, "message": "WINDOW_NOT_FOUND"}),
        }
    });
    if dispatched.get("ok") != Some(&serde_json::Value::Bool(true)) {
        pending_cell().lock().unwrap().remove(&request_id);
        return dispatched;
    }
    match rx.recv_timeout(Duration::from_secs(25)) {
        Ok(payload) => {
            if let Some(error) = payload.get("error") {
                serde_json::json!({"ok": false, "message": error.as_str().unwrap_or("EVAL_ERROR")})
            } else {
                serde_json::json!({"ok": true, "result": payload.get("result").cloned().unwrap_or(serde_json::Value::Null)})
            }
        }
        Err(_) => serde_json::json!({"ok": false, "message": "EVAL_TIMEOUT"}),
    }
}

fn worker_port() -> u16 {
    WORKER_PORT.load(std::sync::atomic::Ordering::SeqCst)
}

static WORKER_PORT: std::sync::atomic::AtomicU16 = std::sync::atomic::AtomicU16::new(0);

/// Parent-death watchdog: exit when the parent shell's PID disappears so a
/// crashed parent never leaks worker/webview process trees.
fn start_parent_watchdog(parent_pid: u32) {
    if parent_pid == 0 {
        return;
    }
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(2));
        if !parent_alive(parent_pid) {
            eprintln!("[embedded-worker] PARENT_GONE {parent_pid}");
            std::process::exit(0);
        }
    });
}

#[cfg(windows)]
fn parent_alive(pid: u32) -> bool {
    use windows_sys::Win32::System::Threading::{GetExitCodeProcess, OpenProcess};
    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    const STILL_ACTIVE: i32 = 259;
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle == std::ptr::null_mut() {
            return false;
        }
        let mut code: u32 = 0;
        let ok = GetExitCodeProcess(handle, &mut code);
        windows_sys::Win32::Foundation::CloseHandle(handle);
        ok != 0 && code as i32 == STILL_ACTIVE
    }
}

#[cfg(not(windows))]
fn parent_alive(_pid: u32) -> bool {
    true
}

/// The parent's raw HWND for ``parent_raw`` — a child window is confined
/// to the parent client area (WS_CHILD — BrowserView-equivalent
/// containment).  Cross-process parenting is legal on Windows.
#[cfg(windows)]
fn parent_handle(raw: isize) -> Option<windows::Win32::Foundation::HWND> {
    if raw == 0 {
        None
    } else {
        Some(windows::Win32::Foundation::HWND(raw as *mut std::ffi::c_void))
    }
}

/// Entry point for ``--embedded-worker`` mode.  Owns the process forever;
/// never returns to the normal shell path.
pub fn run(args: WorkerArgs) -> i32 {
    *token_cell().lock().unwrap() = args.token.clone();
    start_parent_watchdog(args.parent_pid);

    // Loopback listener first so the parent can poll the state file as soon
    // as it lands — the webview may still be warming up.
    let listener = match TcpListener::bind("127.0.0.1:0") {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[embedded-worker] LISTENER_BIND_FAILED {e}");
            return 2;
        }
    };
    let port = listener
        .local_addr()
        .map(|a| a.port())
        .unwrap_or_default();
    WORKER_PORT.store(port, std::sync::atomic::Ordering::SeqCst);
    publish_state(&args.state_file, port);
    std::thread::spawn(move || {
        for incoming in listener.incoming() {
            match incoming {
                Ok(stream) => {
                    std::thread::spawn(move || handle_connection(stream));
                }
                Err(_) => std::thread::sleep(Duration::from_millis(50)),
            }
        }
    });

    let url = args.url.clone();
    let parent_hwnd = args.parent_hwnd;
    let app = tauri::Builder::default()
        .setup(move |app| {
            let parsed: tauri::Url = url
                .parse()
                .unwrap_or_else(|_| "about:blank".parse().unwrap());
            let mut builder = WebviewWindowBuilder::new(app, "session", WebviewUrl::External(parsed))
                .title("embedded-browser")
                .visible(false)
                .decorations(false)
                .resizable(false)
                .minimizable(false)
                .maximizable(false)
                .skip_taskbar(true)
                .shadow(false)
                .focused(false)
                .always_on_top(false)
                .inner_size(1.0, 1.0)
                .position(0.0, 0.0);
            #[cfg(windows)]
            {
                if let Some(parent) = parent_handle(parent_hwnd) {
                    builder = builder.parent_raw(parent);
                }
            }
            builder.build()?;
            *app_handle().lock().unwrap() = Some(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!());

    match app {
        Ok(app) => {
            app.run(|_app, _event| {});
            0
        }
        Err(e) => {
            eprintln!("[embedded-worker] TAURI_BUILD_FAILED {e}");
            3
        }
    }
}

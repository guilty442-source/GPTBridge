//! gptbridge-backend — governed backend host.
//!
//! Native owner of the loopback IPC gateway (port 8765 default),
//! runtime readiness gate, and tool lifecycle commands.  Successor to
//! the retired Python ``boot_core``/``main.py`` chain: single resident
//! process, bounded connection pool, fail-closed auth on every channel.

mod auth;
mod health;
mod tools;

use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use gptbridge_core::ipc::ws_server::{
    read_http_request, write_http_response, write_ws_accept, ServerEvent, ServerSocket,
};

const DEFAULT_PORT: u16 = 8765;
const MAX_WS_CONNECTIONS: u64 = 32;
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(5);
const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(20);

static ACTIVE_CONNECTIONS: AtomicU64 = AtomicU64::new(0);

fn ipc_port() -> u16 {
    std::env::var("GPTBRIDGE_IPC_PORT")
        .ok()
        .and_then(|v| v.trim().parse::<u16>().ok())
        .filter(|p| (1024..=65535).contains(p))
        .unwrap_or(DEFAULT_PORT)
}

fn serve_health(stream: &mut TcpStream, target: &str) {
    let level = match target.split_once('?').map(|(_, q)| q) {
        Some("brief=1") | Some("level=brief") => "brief",
        Some("level=deep") => "deep",
        _ => "full",
    };
    let payload = health::health_payload(level);
    let ready = payload["ok"].as_bool().unwrap_or(false);
    let (status, reason) = if ready { (200, "OK") } else { (503, "STARTING") };
    let _ = write_http_response(
        stream,
        status,
        reason,
        "application/json",
        payload.to_string().as_bytes(),
    );
}

fn dispatch_command(command: &str, payload: &Value) -> Value {
    match command {
        "toolbox_list_tools" => tools::list_tools(),
        "toolbox_start_tool" => {
            tools::start_tool(payload["tool_id"].as_str().unwrap_or_default())
        }
        "toolbox_stop_tool" => {
            tools::stop_tool(payload["tool_id"].as_str().unwrap_or_default())
        }
        "toolbox_force_close_tool" => {
            tools::force_close_tool(payload["tool_id"].as_str().unwrap_or_default())
        }
        "app:get-runtime-status" => {
            let r = health::evaluate();
            json!({
                "ok": r.ok,
                "backend": r.runtime_state,
                "version": gptbridge_core::app::PRODUCT_VERSION,
                "runtime_scope": "main",
                "governance_ready": r.governance_ready,
                "backend_runtime_ready": r.backend_runtime_ready,
                "dependencies_ready": r.dependencies_ready,
                "authenticated_ipc_connected": r.authenticated_ipc,
                "dependencies": r.dependencies,
            })
        }
        "heartbeat_pong" | "state_event_hello" => json!({"ok": true}),
        _ => json!({
            "ok": false,
            "error": format!("COMMAND_UNKNOWN:{command}"),
        }),
    }
}

fn handle_connection(mut stream: TcpStream) {
    // HTTP request head — shared by /health, /shutdown and WS upgrade.
    let Some(request) = read_http_request(&mut stream) else {
        return;
    };
    match request.path() {
        "/health" => {
            serve_health(&mut stream, &request.target.clone());
            return;
        }
        "/shutdown" => {
            if auth::authorize_shutdown(request.header("x-gptbridge-shutdown-token")) {
                let _ = write_http_response(&mut stream, 200, "OK", "text/plain", b"OK");
                std::process::exit(0);
            }
            let _ = write_http_response(&mut stream, 403, "FORBIDDEN", "text/plain", b"Forbidden");
            return;
        }
        _ => {}
    }

    // WebSocket upgrade path.
    if request
        .header("upgrade")
        .map(|v| v.eq_ignore_ascii_case("websocket"))
        != Some(true)
        || request.header("sec-websocket-key").is_none()
    {
        let _ = write_http_response(&mut stream, 400, "BAD REQUEST", "text/plain", b"Bad Request");
        return;
    }
    if ACTIVE_CONNECTIONS.load(Ordering::SeqCst) >= MAX_WS_CONNECTIONS {
        let _ = write_http_response(
            &mut stream,
            503,
            "SERVICE_UNAVAILABLE",
            "text/plain",
            b"capacity-exhausted",
        );
        return;
    }
    if !auth::authorize_websocket(&request.query()) {
        eprintln!("[backend] ws auth rejected: {}", request.target);
        let _ = write_http_response(&mut stream, 403, "FORBIDDEN", "text/plain", b"Forbidden");
        return;
    }
    eprintln!("[backend] ws auth accepted: {}", request.path());
    if write_ws_accept(&mut stream, &request).is_err() {
        return;
    }
    let Some(socket) = ServerSocket::new(stream) else {
        return;
    };
    ACTIVE_CONNECTIONS.fetch_add(1, Ordering::SeqCst);
    health::note_authenticated_connect();

    // Immediate compact status push — the renderer never polls.
    let mut status = health::health_payload("brief");
    status["push"] = json!(true);
    status["immediate"] = json!(true);
    let _ = socket.send(&json!({"event": "runtime_status_push", "payload": status}));

    // Command loop: the socket's reader thread feeds events; this loop
    // answers commands and drives the application heartbeat — ping every
    // HEARTBEAT_INTERVAL, close when the peer stays silent beyond
    // HEARTBEAT_TIMEOUT (any inbound frame counts as liveness).
    let mut last_seen = Instant::now();
    let mut next_ping = Instant::now() + HEARTBEAT_INTERVAL;
    loop {
        let now = Instant::now();
        if now >= next_ping {
            next_ping = now + HEARTBEAT_INTERVAL;
            if !socket.send(&json!({"event": "heartbeat_ping", "payload": {}})) {
                break;
            }
        }
        if last_seen.elapsed() > HEARTBEAT_TIMEOUT {
            break;
        }
        match socket.events().recv_timeout(Duration::from_millis(500)) {
            Ok(ServerEvent::Message(msg)) => {
                last_seen = Instant::now();
                let command = msg["command"].as_str().unwrap_or_default().to_string();
                if command.is_empty() {
                    continue;
                }
                let result = dispatch_command(&command, &msg["payload"]);
                let event = format!("{command}_result");
                if !socket.send(&json!({"event": event, "payload": result})) {
                    break;
                }
            }
            Ok(ServerEvent::Closed) => break,
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }
    drop(socket);
    ACTIVE_CONNECTIONS.fetch_sub(1, Ordering::SeqCst);
    health::note_authenticated_disconnect();
}

fn write_state_file() {
    let root = &gptbridge_core::native::paths::path_library().workspace_root;
    let state_dir = root.join("main-system").join("runtime").join("state");
    let _ = std::fs::create_dir_all(&state_dir);
    let state = json!({
        "role": "backend-native",
        "status": "serving",
        "pid": std::process::id(),
        "active_backend_port": ipc_port(),
        "updated_at": format!("{:?}", std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH).unwrap_or_default()),
    });
    let tmp = state_dir.join("boot-core.json.tmp");
    if std::fs::write(&tmp, state.to_string()).is_ok() {
        let _ = std::fs::rename(&tmp, state_dir.join("boot-core.json"));
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if !args.iter().any(|a| a == "--serve") {
        eprintln!("usage: gptbridge-backend --serve [--auto-kill-backend-port]");
        std::process::exit(2);
    }
    let port = ipc_port();
    let listener = match TcpListener::bind(("127.0.0.1", port)) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("gptbridge-backend: bind 127.0.0.1:{port} failed: {e}");
            std::process::exit(1);
        }
    };
    write_state_file();
    println!("gptbridge-backend listening on 127.0.0.1:{port}");
    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let _ = s.set_read_timeout(Some(Duration::from_secs(30)));
                thread::spawn(move || handle_connection(s));
            }
            Err(_) => continue,
        }
    }
}

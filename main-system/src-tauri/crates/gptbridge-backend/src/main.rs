//! gptbridge-backend — governed backend host.
//!
//! Native owner of the loopback IPC gateway (port 8765 default),
//! runtime readiness gate, and tool lifecycle commands.  Successor to
//! the retired Python ``boot_core``/``main.py`` chain: single resident
//! process, bounded connection pool, fail-closed auth on every channel.

mod audit;
mod auth;
mod channel_host;
mod fault;
mod health;
mod outbox;
mod pg;
mod resource_mode;
mod saga;
mod tools;

use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};
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
const STATUS_EVAL_INTERVAL: Duration = Duration::from_secs(2);

static ACTIVE_CONNECTIONS: AtomicU64 = AtomicU64::new(0);
static NEXT_CONNECTION: AtomicU64 = AtomicU64::new(1);

/// A195 outbox publisher — lazily constructed on the first
/// ``state_event_hello``; ``None`` when the governed PostgreSQL DSN is
/// unavailable (parity with the Python publisher never being created).
static OUTBOX: OnceLock<Option<Arc<outbox::OutboxHub>>> = OnceLock::new();

fn outbox_hub() -> Option<&'static Arc<outbox::OutboxHub>> {
    OUTBOX.get_or_init(outbox::try_new).as_ref()
}

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
                "maintenance_ready": r.ok,
                "governance_ready": r.governance_ready,
                "backend_runtime_ready": r.backend_runtime_ready,
                "dependencies_ready": r.dependencies_ready,
                "authenticated_ipc_connected": r.authenticated_ipc,
                "startup_dead": r.startup_dead,
                "startup_failures": r.startup_failures,
                "dependencies": r.dependencies,
            })
        }
        "app:get-resource-mode" => resource_mode::get(payload),
        "app:set-resource-mode" => resource_mode::set(payload),
        "app:get-saga-operations" | "app:get-saga-operation" => {
            saga::handle(command, payload)
        }
        "app:get-fault-analysis" => fault::handle(payload),
        _ => json!({
            "ok": false,
            "error": format!("COMMAND_UNKNOWN:{command}"),
        }),
    }
}

/// In-band session commands — ordered against event delivery, exactly like
/// the retired Python ``_websocket_session`` loop.  Returns ``Some(event)``
/// when a frame must go out on the socket; ``None`` means consumed silently.
fn session_command(
    conn: u64,
    socket: &ServerSocket,
    command: &str,
    payload: &Value,
) -> Option<Value> {
    match command {
        // Frontend replies to heartbeat_ping; liveness is any inbound frame.
        "heartbeat_pong" => None,
        // A195 outbox control channel.
        "state_event_hello" => outbox_hub().map(|hub| {
            let hello = hub.handle_hello(
                conn,
                &socket.writer(),
                &payload["cursor"],
                &payload["generation"],
            );
            json!({"event": "state_event_session", "payload": hello})
        }),
        "state_event_ack" => {
            if let Some(hub) = outbox_hub() {
                hub.handle_ack(conn, &payload["cursor"]);
            }
            None
        }
        "state_event_resync" => outbox_hub().map(|hub| {
            match hub.handle_resync(conn, &payload["cursor"]) {
                Some(result) => json!({
                    "event": "state_event_resync_result",
                    "payload": result,
                }),
                None => json!({
                    "event": "state_event_session",
                    "payload": hub.handle_hello(conn, &socket.writer(), &json!(0), &json!("")),
                }),
            }
        }),
        _ => Some({
            let result = dispatch_command(command, payload);
            json!({"event": format!("{command}_result"), "payload": result})
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
                channel_host::stop();
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
        // Path only — the target carries the credential query string.
        eprintln!("[backend] ws auth rejected: {}", request.path());
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
    let conn = NEXT_CONNECTION.fetch_add(1, Ordering::SeqCst);
    ACTIVE_CONNECTIONS.fetch_add(1, Ordering::SeqCst);
    health::note_authenticated_connect();

    // Immediate compact status push — the renderer never polls.
    let mut status = health::health_payload("brief");
    status["push"] = json!(true);
    status["immediate"] = json!(true);
    let _ = socket.send(&json!({"event": "runtime_status_push", "payload": status}));
    let mut next_status_eval = Instant::now() + STATUS_EVAL_INTERVAL;

    // Parity with the Python session loop: a latched-dead startup enters
    // degraded mode so the client stays connected and can observe status.
    if health::startup_dead() {
        let readiness = health::evaluate();
        let _ = socket.send(&json!({
            "event": "runtime_degraded",
            "payload": {
                "ok": false,
                "runtime_state": "degraded",
                "startup_failures": readiness.startup_failures,
            },
        }));
    }

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
        // Retired-Python parity: the backend re-pushes a status report on
        // every cycle — the renderer never polls, so a lost or stale push
        // must self-heal; change-detection alone leaves a client that
        // missed the immediate push stuck in Synchronizing forever.
        if now >= next_status_eval {
            next_status_eval = now + STATUS_EVAL_INTERVAL;
            let current = health::health_payload("brief");
            if !socket
                .send(&json!({"event": "runtime_status_push", "payload": current}))
            {
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
                if let Some(frame) = session_command(conn, &socket, &command, &msg["payload"]) {
                    if !socket.send(&frame) {
                        break;
                    }
                }
            }
            Ok(ServerEvent::Closed) => break,
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }
    if let Some(hub) = OUTBOX.get().and_then(|o| o.as_ref()) {
        hub.unregister(conn);
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
        "updated_at": std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64(),
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
    // Channel-layer automation: spawn + supervise the governed
    // shared-layer channel host (shared-layer/manifest.json
    // ``background_service`` contract, managed_by=main-system).
    channel_host::start(port);
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

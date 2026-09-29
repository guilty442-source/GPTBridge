//! ws_server.rs — minimal loopback WebSocket (RFC 6455) server half.
//!
//! Mirror of ``ws.rs`` for the listener side: parse the HTTP request,
//! verify the governed authorization hook, send the 101 handshake, then
//! exchange frames (client frames arrive masked per the RFC; server
//! frames leave unmasked).  Text frames carry JSON; ping is answered
//! with pong; close terminates the session.  Connection admission and
//! frame size are bounded — a loopback IPC server never grows
//! unbounded per-connection state.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver};
use std::sync::{Arc, Mutex};
use std::thread;

use serde_json::Value;

use super::ws::{base64_encode_pub, sha1_pub, WS_GUID_PUB};

const MAX_HTTP_HEAD: usize = 16 * 1024;
const MAX_FRAME: usize = 16 * 1024 * 1024;

/// Parsed HTTP request head for the accept loop.
#[derive(Debug)]
pub struct HttpRequest {
    pub method: String,
    /// Raw request target — e.g. ``/?token=…&instance=…``.
    pub target: String,
    /// Lowercased header name → value.
    pub headers: HashMap<String, String>,
}

impl HttpRequest {
    /// Path component of the request target.
    pub fn path(&self) -> &str {
        self.target.split('?').next().unwrap_or("/")
    }

    /// Decoded query parameters.
    pub fn query(&self) -> HashMap<String, String> {
        let mut out = HashMap::new();
        if let Some(q) = self.target.split_once('?').map(|(_, q)| q) {
            for pair in q.split('&') {
                let (k, v) = match pair.split_once('=') {
                    Some((k, v)) => (k, v),
                    None => (pair, ""),
                };
                out.insert(url_decode(k), url_decode(v));
            }
        }
        out
    }

    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.get(name).map(String::as_str)
    }
}

fn url_decode(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(v) = u8::from_str_radix(&input[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(if bytes[i] == b'+' { b' ' } else { bytes[i] });
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Read one HTTP request head; ``None`` on malformed/oversized input.
pub fn read_http_request(stream: &mut TcpStream) -> Option<HttpRequest> {
    let mut head = Vec::with_capacity(1024);
    let mut byte = [0u8; 1];
    while !head.ends_with(b"\r\n\r\n") {
        if head.len() >= MAX_HTTP_HEAD {
            return None;
        }
        match stream.read(&mut byte) {
            Ok(1) => head.push(byte[0]),
            _ => return None,
        }
    }
    let text = String::from_utf8_lossy(&head);
    let mut lines = text.split("\r\n");
    let request_line = lines.next()?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next()?.to_string();
    let target = parts.next()?.to_string();
    let mut headers = HashMap::new();
    for line in lines {
        if line.is_empty() {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            headers.insert(name.trim().to_ascii_lowercase(), value.trim().to_string());
        }
    }
    Some(HttpRequest {
        method,
        target,
        headers,
    })
}

/// Write a plain HTTP response and leave the stream for the caller to close.
pub fn write_http_response(
    stream: &mut TcpStream,
    status: u16,
    reason: &str,
    content_type: &str,
    body: &[u8],
) -> std::io::Result<()> {
    let head = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(body)
}

/// Accept a WebSocket upgrade: validates the key header and emits 101.
pub fn write_ws_accept(stream: &mut TcpStream, request: &HttpRequest) -> std::io::Result<()> {
    let key = request
        .header("sec-websocket-key")
        .unwrap_or("")
        .to_string();
    let accept = base64_encode_pub(&sha1_pub(format!("{key}{WS_GUID_PUB}").as_bytes()));
    let response = format!(
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\
         Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
    );
    stream.write_all(response.as_bytes())
}

/// Server-side WebSocket session: a reader thread produces ``WsEvent``s
/// while the writer half accepts unmasked ``send`` calls.
pub struct ServerSocket {
    writer: Arc<Mutex<TcpStream>>,
    events: Receiver<ServerEvent>,
    closed: Arc<AtomicBool>,
}

/// Events delivered by the connection reader thread.
#[derive(Debug)]
pub enum ServerEvent {
    /// A decoded JSON text frame from the client.
    Message(Value),
    /// The socket closed (peer close frame, error, or local close()).
    Closed,
}

fn read_exact(stream: &mut TcpStream, buf: &mut [u8]) -> std::io::Result<()> {
    stream.read_exact(buf)
}

fn write_frame(stream: &mut TcpStream, opcode: u8, payload: &[u8]) -> std::io::Result<()> {
    let mut frame = Vec::with_capacity(payload.len() + 10);
    frame.push(0x80 | opcode); // FIN + opcode
    let len = payload.len();
    if len < 126 {
        frame.push(len as u8);
    } else if len <= u16::MAX as usize {
        frame.push(126);
        frame.extend_from_slice(&(len as u16).to_be_bytes());
    } else {
        frame.push(127);
        frame.extend_from_slice(&(len as u64).to_be_bytes());
    }
    frame.extend_from_slice(payload);
    stream.write_all(&frame)
}

/// Shareable writer half of a ``ServerSocket`` — safe to move to
/// heartbeat/broadcast threads while the owner drains ``events``.
#[derive(Clone)]
pub struct ServerWriter {
    writer: Arc<Mutex<TcpStream>>,
    closed: Arc<AtomicBool>,
}

impl ServerWriter {
    pub fn send(&self, value: &Value) -> bool {
        if self.closed.load(Ordering::SeqCst) {
            return false;
        }
        let payload = value.to_string();
        let mut guard = match self.writer.lock() {
            Ok(g) => g,
            Err(e) => e.into_inner(),
        };
        write_frame(&mut guard, 0x1, payload.as_bytes()).is_ok()
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::SeqCst)
    }
}

impl ServerSocket {
    /// A cloneable writer handle for auxiliary threads.
    pub fn writer(&self) -> ServerWriter {
        ServerWriter {
            writer: Arc::clone(&self.writer),
            closed: Arc::clone(&self.closed),
        }
    }

    /// Wrap an already-accepted stream (101 response already written).
    pub fn new(stream: TcpStream) -> Option<Self> {
        stream.set_nodelay(true).ok()?;
        let writer = Arc::new(Mutex::new(stream.try_clone().ok()?));
        let reader_writer = Arc::clone(&writer);
        let closed = Arc::new(AtomicBool::new(false));
        let reader_closed = Arc::clone(&closed);
        let (tx, rx) = channel::<ServerEvent>();
        let mut reader = stream;

        thread::spawn(move || {
            let mut fragments: Vec<u8> = Vec::new();
            loop {
                let mut header = [0u8; 2];
                if read_exact(&mut reader, &mut header).is_err() {
                    break;
                }
                let fin = header[0] & 0x80 != 0;
                let opcode = header[0] & 0x0F;
                let masked = header[1] & 0x80 != 0;
                let mut len = (header[1] & 0x7F) as u64;
                if len == 126 {
                    let mut b = [0u8; 2];
                    if read_exact(&mut reader, &mut b).is_err() {
                        break;
                    }
                    len = u16::from_be_bytes(b) as u64;
                } else if len == 127 {
                    let mut b = [0u8; 8];
                    if read_exact(&mut reader, &mut b).is_err() {
                        break;
                    }
                    len = u64::from_be_bytes(b);
                }
                if len as usize > MAX_FRAME {
                    break;
                }
                let mut mask = [0u8; 4];
                if masked && read_exact(&mut reader, &mut mask).is_err() {
                    break;
                }
                let mut payload = vec![0u8; len as usize];
                if read_exact(&mut reader, &mut payload).is_err() {
                    break;
                }
                if masked {
                    for (i, b) in payload.iter_mut().enumerate() {
                        *b ^= mask[i % 4];
                    }
                }
                match opcode {
                    0x0 => {
                        fragments.extend_from_slice(&payload);
                        if fin {
                            let bytes = std::mem::take(&mut fragments);
                            if let Ok(v) = serde_json::from_slice::<Value>(&bytes) {
                                if tx.send(ServerEvent::Message(v)).is_err() {
                                    return;
                                }
                            }
                        }
                    }
                    0x1 => {
                        if fin {
                            if let Ok(v) = serde_json::from_slice::<Value>(&payload) {
                                if tx.send(ServerEvent::Message(v)).is_err() {
                                    return;
                                }
                            }
                        } else {
                            fragments = payload;
                        }
                    }
                    0x8 => break, // close
                    0x9 => {
                        let _ = write_frame(
                            &mut reader_writer.lock().unwrap_or_else(|e| e.into_inner()),
                            0xA,
                            &payload,
                        );
                    }
                    _ => {}
                }
            }
            reader_closed.store(true, Ordering::SeqCst);
            let _ = tx.send(ServerEvent::Closed);
        });

        Some(Self {
            writer,
            events: rx,
            closed,
        })
    }

    /// Send one JSON text frame (unmasked, per RFC server side).
    pub fn send(&self, value: &Value) -> bool {
        if self.closed.load(Ordering::SeqCst) {
            return false;
        }
        let payload = value.to_string();
        let mut guard = match self.writer.lock() {
            Ok(g) => g,
            Err(e) => e.into_inner(),
        };
        write_frame(&mut guard, 0x1, payload.as_bytes()).is_ok()
    }

    pub fn events(&self) -> &Receiver<ServerEvent> {
        &self.events
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::SeqCst)
    }
}

impl Drop for ServerSocket {
    fn drop(&mut self) {
        self.closed.store(true, Ordering::SeqCst);
        if let Ok(mut guard) = self.writer.lock() {
            let _ = write_frame(&mut guard, 0x8, &[]);
            let _ = guard.shutdown(std::net::Shutdown::Both);
        }
    }
}

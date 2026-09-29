//! ws.rs — minimal loopback WebSocket (RFC 6455) client.
//!
//! Governed tool backends speak JSON text frames over
//! ``ws://127.0.0.1:<port>/?token=…&instance=…``.  A small hand-rolled
//! client over ``TcpStream`` is sufficient — mirroring ``http.rs``, no
//! external websocket stack is pulled in.  Client frames are masked per
//! the RFC; server frames arrive unmasked.  Only text frames carry
//! meaning; ping is answered with pong, close terminates the stream.

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::Value;

const WS_GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
/// Shared with the server half (``ws_server``) for the 101 handshake.
pub(crate) const WS_GUID_PUB: &str = WS_GUID;
const MAX_FRAME: usize = 16 * 1024 * 1024;

/// Events delivered by the reader thread to the UI.
#[derive(Debug)]
pub enum WsEvent {
    /// A decoded JSON text frame from the backend.
    Message(Value),
    /// The socket closed (peer close frame, error, or local close()).
    Closed,
}

/// Shared with the server half (``ws_server``) for the 101 handshake.
pub(crate) fn base64_encode_pub(data: &[u8]) -> String {
    base64_encode(data)
}

fn base64_encode(data: &[u8]) -> String {
    const TABLE: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(TABLE[(n >> 18) as usize & 63] as char);
        out.push(TABLE[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            TABLE[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            TABLE[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

/// Shared with the server half (``ws_server``) for the 101 handshake.
pub(crate) fn sha1_pub(data: &[u8]) -> [u8; 20] {
    sha1(data)
}

/// SHA-1 — used only for the Sec-WebSocket-Accept handshake proof.
fn sha1(data: &[u8]) -> [u8; 20] {
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    let bit_len = (data.len() as u64) * 8;
    let mut msg = data.to_vec();
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());
    for block in msg.chunks_exact(64) {
        let mut w = [0u32; 80];
        for (i, word) in block.chunks_exact(4).enumerate() {
            w[i] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (i, &wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A827999u32),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let tmp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = tmp;
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }
    let mut out = [0u8; 20];
    for (i, v) in h.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&v.to_be_bytes());
    }
    out
}

/// Parse a ``ws://127.0.0.1:<port>/<path>?<query>`` URL.  Returns
/// ``(port, request_target)``; rejects anything that is not loopback.
fn parse_loopback_ws_url(url: &str) -> Option<(u16, String)> {
    let rest = url.strip_prefix("ws://")?;
    let (authority, target) = match rest.find('/') {
        Some(idx) => (&rest[..idx], &rest[idx..]),
        None => (rest, "/"),
    };
    let (host, port_s) = authority.rsplit_once(':')?;
    if host != "127.0.0.1" {
        return None;
    }
    let port: u16 = port_s.parse().ok()?;
    if !(1024..=65535).contains(&port) {
        return None;
    }
    Some((port, target.to_string()))
}

fn write_frame(stream: &mut TcpStream, opcode: u8, payload: &[u8]) -> std::io::Result<()> {
    let mut mask = [0u8; 4];
    getrandom::getrandom(&mut mask).map_err(|_| {
        std::io::Error::new(std::io::ErrorKind::Other, "rng unavailable")
    })?;
    let mut frame = Vec::with_capacity(payload.len() + 14);
    frame.push(0x80 | opcode); // FIN + opcode
    let len = payload.len();
    if len < 126 {
        frame.push(0x80 | len as u8);
    } else if len <= u16::MAX as usize {
        frame.push(0x80 | 126);
        frame.extend_from_slice(&(len as u16).to_be_bytes());
    } else {
        frame.push(0x80 | 127);
        frame.extend_from_slice(&(len as u64).to_be_bytes());
    }
    frame.extend_from_slice(&mask);
    frame.extend(payload.iter().zip(mask.iter().cycle()).map(|(b, m)| b ^ m));
    stream.write_all(&frame)
}

fn read_exact(stream: &mut TcpStream, buf: &mut [u8]) -> std::io::Result<()> {
    stream.read_exact(buf)
}

/// Loopback WebSocket session: a reader thread produces `WsEvent`s on the
/// channel while the writer half accepts `send` calls.
pub struct LoopbackSocket {
    writer: Arc<Mutex<TcpStream>>,
    events: Receiver<WsEvent>,
    closed: Arc<AtomicBool>,
}

impl LoopbackSocket {
    /// Connect and perform the opening handshake.
    pub fn connect(url: &str, timeout: Duration) -> Option<Self> {
        let (port, target) = parse_loopback_ws_url(url)?;
        let addr = format!("127.0.0.1:{port}");
        let resolved = addr.to_socket_addrs().ok()?.next()?;
        let mut stream = TcpStream::connect_timeout(&resolved, timeout).ok()?;
        stream.set_nodelay(true).ok()?;
        stream.set_read_timeout(Some(Duration::from_secs(120))).ok()?;

        let mut key_bytes = [0u8; 16];
        getrandom::getrandom(&mut key_bytes).ok()?;
        let key = base64_encode(&key_bytes);
        let request = format!(
            "GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\
             Upgrade: websocket\r\nConnection: Upgrade\r\n\
             Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        );
        stream.write_all(request.as_bytes()).ok()?;

        // Read the handshake response header block.
        let mut head = Vec::with_capacity(512);
        let mut byte = [0u8; 1];
        let deadline = std::time::Instant::now() + timeout;
        while !head.ends_with(b"\r\n\r\n") {
            if std::time::Instant::now() > deadline || head.len() > 16 * 1024 {
                return None;
            }
            match stream.read(&mut byte) {
                Ok(1) => head.push(byte[0]),
                _ => return None,
            }
        }
        let head_text = String::from_utf8_lossy(&head);
        if !head_text.starts_with("HTTP/1.1 101") && !head_text.starts_with("HTTP/1.0 101") {
            return None;
        }
        let expected = base64_encode(&sha1(format!("{key}{WS_GUID}").as_bytes()));
        let accept_ok = head_text
            .lines()
            .any(|l| l.to_ascii_lowercase().starts_with("sec-websocket-accept:")
                && l.splitn(2, ':').nth(1).map(str::trim) == Some(expected.as_str()));
        if !accept_ok {
            return None;
        }

        let writer = Arc::new(Mutex::new(stream.try_clone().ok()?));
        let reader_writer = Arc::clone(&writer);
        let closed = Arc::new(AtomicBool::new(false));
        let reader_closed = Arc::clone(&closed);
        let (tx, rx) = channel::<WsEvent>();
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
                                if tx.send(WsEvent::Message(v)).is_err() {
                                    return;
                                }
                            }
                        }
                    }
                    0x1 => {
                        if fin {
                            if let Ok(v) = serde_json::from_slice::<Value>(&payload) {
                                if tx.send(WsEvent::Message(v)).is_err() {
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
            let _ = tx.send(WsEvent::Closed);
        });

        Some(Self {
            writer,
            events: rx,
            closed,
        })
    }

    /// Send one JSON text frame.  Returns false when the socket is closed.
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

    /// Drain pending backend events (non-blocking).
    pub fn events(&self) -> &Receiver<WsEvent> {
        &self.events
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::SeqCst)
    }
}

impl Drop for LoopbackSocket {
    fn drop(&mut self) {
        self.closed.store(true, Ordering::SeqCst);
        if let Ok(mut guard) = self.writer.lock() {
            let _ = write_frame(&mut guard, 0x8, &[]);
            let _ = guard.shutdown(std::net::Shutdown::Both);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::net::TcpListener;

    /// Mock backend: handshake, read one masked text frame, echo a JSON
    /// result frame back unmasked (server-side per RFC), then a ping.
    fn mock_server(listener: TcpListener) {
        let (mut stream, _) = listener.accept().unwrap();
        let mut head = Vec::new();
        let mut byte = [0u8; 1];
        while !head.ends_with(b"\r\n\r\n") {
            stream.read_exact(&mut byte).unwrap();
            head.push(byte[0]);
        }
        let text = String::from_utf8_lossy(&head);
        let key = text
            .lines()
            .find(|l| l.to_ascii_lowercase().starts_with("sec-websocket-key:"))
            .and_then(|l| l.splitn(2, ':').nth(1))
            .map(str::trim)
            .unwrap()
            .to_string();
        let accept = base64_encode(&sha1(format!("{key}{WS_GUID}").as_bytes()));
        let response = format!(
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\
             Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
        );
        stream.write_all(response.as_bytes()).unwrap();

        // Read the client's masked text frame.
        let mut header = [0u8; 2];
        stream.read_exact(&mut header).unwrap();
        assert!(header[1] & 0x80 != 0, "client frames must be masked");
        let len = (header[1] & 0x7F) as usize;
        let mut mask = [0u8; 4];
        stream.read_exact(&mut mask).unwrap();
        let mut payload = vec![0u8; len];
        stream.read_exact(&mut payload).unwrap();
        for (i, b) in payload.iter_mut().enumerate() {
            *b ^= mask[i % 4];
        }
        let received: Value = serde_json::from_slice(&payload).unwrap();
        assert_eq!(received["command"], "star_chat_status");

        // Reply with an unmasked text frame (server-to-client).
        let reply = json!({"event": "star_chat_status_result",
                           "payload": {"ok": true, "request_id": "r1"}});
        let body = reply.to_string();
        let mut frame = vec![0x81u8];
        frame.push(body.len() as u8);
        frame.extend_from_slice(body.as_bytes());
        stream.write_all(&frame).unwrap();
    }

    #[test]
    fn loopback_handshake_and_roundtrip() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || mock_server(listener));

        let socket = LoopbackSocket::connect(
            &format!("ws://127.0.0.1:{port}/?token=t&instance=i"),
            Duration::from_secs(5),
        )
        .expect("handshake");
        assert!(socket.send(&json!({"command": "star_chat_status",
                                    "payload": {"request_id": "r1"}})));
        match socket.events().recv_timeout(Duration::from_secs(5)) {
            Ok(WsEvent::Message(v)) => {
                assert_eq!(v["event"], "star_chat_status_result");
            }
            other => panic!("expected message, got {:?}", other.map(|_| ())),
        }
    }

    #[test]
    fn rejects_non_loopback() {
        assert!(LoopbackSocket::connect(
            "ws://example.com:9000/",
            Duration::from_millis(50)
        )
        .is_none());
        assert!(LoopbackSocket::connect(
            "ws://127.0.0.1:80/",
            Duration::from_millis(50)
        )
        .is_none());
    }
}

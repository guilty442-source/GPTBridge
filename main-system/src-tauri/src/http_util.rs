//! http_util.rs — minimal loopback HTTP/1.1 client.
//!
//! The governed backend and bridge listen on 127.0.0.1 with plain HTTP, so a
//! small TcpStream client is sufficient; no external HTTP stack is needed.

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

fn connect_loopback(host: &str, port: u16, timeout: Duration) -> Option<TcpStream> {
    let addr = format!("{host}:{port}");
    let sock = addr.to_socket_addrs().ok()?.next()?;
    let stream = TcpStream::connect_timeout(&sock, timeout).ok()?;
    Some(stream)
}

/// One HTTP response from the loopback peer.
pub struct HttpResponse {
    pub status: u16,
    pub body: Vec<u8>,
}

/// Perform a GET against ``127.0.0.1:port`` with optional headers.
/// Returns ``None`` on any transport failure (connection refused, timeout,
/// malformed response) — callers treat that as "not alive".
pub fn get(
    host: &str,
    port: u16,
    path: &str,
    headers: &[(&str, &str)],
    timeout: Duration,
) -> Option<HttpResponse> {
    let mut stream = connect_loopback(host, port, timeout)?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.set_write_timeout(Some(timeout)).ok()?;
    stream.set_nodelay(true).ok()?;

    let mut request = format!("GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n");
    for (name, value) in headers {
        request.push_str(&format!("{name}: {value}\r\n"));
    }
    request.push_str("\r\n");
    stream.write_all(request.as_bytes()).ok()?;

    let deadline = std::time::Instant::now() + timeout + Duration::from_secs(2);
    let mut raw = Vec::with_capacity(4096);
    let mut buf = [0u8; 8192];
    loop {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                raw.extend_from_slice(&buf[..n]);
                if raw.len() > 4 * 1024 * 1024 {
                    return None;
                }
            }
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                break
            }
            Err(_) => return None,
        }
        if std::time::Instant::now() > deadline {
            break;
        }
        // Stop early once headers + full body for Content-Length are in.
        if let Some(pos) = find_subslice(&raw, b"\r\n\r\n") {
            let headers_part = &raw[..pos];
            let text = String::from_utf8_lossy(headers_part);
            let content_length = text
                .lines()
                .find(|l| l.to_ascii_lowercase().starts_with("content-length:"))
                .and_then(|l| l.split(':').nth(1)?.trim().parse::<usize>().ok());
            if let Some(cl) = content_length {
                if raw.len() >= pos + 4 + cl {
                    break;
                }
            }
        }
    }

    let header_end = find_subslice(&raw, b"\r\n\r\n")?;
    let head = String::from_utf8_lossy(&raw[..header_end]).to_string();
    let status = head
        .lines()
        .next()?
        .split_whitespace()
        .nth(1)?
        .parse::<u16>()
        .ok()?;
    let body = raw[header_end + 4..].to_vec();
    Some(HttpResponse { status, body })
}

/// POST a body; same contract as ``get``.
pub fn post(
    host: &str,
    port: u16,
    path: &str,
    headers: &[(&str, &str)],
    body: &[u8],
    timeout: Duration,
) -> Option<HttpResponse> {
    let mut stream = connect_loopback(host, port, timeout)?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.set_write_timeout(Some(timeout)).ok()?;
    stream.set_nodelay(true).ok()?;

    let mut request = format!(
        "POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\nContent-Length: {}\r\n",
        body.len()
    );
    for (name, value) in headers {
        request.push_str(&format!("{name}: {value}\r\n"));
    }
    request.push_str("\r\n");
    stream.write_all(request.as_bytes()).ok()?;
    stream.write_all(body).ok()?;

    let mut raw = Vec::with_capacity(4096);
    let mut buf = [0u8; 8192];
    loop {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => raw.extend_from_slice(&buf[..n]),
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                break
            }
            Err(_) => return None,
        }
        if let Some(pos) = find_subslice(&raw, b"\r\n\r\n") {
            let text = String::from_utf8_lossy(&raw[..pos]).to_string();
            let content_length = text
                .lines()
                .find(|l| l.to_ascii_lowercase().starts_with("content-length:"))
                .and_then(|l| l.split(':').nth(1)?.trim().parse::<usize>().ok());
            if let Some(cl) = content_length {
                if raw.len() >= pos + 4 + cl {
                    break;
                }
            }
        }
    }

    let header_end = find_subslice(&raw, b"\r\n\r\n")?;
    let head = String::from_utf8_lossy(&raw[..header_end]).to_string();
    let status = head
        .lines()
        .next()?
        .split_whitespace()
        .nth(1)?
        .parse::<u16>()
        .ok()?;
    let body_bytes = raw[header_end + 4..].to_vec();
    Some(HttpResponse {
        status,
        body: body_bytes,
    })
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|w| w == needle)
}

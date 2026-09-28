//! Minimal blocking HTTP/1.1 JSON client (loopback services only).

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

use serde_json::Value;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
const IO_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_RESPONSE_BYTES: usize = 64 * 1024 * 1024;

fn host_port(base: &str) -> Result<(&str, u16), String> {
    let rest = base
        .strip_prefix("http://")
        .ok_or_else(|| format!("non-http base url: {}", base))?;
    let (host, port) = rest
        .trim_end_matches('/')
        .rsplit_once(':')
        .ok_or_else(|| format!("missing port in base url: {}", base))?;
    if !matches!(host, "127.0.0.1" | "localhost" | "::1" | "[::1]") {
        return Err(format!("refusing non-loopback upstream: {}", host));
    }
    Ok((host, port.parse().map_err(|_| "bad port".to_string())?))
}

/// POST `body` as JSON to `base + path`; returns the decoded JSON body.
/// Errors surface as strings; callers decide the fail-closed shape.
pub fn post_json(base: &str, path: &str, body: &Value) -> Result<Value, String> {
    let (host, port) = host_port(base)?;
    let payload = body.to_string();
    let addr = (host, port)
        .to_socket_addrs()
        .map_err(|e| e.to_string())?
        .next()
        .ok_or_else(|| format!("unresolvable upstream {}:{}", host, port))?;
    let mut stream =
        TcpStream::connect_timeout(&addr, CONNECT_TIMEOUT).map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(IO_TIMEOUT))
        .and_then(|_| stream.set_write_timeout(Some(IO_TIMEOUT)))
        .ok();
    let _ = stream.set_nodelay(true);
    let request = format!(
        "POST {} HTTP/1.1\r\nHost: {}:{}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        path,
        host,
        port,
        payload.len()
    );
    stream
        .write_all(request.as_bytes())
        .and_then(|_| stream.write_all(payload.as_bytes()))
        .map_err(|e| e.to_string())?;

    let mut buf = Vec::with_capacity(8192);
    let mut chunk = [0u8; 8192];
    loop {
        let n = stream.read(&mut chunk).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        buf.extend_from_slice(&chunk[..n]);
        if buf.len() > MAX_RESPONSE_BYTES {
            return Err("RESPONSE_TOO_LARGE".to_string());
        }
    }
    let text = String::from_utf8_lossy(&buf);
    let split = text
        .find("\r\n\r\n")
        .ok_or_else(|| "malformed http response".to_string())?;
    let status = text
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|c| c.parse::<u16>().ok())
        .unwrap_or(0);
    if status >= 400 {
        return Err(format!("upstream HTTP {}", status));
    }
    serde_json::from_str(&text[split + 4..]).map_err(|e| e.to_string())
}

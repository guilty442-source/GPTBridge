use std::sync::mpsc::TryRecvError;
use std::time::{Duration, Instant};

use gptbridge_core::ipc::ws::{LoopbackSocket, WsEvent};
use serde_json::{json, Value};

const RECONNECT_DELAY: Duration = Duration::from_millis(500);

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum ConnState {
    Connecting,
    Connected,
    Disconnected,
}

/// Shared governed backend connection for native tool surfaces.
///
/// Owns the loopback WebSocket, the 500 ms reconnect cadence and the
/// request-id counter; each tool surface drives it from ``tick`` and
/// routes the drained ``(event, payload)`` pairs itself.
pub(crate) struct Backend {
    socket: Option<LoopbackSocket>,
    pub(crate) state: ConnState,
    disconnected_at: Option<Instant>,
    next_request: u64,
    req_prefix: &'static str,
}

impl Backend {
    pub(crate) fn new(req_prefix: &'static str) -> Self {
        Self {
            socket: None,
            state: ConnState::Disconnected,
            disconnected_at: Some(Instant::now() - RECONNECT_DELAY),
            next_request: 0,
            req_prefix,
        }
    }

    pub(crate) fn connected(&self) -> bool {
        self.state == ConnState::Connected
    }

    /// Reconnect when due and drain all pending socket events.
    /// Returns ``(events, closed_this_tick)``.
    pub(crate) fn tick(&mut self, url: &str) -> (Vec<(String, Value)>, bool) {
        if self.state == ConnState::Disconnected {
            let due = self
                .disconnected_at
                .map(|t| t.elapsed() >= RECONNECT_DELAY)
                .unwrap_or(true);
            if due {
                self.state = ConnState::Connecting;
                match LoopbackSocket::connect(url, Duration::from_secs(4)) {
                    Some(socket) => {
                        self.socket = Some(socket);
                        self.state = ConnState::Connected;
                        self.disconnected_at = None;
                    }
                    None => {
                        self.state = ConnState::Disconnected;
                        self.disconnected_at = Some(Instant::now());
                    }
                }
            }
        }
        let mut batch = Vec::new();
        let mut closed = false;
        if let Some(socket) = self.socket.as_ref() {
            loop {
                match socket.events().try_recv() {
                    Ok(WsEvent::Message(v)) => {
                        let event = v["event"].as_str().unwrap_or("").to_string();
                        batch.push((event, v["payload"].clone()));
                    }
                    Ok(WsEvent::Closed) | Err(TryRecvError::Disconnected) => {
                        closed = true;
                        break;
                    }
                    Err(TryRecvError::Empty) => break,
                }
            }
        }
        if closed {
            self.socket = None;
            self.state = ConnState::Disconnected;
            self.disconnected_at = Some(Instant::now());
        }
        (batch, closed)
    }

    /// Send a governed command frame; the request id is generated and
    /// injected into the payload.  Returns ``None`` when the frame could
    /// not be written.
    pub(crate) fn send(&mut self, command: &str, payload: Value) -> Option<String> {
        self.next_request += 1;
        let request_id = format!(
            "{}-{}-{}",
            self.req_prefix,
            self.next_request,
            std::process::id()
        );
        let mut frame = json!({
            "command": command,
            "payload": { "request_id": request_id },
        });
        if let (Some(body), Some(extra)) = (
            frame["payload"].as_object_mut(),
            payload.as_object(),
        ) {
            for (k, v) in extra {
                body.insert(k.clone(), v.clone());
            }
        }
        match self.socket.as_ref().map(|s| s.send(&frame)) {
            Some(true) => Some(request_id),
            _ => None,
        }
    }
}

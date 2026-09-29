//! IPC domain — loopback HTTP transport and backend endpoint discovery.

pub mod discovery;
pub mod envelope;
pub mod http;
pub mod ws;
pub mod ws_server;

pub use envelope::{RequestEnvelope, IPC_ENVELOPE_VERSION};

pub use discovery::{
    backend_session_descriptor, is_gateway_alive, resolve_backend_port, BACKEND_HEALTH_PATH,
    LOOPBACK_HOST,
};
pub use http::{get, post, HttpResponse};
pub use ws::{LoopbackSocket, WsEvent};
pub use ws_server::{HttpRequest, ServerEvent, ServerSocket, ServerWriter};

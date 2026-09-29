//! Lifecycle domain — managed-backend (boot_core) supervision.
//!
//! Architecture boundary (A60/A61): the desktop shell only wakes the screen
//! and spawns the startup core; the managed native backend supervises the
//! main system and generates its own governance bootstrap token (the Python
//! ``boot_core``/``main.py`` chain is retired, B166).  This domain only
//! spawns/stops the backend, tracks liveness, attaches to an existing
//! governed backend, and applies the bounded auto-restart policy.

mod backend;
mod monitor;
mod process;

pub use backend::{
    backend_runtime_info, ensure_backend_started, get_backend_status, restart_backend,
    start_backend, stop_backend, BackendStatus,
};

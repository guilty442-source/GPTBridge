//! pg.rs — governed PostgreSQL access for the native backend host.
//!
//! Parity with the retired Python ``dsn_policy.resolve_dsn(DsnPurpose.RUNTIME)``:
//! the runtime DSN comes from ``GPTBRIDGE_POSTGRES_DSN`` only.  A ``credman:``
//! indirection requires the governed credential store, which this host does
//! not implement — it fails closed (returns ``None``) instead of falling back.

use postgres::{Client, NoTls};

/// Resolve the runtime DSN.  ``None`` means unavailable — callers map that
/// to the governed *_UNAVAILABLE denial rather than guessing a default.
pub fn runtime_dsn() -> Option<String> {
    let raw = std::env::var("GPTBRIDGE_POSTGRES_DSN").ok()?;
    let dsn = raw.trim();
    if dsn.is_empty() || dsn.starts_with("credman:") {
        return None;
    }
    Some(dsn.to_string())
}

/// Open a blocking connection with a bounded connect timeout — a down DB
/// must fail in seconds, never hang the command loop.
pub fn connect() -> Option<Client> {
    let dsn = runtime_dsn()?;
    let connstr = if dsn.contains("connect_timeout") {
        dsn
    } else if dsn.contains("://") {
        // URI form — timeout travels as a query parameter.
        let sep = if dsn.contains('?') { "&" } else { "?" };
        format!("{dsn}{sep}connect_timeout=5")
    } else {
        format!("{dsn} connect_timeout=5")
    };
    Client::connect(&connstr, NoTls).ok()
}

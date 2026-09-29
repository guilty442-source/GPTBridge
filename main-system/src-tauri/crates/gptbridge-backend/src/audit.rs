//! audit.rs — durable per-command audit ledgers (JSONL append).
//!
//! Port of ``command_router/_audit_writer.py``: every governed query writes
//! one JSON line to ``main-system/runtime/state/<ledger>.jsonl`` — best-effort
//! (a failed audit append must not break the query path, same as Python).

use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;

use gptbridge_core::native::paths;
use serde_json::{json, Value};

fn state_dir() -> PathBuf {
    paths::path_library()
        .workspace_root
        .join("main-system")
        .join("runtime")
        .join("state")
}

/// ISO-8601 UTC timestamp with the ``Z`` suffix the ledgers already carry.
pub fn iso_now() -> String {
    let now = time::OffsetDateTime::now_utc();
    now.format(&time::format_description::well_known::Rfc3339)
        .unwrap_or_default()
}

/// Append one record to ``runtime/state/<name>.jsonl``.  Creates the
/// directory lazily; any I/O failure is swallowed (audit is best-effort).
pub fn append_audit_record(name: &str, record: Value) {
    let _ = std::fs::create_dir_all(state_dir());
    let path = state_dir().join(format!("{name}.jsonl"));
    let Ok(mut file) = OpenOptions::new().create(true).append(true).open(&path)
    else {
        return;
    };
    let mut row = record;
    row["timestamp"] = json!(iso_now());
    let _ = writeln!(file, "{}", row);
}

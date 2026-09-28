//! app.rs — Application Core.
//!
//! Process-wide facts shared by every UI surface (Tauri shell, GPUI views,
//! egui console): product identity, the managed-backend flag, the uniform
//! ``[Main System]`` diagnostics reporter, and time helpers.

/// Product version reported through status payloads (parity with the retired
/// Electron ``product-version.ts``).
pub const PRODUCT_VERSION: &str = "1.0.0";

/// Whether this process owns the managed-backend lifecycle
/// (``GPTBRIDGE_MANAGE_BACKEND=1``).  When the dev script supervises the
/// backend the shell only attaches/probes.
pub fn manage_backend() -> bool {
    std::env::var("GPTBRIDGE_MANAGE_BACKEND").ok().as_deref() == Some("1")
}

/// Best-effort diagnostics reporter.  println!/eprintln! panic on a broken
/// inherited pipe — the shell can outlive the launcher's stdio handles, so
/// a failed write must never be fatal.
pub fn report(event: &str, payload: serde_json::Value) {
    use std::io::Write;
    let _ = writeln!(std::io::stderr().lock(), "[Main System] {event} {payload}");
    let _ = std::io::stderr().flush();
    let _ = writeln!(std::io::stdout().lock(), "[Main System] {event} {payload}");
    let _ = std::io::stdout().flush();
}

/// Current time as epoch milliseconds.
pub fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// Civil-from-days UTC timestamp (Howard Hinnant's algorithm) — used by the
/// loopback bridge's published state file.
pub fn iso_now() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64;
    let days = secs.div_euclid(86_400);
    let secs_of_day = secs.rem_euclid(86_400);
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    let (h, mi, s) = (
        secs_of_day / 3600,
        (secs_of_day % 3600) / 60,
        secs_of_day % 60,
    );
    format!("{y:04}-{m:02}-{d:02}T{h:02}:{mi:02}:{s:02}Z")
}

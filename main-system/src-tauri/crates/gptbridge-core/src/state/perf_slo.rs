//! slo.rs — port of src-ui/main/perfSlo.ts.
//!
//! MS10 SLO dashboard data layer: reads the backend ``star-perf-baseline/v1``
//! snapshot (``runtime/state/perf-baseline-latest.json``) and evaluates it
//! against blueprint SLO budgets.  Unmeasurable fields stay null /
//! ``available:false`` — fail-closed, never fabricated.

use std::fs;
use std::path::Path;

pub const SLO_BUDGET_BACKEND_RSS_MB: f64 = 220.0;
pub const SLO_BUDGET_IDLE_CPU_PERCENT: f64 = 3.0;

fn as_number(value: &serde_json::Value) -> Option<f64> {
    value.as_f64().filter(|v| v.is_finite())
}

fn budget_status(value: Option<f64>, budget: Option<f64>) -> &'static str {
    match (value, budget) {
        (None, _) => "unknown",
        (Some(_), None) => "measured",
        (Some(v), Some(b)) if v >= b => "over",
        (Some(v), Some(b)) if v >= b * 0.9 => "warn",
        _ => "ok",
    }
}

fn metric(
    key: &str,
    value: Option<f64>,
    unit: &str,
    budget: Option<f64>,
) -> serde_json::Value {
    serde_json::json!({
        "key": key,
        "value": value,
        "unit": unit,
        "budget": budget,
        "status": budget_status(value, budget),
    })
}

fn empty_report(error: Option<&str>) -> serde_json::Value {
    let mut report = serde_json::json!({
        "available": false,
        "baselineVersion": null,
        "capturedAt": null,
        "snapshotAgeS": null,
        "metrics": [],
        "ipcPerCommand": [],
    });
    if let Some(error) = error {
        report["error"] = serde_json::Value::String(error.to_string());
    }
    report
}

pub fn evaluate_baseline(snapshot: &serde_json::Value, now_ms: i64) -> serde_json::Value {
    if !snapshot.is_object() {
        return empty_report(Some("snapshot-not-an-object"));
    }
    let baseline_version = snapshot["baseline_version"].as_str();
    let captured_at = snapshot["captured_at"].as_str();
    let snapshot_age_s = captured_at.and_then(|iso| {
        // Parse seconds-of-epoch by re-parsing through a civil algorithm is
        // overkill; use time parsing via a lightweight ISO approach.
        parse_iso_millis(iso).map(|ms| ((now_ms - ms).max(0)) / 1000)
    });

    let proc = &snapshot["process"];
    let gpu = &snapshot["gpu"];
    let ipc = &snapshot["ipc"];

    let metrics = vec![
        metric("ipc_p95_ms", as_number(&ipc["p95_ms"]), "ms", None),
        metric("ipc_p50_ms", as_number(&ipc["p50_ms"]), "ms", None),
        metric("ipc_p99_ms", as_number(&ipc["p99_ms"]), "ms", None),
        metric(
            "backend_rss_mb",
            as_number(&proc["rss_mb"]),
            "MB",
            Some(SLO_BUDGET_BACKEND_RSS_MB),
        ),
        metric("cpu_percent", as_number(&proc["cpu_percent"]), "percent", None),
        metric(
            "gpu_vram_used_mb",
            as_number(&gpu["used_mb"]),
            "MB",
            as_number(&gpu["total_mb"]),
        ),
        metric(
            "gpu_utilization",
            as_number(&gpu["utilization"]),
            "percent",
            None,
        ),
        metric("ipc_samples", as_number(&ipc["samples"]), "count", None),
    ];

    let mut ipc_per_command: Vec<serde_json::Value> = ipc["per_command"]
        .as_object()
        .map(|map| {
            map.iter()
                .map(|(command, stats)| {
                    serde_json::json!({
                        "command": command,
                        "samples": as_number(&stats["samples"]).unwrap_or(0.0),
                        "p95_ms": as_number(&stats["p95_ms"]),
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    ipc_per_command.sort_by(|a, b| {
        b["p95_ms"]
            .as_f64()
            .unwrap_or(0.0)
            .partial_cmp(&a["p95_ms"].as_f64().unwrap_or(0.0))
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    let available = metrics.iter().any(|m| !m["value"].is_null());
    let mut report = serde_json::json!({
        "available": available,
        "baselineVersion": baseline_version,
        "capturedAt": captured_at,
        "snapshotAgeS": snapshot_age_s,
        "metrics": metrics,
        "ipcPerCommand": ipc_per_command,
    });
    if !available {
        report["error"] = serde_json::Value::String("no-measurable-fields".to_string());
    }
    report
}

/// Parse a minimal ISO-8601 UTC timestamp (``YYYY-MM-DDTHH:MM:SS[.sss]Z``)
/// into epoch milliseconds; returns None on deviation.
fn parse_iso_millis(iso: &str) -> Option<i64> {
    let bytes = iso.as_bytes();
    if bytes.len() < 20 || bytes[4] != b'-' || bytes[7] != b'-' || bytes[10] != b'T' {
        return None;
    }
    let num = |a: usize, b: usize| iso.get(a..b)?.parse::<i64>().ok();
    let (year, month, day) = (num(0, 4)?, num(5, 7)?, num(8, 10)?);
    let (hour, minute, second) = (num(11, 13)?, num(14, 16)?, num(17, 19)?);
    // Days from civil (Hinnant).
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    Some(days * 86_400_000 + hour * 3_600_000 + minute * 60_000 + second * 1_000)
}

pub fn get_perf_slo(workspace_root: &Path) -> serde_json::Value {
    let snapshot_path = workspace_root
        .join("main-system")
        .join("runtime")
        .join("state")
        .join("perf-baseline-latest.json");
    let Ok(raw) = fs::read_to_string(&snapshot_path) else {
        return empty_report(Some("snapshot-unavailable"));
    };
    let Ok(snapshot) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return empty_report(Some("snapshot-malformed"));
    };
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0);
    evaluate_baseline(&snapshot, now_ms)
}

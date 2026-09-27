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

#[cfg(test)]
mod tests {
    //! Contract port of `main-system/scripts/test_perf_slo.ts`: the SLO
    //! dashboard contract moved with the implementation into this crate.

    use super::*;
    use serde_json::json;

    fn sample_snapshot() -> serde_json::Value {
        json!({
            "baseline_version": "star-perf-baseline/v1",
            "captured_at": "2026-09-22T16:00:00Z",
            "process": { "rss_mb": 96.8, "cpu_percent": 2.1, "threads": 30 },
            "gpu": { "total_mb": 6144, "used_mb": 512, "free_mb": 5632, "utilization": 4 },
            "ipc": {
                "samples": 128,
                "p50_ms": 3.2,
                "p95_ms": 12.4,
                "p99_ms": 40.1,
                "per_command": {
                    "tools.list": { "samples": 40, "p95_ms": 9.9 },
                    "status.get": { "samples": 88, "p95_ms": 15.5 },
                },
            },
        })
    }

    fn metric_of<'a>(report: &'a serde_json::Value, key: &str) -> &'a serde_json::Value {
        report["metrics"]
            .as_array()
            .and_then(|metrics| metrics.iter().find(|m| m["key"] == key))
            .unwrap_or_else(|| panic!("missing metric {key}"))
    }

    fn write_snapshot(workspace_root: &Path, payload: &serde_json::Value) {
        let state_dir = workspace_root.join("main-system/runtime/state");
        fs::create_dir_all(&state_dir).unwrap();
        fs::write(
            state_dir.join("perf-baseline-latest.json"),
            serde_json::to_string(payload).unwrap(),
        )
        .unwrap();
    }

    #[test]
    fn full_snapshot_parses_and_budgets_apply() {
        let report = evaluate_baseline(
            &sample_snapshot(),
            parse_iso_millis("2026-09-22T16:01:00Z").unwrap(),
        );
        assert_eq!(report["available"], json!(true));
        assert_eq!(report["baselineVersion"], json!("star-perf-baseline/v1"));
        assert_eq!(report["snapshotAgeS"], json!(60));
        assert_eq!(metric_of(&report, "backend_rss_mb")["status"], json!("ok"));
        assert_eq!(
            metric_of(&report, "backend_rss_mb")["budget"],
            json!(SLO_BUDGET_BACKEND_RSS_MB)
        );
        assert_eq!(metric_of(&report, "ipc_p95_ms")["value"], json!(12.4));
        assert_eq!(metric_of(&report, "ipc_p95_ms")["status"], json!("measured"));
        let per_command = report["ipcPerCommand"].as_array().unwrap();
        assert_eq!(per_command[0]["command"], json!("status.get")); // sorted desc
        assert_eq!(per_command[1]["command"], json!("tools.list"));
    }

    #[test]
    fn budget_boundaries_over_and_warn() {
        let mut snapshot = sample_snapshot();
        snapshot["process"] = json!({ "rss_mb": SLO_BUDGET_BACKEND_RSS_MB + 1.0 });
        let over = evaluate_baseline(&snapshot, 0);
        assert_eq!(metric_of(&over, "backend_rss_mb")["status"], json!("over"));

        snapshot["process"] = json!({ "rss_mb": SLO_BUDGET_BACKEND_RSS_MB * 0.95 });
        let warn = evaluate_baseline(&snapshot, 0);
        assert_eq!(metric_of(&warn, "backend_rss_mb")["status"], json!("warn"));
    }

    #[test]
    fn invalid_snapshots_fail_closed() {
        assert_eq!(evaluate_baseline(&json!(null), 0)["available"], json!(false));
        assert_eq!(evaluate_baseline(&json!("x"), 0)["available"], json!(false));
        let empty = evaluate_baseline(&json!({}), 0);
        assert_eq!(empty["available"], json!(false));
        assert_eq!(empty["error"], json!("no-measurable-fields"));
    }

    #[test]
    fn get_perf_slo_missing_malformed_and_fixture() {
        let workspace_root = std::env::temp_dir().join(format!(
            "gptbridge-perf-slo-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&workspace_root);
        fs::create_dir_all(&workspace_root).unwrap();

        let missing = get_perf_slo(&workspace_root);
        assert_eq!(missing["available"], json!(false));
        assert_eq!(missing["error"], json!("snapshot-unavailable"));

        let state_dir = workspace_root.join("main-system/runtime/state");
        fs::create_dir_all(&state_dir).unwrap();
        fs::write(state_dir.join("perf-baseline-latest.json"), "{not json").unwrap();
        let malformed = get_perf_slo(&workspace_root);
        assert_eq!(malformed["available"], json!(false));
        assert_eq!(malformed["error"], json!("snapshot-malformed"));

        write_snapshot(&workspace_root, &sample_snapshot());
        let parsed = get_perf_slo(&workspace_root);
        assert_eq!(parsed["available"], json!(true));
        let gpu_used = metric_of(&parsed, "gpu_vram_used_mb");
        assert_eq!(gpu_used["value"].as_f64(), Some(512.0));
        assert_eq!(gpu_used["budget"].as_f64(), Some(6144.0));
        assert_eq!(gpu_used["status"], json!("ok"));

        let _ = fs::remove_dir_all(&workspace_root);
    }
}

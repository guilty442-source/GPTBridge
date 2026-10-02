//! tools/reaper.rs — idle tool-runtime reaper（閒置後端/服務自動關閉）。
//!
//! 政策來源：資源管制器規則檔
//! ``main-system/config/resource-governor-rules.json`` 的 ``idle_reap``
//! 區塊（enabled / idle_after_s / poll_s / min_observations / exclude）。
//! 執法點在 backend——工具生命週期（running registry＋shutdown token）
//! 只有這裡看得見，governor 只出政策不動手。
//!
//! 判定（每 ``poll_s`` 掃一次 running registry）：
//!   runtime ``/metrics`` 報告
//!     ``last_activity_at`` 距今 ≥ ``idle_after_s``
//!     且 ``in_flight_requests == 0`` 且 ``active_ws_connections == 0``
//!     且 ``worker_queue_size == 0``
//!   連續 ``min_observations`` 次成立 → 走 governed ``stop_tool``
//!   （authenticated /shutdown → 3s 緩衝 → kill）。重啟由
//!   ``toolbox_start_tool`` 按需 5s 契約承接，收割不收功能。
//!
//! Fail-closed：runtime 未發佈 ``in_flight_requests``（舊版二進位無
//! WS 活動計量）→ 永不收割——看不見 WS 工作就不能證明「無用」。
//! manifest ``lifecycle.unloadable == false`` 的工具同樣豁免。

use std::collections::{HashMap, HashSet};
use std::thread;
use std::time::Duration;

use serde_json::{json, Value};
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;

use gptbridge_core::ipc::http;

use super::{load_manifest, running, stop_tool, workspace_root};
use crate::audit;

const LEDGER: &str = "idle-reaper";
const STATE_FILE: &str = "idle-reaper";
const METRICS_TIMEOUT: Duration = Duration::from_millis(1500);

struct Policy {
    enabled: bool,
    poll: Duration,
    idle_after: Duration,
    min_observations: u32,
    exclude: HashSet<String>,
}

fn load_policy() -> Policy {
    let mut policy = Policy {
        enabled: true,
        poll: Duration::from_secs(30),
        idle_after: Duration::from_secs(900),
        min_observations: 2,
        exclude: HashSet::new(),
    };
    let path = workspace_root()
        .join("main-system")
        .join("config")
        .join("resource-governor-rules.json");
    let Ok(raw) = std::fs::read_to_string(&path) else {
        return policy;
    };
    let Ok(rules) = serde_json::from_str::<Value>(&raw) else {
        return policy;
    };
    let reap = &rules["idle_reap"];
    if !reap.is_object() {
        return policy;
    }
    policy.enabled = reap["enabled"].as_bool().unwrap_or(true);
    let num = |key: &str, default: f64| reap[key].as_f64().unwrap_or(default);
    policy.poll =
        Duration::from_secs_f64(num("poll_s", 30.0).clamp(10.0, 3600.0));
    policy.idle_after = Duration::from_secs_f64(
        num("idle_after_s", 900.0).clamp(60.0, 86400.0),
    );
    policy.min_observations =
        (num("min_observations", 2.0) as u32).clamp(1, 10);
    if let Some(list) = reap["exclude"].as_array() {
        policy.exclude = list
            .iter()
            .filter_map(|v| v.as_str().map(str::to_string))
            .collect();
    }
    policy
}

fn parse_ts(text: &str) -> Option<OffsetDateTime> {
    OffsetDateTime::parse(text.trim(), &Rfc3339).ok()
}

/// 距上次活動的秒數：reaper-aware runtime 的 ``last_activity_at``
/// 優先，否則取各 channel ``last_request_at`` 最新值，都沒有就代表
/// 開機至今不曾服務 → 以 uptime 計。
fn idle_seconds(metrics: &Value, now: OffsetDateTime) -> Option<f64> {
    if let Some(t) = metrics["last_activity_at"]
        .as_str()
        .and_then(parse_ts)
    {
        return Some((now - t).as_seconds_f64().max(0.0));
    }
    let mut latest: Option<OffsetDateTime> = None;
    if let Some(channels) = metrics["channel_health"].as_object() {
        for health in channels.values() {
            if let Some(t) =
                health["last_request_at"].as_str().and_then(parse_ts)
            {
                if latest.map(|x| t > x).unwrap_or(true) {
                    latest = Some(t);
                }
            }
        }
    }
    match latest {
        Some(t) => Some((now - t).as_seconds_f64().max(0.0)),
        None => metrics["uptime_seconds"].as_f64(),
    }
}

enum Probe {
    /// /metrics 不可達或回應異形——當作非收割對象（不構成閒置證據）。
    Gone,
    /// 舊版 runtime：沒有 in-flight/WS 計量，看不見的忙不能當閒。
    Unsupported,
    Busy,
    Idle(f64),
}

fn probe(port: u16) -> Probe {
    let Some(resp) = http::get(
        "127.0.0.1",
        port,
        "/metrics",
        &[("Connection", "close")],
        METRICS_TIMEOUT,
    ) else {
        return Probe::Gone;
    };
    if resp.status != 200 {
        return Probe::Gone;
    }
    let Ok(metrics) = serde_json::from_slice::<Value>(&resp.body) else {
        return Probe::Gone;
    };
    let (Some(in_flight), Some(ws)) = (
        metrics["in_flight_requests"].as_i64(),
        metrics["active_ws_connections"].as_i64(),
    ) else {
        return Probe::Unsupported;
    };
    let queue = metrics["worker_queue_size"].as_i64().unwrap_or(0);
    if in_flight > 0 || ws > 0 || queue > 0 {
        return Probe::Busy;
    }
    match idle_seconds(&metrics, OffsetDateTime::now_utc()) {
        Some(idle_s) => Probe::Idle(idle_s),
        None => Probe::Gone,
    }
}

fn reap_pass(
    policy: &Policy,
    streaks: &mut HashMap<String, u32>,
    unsupported: &mut HashSet<String>,
    unloadable_cache: &mut HashMap<String, bool>,
    reaped: &mut u64,
) -> Vec<Value> {
    let tools: Vec<(String, u16)> = {
        let registry = running().lock().unwrap_or_else(|e| e.into_inner());
        registry
            .tools
            .iter()
            .map(|(id, tool)| (id.clone(), tool.runtime_port))
            .collect()
    };
    let mut watched = Vec::new();
    for (id, port) in tools {
        if port == 0 || policy.exclude.contains(&id) {
            streaks.remove(&id);
            continue;
        }
        let allowed = *unloadable_cache.entry(id.clone()).or_insert_with(|| {
            load_manifest(&id)
                .map(|m| m["lifecycle"]["unloadable"].as_bool() != Some(false))
                .unwrap_or(true)
        });
        if !allowed {
            streaks.remove(&id);
            continue;
        }
        match probe(port) {
            Probe::Gone => {
                streaks.remove(&id);
            }
            Probe::Unsupported => {
                streaks.remove(&id);
                if unsupported.insert(id.clone()) {
                    audit::append_audit_record(
                        LEDGER,
                        json!({
                            "event": "idle-reaper.unsupported-runtime",
                            "tool_id": id,
                            "detail": "metrics lacks in_flight_requests/active_ws_connections",
                        }),
                    );
                }
            }
            Probe::Busy => {
                streaks.remove(&id);
            }
            Probe::Idle(idle_s) => {
                if idle_s < policy.idle_after.as_secs_f64() {
                    streaks.remove(&id);
                    watched.push(json!({
                        "tool_id": id,
                        "idle_s": idle_s.round(),
                    }));
                    continue;
                }
                let streak = streaks.entry(id.clone()).or_insert(0);
                *streak += 1;
                if *streak < policy.min_observations {
                    continue;
                }
                // 收割前再探一次——收窄「判定後有請求抵達」的 TOCTOU 窗口。
                let confirm = match probe(port) {
                    Probe::Idle(s) => s >= policy.idle_after.as_secs_f64(),
                    _ => false,
                };
                if !confirm {
                    streaks.remove(&id);
                    continue;
                }
                audit::append_audit_record(
                    LEDGER,
                    json!({
                        "event": "idle-reaper.stop",
                        "tool_id": id,
                        "idle_s": idle_s.round(),
                    }),
                );
                let result = stop_tool(&id);
                streaks.remove(&id);
                *reaped += 1;
                audit::append_audit_record(
                    LEDGER,
                    json!({
                        "event": "idle-reaper.stopped",
                        "tool_id": id,
                        "ok": result["ok"].as_bool().unwrap_or(false),
                        "status": result["status"],
                    }),
                );
            }
        }
    }
    watched
}

fn write_state(enabled: bool, watched: &[Value], reaped: u64) {
    let dir = workspace_root()
        .join("main-system")
        .join("runtime")
        .join("state");
    let _ = std::fs::create_dir_all(&dir);
    let state = json!({
        "role": "idle-reaper",
        "status": if enabled { "watching" } else { "disabled" },
        "managed_by": "main-system",
        "watching": watched,
        "reaped_total": reaped,
        "updated_at": std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64(),
    });
    let tmp = dir.join(format!("{STATE_FILE}.json.tmp"));
    if std::fs::write(&tmp, state.to_string()).is_ok() {
        let _ = std::fs::rename(&tmp, dir.join(format!("{STATE_FILE}.json")));
    }
}

fn run() -> ! {
    let mut streaks: HashMap<String, u32> = HashMap::new();
    let mut unsupported: HashSet<String> = HashSet::new();
    let mut unloadable_cache: HashMap<String, bool> = HashMap::new();
    let mut reaped: u64 = 0;
    loop {
        let policy = load_policy();
        let watched = if policy.enabled {
            reap_pass(
                &policy,
                &mut streaks,
                &mut unsupported,
                &mut unloadable_cache,
                &mut reaped,
            )
        } else {
            streaks.clear();
            Vec::new()
        };
        write_state(policy.enabled, &watched, reaped);
        thread::sleep(policy.poll);
    }
}

/// Start the reaper thread. Idempotent-at-most-once by construction —
/// ``main`` calls it once at boot alongside the supervision engines.
pub fn start() {
    thread::spawn(run);
}

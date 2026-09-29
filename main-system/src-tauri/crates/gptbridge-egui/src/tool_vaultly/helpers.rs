//! tool_vaultly/helpers.rs — pure formatting/parsing helpers for the vaultly surface.

use serde_json::Value;

pub(crate) fn split_keywords(value: &str) -> Vec<String> {
    let mut out: Vec<String> = value
        .split(['\n', ',', '，'])
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect();
    out.sort();
    out.dedup();
    out
}

pub(crate) fn split_links(value: &str) -> Vec<String> {
    let mut out: Vec<String> = value
        .split(|c: char| c.is_whitespace() || c == ',')
        .map(str::trim)
        .filter(|s| {
            s.to_ascii_lowercase().starts_with("http://")
                || s.to_ascii_lowercase().starts_with("https://")
        })
        .map(str::to_string)
        .collect();
    out.sort();
    out.dedup();
    out
}

pub(crate) fn format_bytes(value: f64) -> String {
    if value <= 0.0 {
        return "未知".to_string();
    }
    let units = ["B", "KB", "MB", "GB", "TB"];
    let mut current = value;
    let mut index = 0;
    while current >= 1024.0 && index < units.len() - 1 {
        current /= 1024.0;
        index += 1;
    }
    if index == 0 {
        format!("{} {}", current as u64, units[index])
    } else {
        format!("{:.1} {}", current, units[index])
    }
}

pub(crate) fn format_rate(value: f64) -> String {
    if value <= 0.0 {
        "尚未形成速率".to_string()
    } else if value >= 10.0 {
        format!("{:.0} / 分", value)
    } else {
        format!("{:.1} / 分", value)
    }
}

pub(crate) fn status_color(status: &str) -> egui::Color32 {
    match status {
        "completed" => egui::Color32::from_rgb(52, 211, 153),
        "failed" | "cancelled" => egui::Color32::from_rgb(248, 113, 113),
        "running" => egui::Color32::from_rgb(251, 191, 36),
        _ => egui::Color32::from_rgb(125, 211, 252),
    }
}

pub(crate) fn job_progress(job: &Value) -> f64 {
    if let Some(pct) = job["automation_summary"]["progress_percent"].as_f64()
    {
        return pct.clamp(0.0, 100.0);
    }
    let total = job["progress_total"].as_f64().unwrap_or(0.0);
    if total <= 0.0 {
        return 0.0;
    }
    (job["progress_current"].as_f64().unwrap_or(0.0) / total * 100.0)
        .clamp(0.0, 100.0)
}

pub(crate) fn auto_scan_label(status: &Value) -> String {
    if !status.is_object() {
        return "準備自動掃描".to_string();
    }
    match status["status"].as_str().unwrap_or("") {
        "scanning" => "自動掃描中…".to_string(),
        "completed" => "自動掃描已啟用".to_string(),
        "error" => "等待自動重試".to_string(),
        _ => "等待登入".to_string(),
    }
}

pub(crate) fn diagnostic_state_text(state: &str) -> &'static str {
    match state {
        "ready" => "可用",
        "running" => "執行中",
        "attention" => "需檢查",
        "setup" => "待設定",
        "waiting_login" => "等待登入",
        _ => "待命",
    }
}

pub(crate) fn platform_health_text(health: &str) -> &'static str {
    match health {
        "ready" => "可用",
        "running" => "掃描中",
        "attention" => "需檢查",
        "login_required" => "等待登入",
        _ => "待命",
    }
}

pub(crate) fn failure_category_text(category: &str) -> &str {
    match category {
        "destination" => "下載位置",
        "login" => "登入狀態",
        "network" => "網路",
        "media" => "媒體解析",
        "platform" => "平台版面",
        "cancelled" => "已取消",
        other => other,
    }
}

pub(crate) fn post_status_label(status: &str) -> &str {
    match status {
        "ready" => "可瀏覽",
        "no_media" => "無媒體",
        "error" => "索引失敗",
        _ => "已發現",
    }
}

pub(crate) fn post_media_summary(post: &Value) -> String {
    let media = post["media"].as_array();
    let (photos, videos) = media
        .map(|items| {
            items.iter().fold((0usize, 0usize), |(p, v), m| {
                match m["media_type"].as_str().unwrap_or("") {
                    "photo" => (p + 1, v),
                    "video" => (p, v + 1),
                    _ => (p, v),
                }
            })
        })
        .unwrap_or((0, 0));
    let mut parts = Vec::new();
    if photos > 0 {
        parts.push(format!("{photos} 張照片"));
    }
    if videos > 0 {
        parts.push(format!("{videos} 支影片"));
    }
    if parts.is_empty() {
        let count = post["downloadable_count"]
            .as_u64()
            .or_else(|| post["media_count"].as_u64())
            .unwrap_or(0);
        format!("{count} 個媒體")
    } else {
        parts.join("、")
    }
}

pub(crate) fn matches_account_search(account: &Value, query: &str) -> bool {
    if query.is_empty() {
        return true;
    }
    [
        "handle",
        "display_name",
        "platform",
        "profile_url",
    ]
    .iter()
    .filter_map(|key| account[key].as_str())
    .any(|v| v.to_lowercase().contains(query))
}

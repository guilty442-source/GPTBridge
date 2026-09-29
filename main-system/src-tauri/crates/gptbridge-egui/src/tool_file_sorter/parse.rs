//! tool_file_sorter/parse.rs — stdout/protocol parsing and pure helpers.

use serde_json::Value;

use super::{FOLDERS_JSON_PREFIX, PLAN_JSON_PREFIXES, PROFILES_JSON_PREFIXES};

pub(crate) fn parse_tool_json(stdout: &str) -> Option<Value> {
    let trimmed = stdout.trim();
    if trimmed.starts_with('{') || trimmed.starts_with('[') {
        if let Ok(v) = serde_json::from_str::<Value>(trimmed) {
            return Some(v);
        }
    }
    for line in stdout.lines() {
        let line = line.trim();
        if !(line.starts_with('{') || line.starts_with('[')) {
            continue;
        }
        if let Ok(v) = serde_json::from_str::<Value>(line) {
            return Some(v);
        }
    }
    None
}

pub(crate) fn parse_json_with_prefixes(stdout: &str, prefixes: &[&str]) -> Option<Value> {
    if let Some(v) = parse_tool_json(stdout) {
        return Some(v);
    }
    for prefix in prefixes {
        for line in stdout.lines() {
            if let Some(rest) = line.trim_start().strip_prefix(prefix) {
                if let Ok(v) = serde_json::from_str::<Value>(rest.trim()) {
                    if v.is_object() || v.is_array() {
                        return Some(v);
                    }
                }
            }
        }
    }
    None
}

pub(crate) fn parse_destination_folders(stdout: &str) -> Vec<String> {
    for line in stdout.lines() {
        if let Some(rest) = line.trim_start().strip_prefix(FOLDERS_JSON_PREFIX) {
            if let Ok(Value::Array(items)) = serde_json::from_str::<Value>(rest.trim())
            {
                let mut out: Vec<String> = items
                    .iter()
                    .filter_map(|v| v.as_str())
                    .map(str::trim)
                    .filter(|s| is_direct_child_folder_name(s))
                    .map(str::to_string)
                    .collect();
                out.sort();
                out.dedup();
                return out;
            }
        }
    }
    Vec::new()
}

pub(crate) fn is_direct_child_folder_name(value: &str) -> bool {
    let name = value.trim();
    !name.is_empty()
        && name != "."
        && name != ".."
        && !name.contains('/')
        && !name.contains('\\')
        && !name.contains(':')
}

pub(crate) fn normalize_path(value: &str) -> String {
    value
        .trim()
        .trim_end_matches(['\\', '/'])
        .replace('/', "\\")
        .to_lowercase()
}

pub(crate) fn parse_profile(stdout: &str, target_dir: &str) -> Option<Value> {
    let parsed = parse_json_with_prefixes(stdout, &PROFILES_JSON_PREFIXES)?;
    let profiles: Vec<Value> = if let Some(items) = parsed.as_array() {
        items.clone()
    } else if let Some(items) = parsed["profiles"].as_array() {
        items.clone()
    } else if parsed["profile"].is_object() {
        vec![parsed["profile"].clone()]
    } else {
        vec![parsed]
    };
    let normalized = normalize_path(target_dir);
    profiles.into_iter().find(|p| {
        let path = p["target_dir"]
            .as_str()
            .or_else(|| p["source_dir"].as_str())
            .or_else(|| p["path"].as_str())
            .unwrap_or("");
        normalize_path(path) == normalized
    })
}

pub(crate) fn parse_sort_plan(stdout: &str) -> Option<Value> {
    let parsed = parse_json_with_prefixes(stdout, &PLAN_JSON_PREFIXES)?;
    if parsed["plan"].is_object() {
        Some(parsed["plan"].clone())
    } else {
        Some(parsed)
    }
}

pub(crate) fn plan_id(plan: &Value) -> String {
    plan["plan_id"]
        .as_str()
        .or_else(|| plan["id"].as_str())
        .unwrap_or("")
        .trim()
        .to_string()
}

pub(crate) fn plan_actions(plan: &Value) -> Vec<Value> {
    if let Some(items) = plan["operations"].as_array() {
        items.clone()
    } else {
        plan["actions"].as_array().cloned().unwrap_or_default()
    }
}

pub(crate) fn plan_action_count(plan: &Value) -> usize {
    if let Some(items) = plan["operations"].as_array() {
        return items.len();
    }
    if let Some(items) = plan["actions"].as_array() {
        return items.len();
    }
    plan["action_count"]
        .as_u64()
        .or_else(|| plan["summary"]["ready"].as_u64())
        .unwrap_or(0) as usize
}

pub(crate) fn parse_keywords(value: &str) -> Vec<String> {
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

pub(crate) fn category_label(category: &str) -> &str {
    match category {
        "non_person_image_candidate" => "可能不含人物",
        "large_video_file" => "過大影片",
        "bad_video_file" => "影片問題",
        "similar_video_duplicate" => "相似影片",
        "similar_image_duplicate" => "相似圖片",
        other => other,
    }
}

pub(crate) fn format_file_size(size: f64) -> String {
    if size >= 1_073_741_824.0 {
        format!("{:.1} GB", size / 1_073_741_824.0)
    } else if size >= 1_048_576.0 {
        format!("{:.1} MB", size / 1_048_576.0)
    } else if size >= 1024.0 {
        format!("{:.1} KB", size / 1024.0)
    } else {
        format!("{} B", size as u64)
    }
}

pub(crate) fn cleanup_file_summary(file: &Value) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(cats) = file["categories"].as_array() {
        let labels: Vec<&str> = cats
            .iter()
            .filter_map(|c| c.as_str())
            .map(category_label)
            .collect();
        if !labels.is_empty() {
            parts.push(labels.join(" / "));
        }
    }
    if let Some(size) = file["size"].as_f64() {
        parts.push(format_file_size(size));
    }
    if let Some(sim) = file["video_similarity"].as_f64() {
        parts.push(format!("相似度 {:.0}%", sim));
    }
    if let Some(sim) = file["similar_to"].as_str() {
        parts.push(format!("相似於 {sim}"));
    }
    if let Some(issue) = file["video_issue"].as_str() {
        parts.push(format!("影片狀態 {issue}"));
    }
    if let (Some(w), Some(h)) = (file["width"].as_f64(), file["height"].as_f64()) {
        parts.push(format!("{}x{}", w as u64, h as u64));
    }
    if let Some(conf) = file["visual_recognition_confidence"].as_f64() {
        parts.push(format!("模型信心 {:.0}%", conf * 100.0));
    }
    if parts.is_empty() {
        "已列入清理候選".to_string()
    } else {
        parts.join(" - ")
    }
}

pub(crate) fn progress_text(progress: &Value) -> String {
    if let Some(msg) = progress["message"].as_str().filter(|m| !m.is_empty()) {
        return msg.to_string();
    }
    match progress["phase"].as_str().unwrap_or("") {
        "folder_scan" => format!(
            "掃描資料夾 {}",
            progress["current_folder"].as_str().unwrap_or("")
        ),
        "image_analysis" => format!(
            "分析圖片 {}",
            progress["current_file"].as_str().unwrap_or("")
        ),
        "video_analysis" => format!(
            "分析影片 {}",
            progress["current_file"].as_str().unwrap_or("")
        ),
        phase if !phase.is_empty() => phase.to_string(),
        _ => "掃描中".to_string(),
    }
}

pub(crate) fn format_run_output(result: &Value) -> String {
    let mut out = String::new();
    if let Some(stdout) = result["stdout"].as_str() {
        out.push_str(stdout.trim_end());
    }
    if let Some(stderr) = result["stderr"].as_str().filter(|s| !s.is_empty()) {
        if !out.is_empty() {
            out.push_str("\n\n");
        }
        out.push_str(stderr.trim_end());
    }
    out
}

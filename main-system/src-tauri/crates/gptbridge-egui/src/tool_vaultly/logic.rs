//! tool_vaultly/logic.rs — governed request plumbing, state application and job gates.

use std::collections::HashSet;

use serde_json::{json, Value};

use super::*;
use super::helpers::matches_account_search;

impl VaultlyWindow {
    pub(crate) fn request(
        &mut self,
        command: &str,
        payload: Value,
        action: VaultAction,
        context: &str,
    ) {
        match self.backend.send(command, payload) {
            Some(request_id) => {
                self.pending.insert(
                    request_id,
                    Pending {
                        action,
                        context: context.to_string(),
                    },
                );
            }
            None => {
                self.message = "後端尚未接收指令".to_string();
                self.busy_action.clear();
            }
        }
    }

    pub(crate) fn request_state(&mut self) {
        if self
            .pending
            .values()
            .any(|p| matches!(p.action, VaultAction::State))
        {
            return;
        }
        self.request(
            "vaultly_get_state",
            json!({
                "posts": {
                    "limit": POST_PAGE_SIZE,
                    "offset": self.post_page * POST_PAGE_SIZE,
                    "platform": self.post_platform_filter,
                    "status": self.post_status_filter,
                    "query": self.post_search.trim(),
                }
            }),
            VaultAction::State,
            "",
        );
    }

    pub(crate) fn run_action(
        &mut self,
        action_name: &str,
        command: &str,
        payload: Value,
        action: VaultAction,
        context: &str,
    ) {
        self.busy_action = action_name.to_string();
        self.request(command, payload, action, context);
    }

    pub(crate) fn apply_state(&mut self, state: &Value) {
        if let Some(items) = state["platforms"].as_array() {
            if !items.is_empty() {
                self.platforms = items.clone();
            }
        }
        self.accounts =
            state["accounts"].as_array().cloned().unwrap_or_default();
        self.filter_terms = state["filter_terms"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        self.removed_accounts = state["removed_accounts"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        self.auto_scan = state["auto_scan"].clone();
        self.jobs = state["jobs"].as_array().cloned().unwrap_or_default();
        self.post_scan_jobs = state["post_scan_jobs"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        self.download_automation = if state["download_automation"].is_object()
        {
            Some(state["download_automation"].clone())
        } else {
            None
        };
        self.destination_health = if state["destination_health"].is_object()
        {
            Some(state["destination_health"].clone())
        } else {
            None
        };
        self.diagnostics = if state["diagnostics"].is_object()
            && state["diagnostics"].get("state").is_some()
        {
            Some(state["diagnostics"].clone())
        } else {
            None
        };
        self.version = state["version"].as_str().unwrap_or("").to_string();
        self.posts = state["posts"].as_array().cloned().unwrap_or_default();
        self.posts_total = state["posts_total"]
            .as_i64()
            .unwrap_or(self.posts.len() as i64);
        if !self
            .posts
            .iter()
            .any(|p| p["post_id"].as_str() == Some(&self.selected_post_id))
        {
            self.selected_post_id = self
                .posts
                .first()
                .and_then(|p| p["post_id"].as_str())
                .unwrap_or("")
                .to_string();
        }
        if self.destination.is_empty() {
            self.destination =
                state["destination"].as_str().unwrap_or("").to_string();
        }
        self.paths = (
            state["workspace_path"].as_str().unwrap_or("").to_string(),
            state["database_path"].as_str().unwrap_or("").to_string(),
            state["browser_profile_path"].as_str().unwrap_or("").to_string(),
        );
        self.safety_notice =
            state["safety_notice"].as_str().unwrap_or("").to_string();
        if !self.selection_loaded {
            self.selection_loaded = true;
            self.selected_ids = self
                .accounts
                .iter()
                .filter(|a| a["selected"].as_bool() == Some(true))
                .filter_map(|a| a["account_id"].as_str().map(str::to_string))
                .collect();
        } else {
            let known: HashSet<String> = self
                .accounts
                .iter()
                .filter_map(|a| a["account_id"].as_str().map(str::to_string))
                .collect();
            self.selected_ids.retain(|id| known.contains(id));
        }
    }

    pub(crate) fn handle_result(&mut self, action: VaultAction, context: &str, payload: &Value) {
        let ok = payload["ok"].as_bool().unwrap_or(true);
        match action {
            VaultAction::State => {
                if !ok {
                    self.message = payload["message"]
                        .as_str()
                        .unwrap_or("載入下載中心失敗")
                        .to_string();
                    return;
                }
                self.apply_state(payload);
                self.message = "下載中心已就緒".to_string();
            }
            VaultAction::CheckDest => {
                self.destination_health =
                    if payload["destination_health"].is_object() {
                        Some(payload["destination_health"].clone())
                    } else {
                        None
                    };
            }
            _ => {
                if !ok {
                    self.message = payload["message"]
                        .as_str()
                        .unwrap_or("操作失敗")
                        .to_string();
                } else if matches!(action, VaultAction::ExportReport) {
                    let path =
                        payload["report_path"].as_str().unwrap_or("");
                    self.message = if path.is_empty() {
                        "診斷報告已匯出".to_string()
                    } else {
                        format!("診斷報告已匯出：{path}")
                    };
                } else {
                    self.message = payload["message"]
                        .as_str()
                        .unwrap_or("操作完成")
                        .to_string();
                }
                let _ = context;
                self.request_state();
            }
        }
        self.busy_action.clear();
    }

    pub(crate) fn handle_event(&mut self, event: &str, payload: &Value) {
        if !event.ends_with("_result") && event != "error" {
            return;
        }
        let request_id = payload["request_id"].as_str().unwrap_or("");
        let Some(pending) = self.pending.remove(request_id) else {
            return;
        };
        if event == "error" {
            self.message = payload["message"]
                .as_str()
                .unwrap_or("後端處理失敗。")
                .to_string();
            self.busy_action.clear();
            return;
        }
        self.handle_result(pending.action, &pending.context, payload);
    }

    pub(crate) fn check_destination(&mut self) {
        let trimmed = self.destination.trim().to_string();
        if trimmed.is_empty() {
            self.destination_health = None;
            return;
        }
        self.request(
            "vaultly_check_destination",
            json!({"path": trimmed}),
            VaultAction::CheckDest,
            "",
        );
    }

    pub(crate) fn media_types_or_message(&mut self) -> Option<Vec<&'static str>> {
        let types = self.conditions.media_types();
        if types.is_empty() {
            self.message = "請至少選擇照片或影片。".to_string();
            return None;
        }
        Some(types)
    }

    pub(crate) fn destination_gate(&mut self, preview_only: bool) -> bool {
        if preview_only {
            return true;
        }
        if self.destination.trim().is_empty() {
            self.message = "請先選擇下載資料夾。".to_string();
            return false;
        }
        if let Some(health) = &self.destination_health {
            if health["ok"].as_bool() != Some(true) {
                self.message = health["message"]
                    .as_str()
                    .unwrap_or("下載資料夾不可用")
                    .to_string();
                return false;
            }
        }
        true
    }

    pub(crate) fn create_link_job(&mut self, preview_only: bool, links: Vec<String>) {
        if links.is_empty() {
            self.message =
                "請先貼上 Instagram / X 的貼文、Reel、Story 或 status 連結。"
                    .to_string();
            return;
        }
        if !self.destination_gate(preview_only) {
            return;
        }
        if self.media_types_or_message().is_none() {
            return;
        }
        self.run_action(
            "link-job",
            "vaultly_create_link_job",
            json!({
                "links": links,
                "destination": self.destination.trim(),
                "preview_only": preview_only,
                "conditions": self.conditions.to_json(false),
            }),
            VaultAction::CreateLinkJob,
            "",
        );
    }

    pub(crate) fn create_job(&mut self, preview_only: bool) {
        if self.selected_ids.is_empty() {
            self.message = "請至少勾選一個追蹤帳號".to_string();
            return;
        }
        if !self.destination_gate(preview_only) {
            return;
        }
        if self.media_types_or_message().is_none() {
            return;
        }
        self.run_action(
            "job",
            "vaultly_create_job",
            json!({
                "account_ids": self.selected_ids.iter().collect::<Vec<_>>(),
                "destination": self.destination.trim(),
                "preview_only": preview_only,
                "conditions": self.conditions.to_json(true),
            }),
            VaultAction::CreateJob,
            "",
        );
    }
}

impl VaultlyWindow {
    /// Group accounts by platform with the web surface's ordering
    /// (selected first, then case-insensitive handle).
    pub(crate) fn grouped_accounts(&self) -> Vec<(Value, Vec<Value>, usize)> {
        let query = self.account_search.trim().to_lowercase();
        self.platforms
            .iter()
            .map(|platform| {
                let platform_id = platform["id"].as_str().unwrap_or("");
                let platform_accounts: Vec<Value> = self
                    .accounts
                    .iter()
                    .filter(|a| a["platform"].as_str() == Some(platform_id))
                    .cloned()
                    .collect();
                let total = platform_accounts.len();
                let mut visible: Vec<Value> = platform_accounts
                    .into_iter()
                    .filter(|a| matches_account_search(a, &query))
                    .collect();
                visible.sort_by(|left, right| {
                    let lid = left["account_id"].as_str().unwrap_or("");
                    let rid = right["account_id"].as_str().unwrap_or("");
                    self.selected_ids
                        .contains(rid)
                        .cmp(&self.selected_ids.contains(lid))
                        .then_with(|| {
                            left["handle"]
                                .as_str()
                                .unwrap_or("")
                                .to_lowercase()
                                .cmp(&right["handle"].as_str().unwrap_or("").to_lowercase())
                        })
                });
                (platform.clone(), visible, total)
            })
            .collect()
    }
}

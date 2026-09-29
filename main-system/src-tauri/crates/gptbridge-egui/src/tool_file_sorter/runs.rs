//! tool_file_sorter/runs.rs — cleanup runs, event routing and result handling.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use super::*;
use super::parse::*;

impl FileSorterWindow {
    pub(crate) fn request_stop_cleanup(&mut self) {
        if self.cleanup_state != RunState::Running || self.cleanup_stop_requested {
            return;
        }
        self.cleanup_stop_requested = true;
        self.cleanup_message = "正在停止清理掃描...".to_string();
        let Some(active) = self.active.as_ref() else { return };
        if active.kind != RunKind::Cleanup {
            return;
        }
        self.backend.send(
            "toolbox_cancel_tool_run",
            json!({
                "tool_id": TOOL_ID,
                "source": "tool_window",
                "request_id": active.request_id,
            }),
        );
    }

    pub(crate) fn build_cleanup_args(&self) -> Vec<String> {
        let mut args = vec![
            self.target_dir.trim().to_string(),
            "--cleanup-scan".to_string(),
            "--json".to_string(),
        ];
        if self.cleanup_image_issues {
            args.push("--image-cleanup".to_string());
        }
        if self.cleanup_similar_images {
            args.push("--similar-image-analysis".to_string());
        }
        if self.cleanup_video_issues {
            args.push("--video-cleanup".to_string());
        }
        if self.cleanup_similar_videos {
            args.push("--similar-video-analysis".to_string());
        }
        args.push("--similar-video-threshold".to_string());
        args.push(format!("{}", self.cleanup_threshold as u64));
        args.push("--analysis-speed".to_string());
        args.push(format!("{}", self.cleanup_speed as u64));
        args.push("--model-temperature".to_string());
        args.push(format!("{}", self.model_temperature));
        args.push("--model-top-p".to_string());
        args.push(format!("{}", self.model_top_p));
        args.push("--model-context-window".to_string());
        args.push(format!("{}", self.model_context_window));
        args.push("--model-max-output-tokens".to_string());
        args.push(format!("{}", self.model_max_tokens));
        if !self.cleanup_parallel {
            args.push("--no-parallel-analysis".to_string());
        }
        args
    }

    pub(crate) fn start_cleanup(&mut self) {
        if !self.can_run() {
            return;
        }
        self.cleanup_state = RunState::Running;
        self.cleanup_progress = Some(json!({
            "phase": "scan_started",
            "message": "正在掃描；完成後顯示完整結果",
        }));
        self.cleanup_files.clear();
        self.cleanup_output.clear();
        self.cleanup_stop_requested = false;
        self.enqueue(
            RunKind::Cleanup,
            self.build_cleanup_args(),
            RUN_TIMEOUT_S,
            "正在執行清理掃描...",
            "清理掃描完成",
        );
    }

    pub(crate) fn reveal_path(&self, raw: &str) {
        let path: PathBuf = if Path::new(raw).is_absolute() {
            PathBuf::from(raw)
        } else {
            Path::new(self.target_dir.trim()).join(raw)
        };
        // Explorer's /select only highlights existing files; fail closed
        // to revealing the parent when the entry vanished between scans.
        let arg = if path.is_file() {
            format!("/select,{}", path.display())
        } else {
            format!("/select,{}", path.parent().unwrap_or(&path).display())
        };
        let _ = std::process::Command::new("explorer.exe").arg(arg).spawn();
    }

    // ---- event handling -------------------------------------------------

    pub(crate) fn handle_event(&mut self, event: &str, payload: &Value) {
        let request_id = payload["request_id"].as_str().unwrap_or("");
        let matches_active = self
            .active
            .as_ref()
            .map(|a| a.request_id == request_id)
            .unwrap_or(false);
        match event {
            "toolbox_run_tool_progress" if matches_active => {
                if self.active.as_ref().map(|a| a.kind) == Some(RunKind::Cleanup) {
                    self.cleanup_progress = Some(payload.clone());
                    self.cleanup_message = progress_text(payload);
                }
            }
            "toolbox_run_tool_result" if matches_active => {
                let active = self.active.take().expect("matched");
                self.finish_run(active, payload);
            }
            "error" if matches_active => {
                let active = self.active.take().expect("matched");
                let message = payload["message"]
                    .as_str()
                    .unwrap_or("後端處理失敗。")
                    .to_string();
                self.finish_run_error(active, &message);
            }
            _ => {}
        }
    }

    pub(crate) fn finish_run_error(&mut self, active: ActiveRun, message: &str) {
        match active.kind {
            RunKind::Cleanup => {
                if self.cleanup_stop_requested {
                    self.cleanup_state = RunState::Idle;
                    self.cleanup_message = "清理掃描已停止".to_string();
                } else {
                    self.cleanup_state = RunState::Error;
                    self.cleanup_message = message.to_string();
                }
            }
            RunKind::ListFolders => {
                self.folder_scan_status = message.to_string();
            }
            RunKind::Profiles | RunKind::SelectScanTarget => {
                self.auto_organize_status = message.to_string();
            }
            RunKind::SetDuplicateTrash => {
                self.dup_trash_status = message.to_string();
            }
            RunKind::SetProfileEnabled => {
                self.auto_organize_status = message.to_string();
            }
            _ => {
                self.run_state = RunState::Error;
                self.message = message.to_string();
            }
        }
    }

    pub(crate) fn finish_run(&mut self, active: ActiveRun, payload: &Value) {
        let ok = payload["ok"].as_bool().unwrap_or(false);
        let cancelled = payload["cancelled"].as_bool().unwrap_or(false);
        let stdout = payload["stdout"].as_str().unwrap_or("").to_string();
        let message = payload["message"].as_str().unwrap_or("").to_string();
        let output = format_run_output(payload);
        if !ok {
            self.finish_run_error(
                active,
                if message.is_empty() {
                    "工具執行失敗"
                } else {
                    &message
                },
            );
            return;
        }
        match active.kind {
            RunKind::ListFolders => {
                let folders = parse_destination_folders(&stdout);
                if !folders.contains(&self.keyword_folder) {
                    self.keyword_folder.clear();
                }
                self.folder_scan_status = if folders.is_empty() {
                    "尚未找到可用目的地資料夾".to_string()
                } else {
                    format!("已找到 {} 個目的地資料夾", folders.len())
                };
                self.destination_folders = folders;
                self.folders_loaded = true;
            }
            RunKind::Profiles | RunKind::SelectScanTarget => {
                let target = self.target_dir.clone();
                if let Some(profile) = parse_profile(&stdout, &target) {
                    if let Some(enabled) = profile["enabled"].as_bool() {
                        self.apply_profile(&profile, enabled);
                    }
                } else {
                    self.migration_review = false;
                    self.auto_organize = false;
                    self.dup_trash = false;
                    self.auto_organize_status =
                        "此資料夾尚未啟用背景自動分類".to_string();
                    self.dup_trash_status =
                        "完全重複檔自動回收尚未啟用".to_string();
                }
                self.profile_loaded = true;
            }
            RunKind::SetProfileEnabled => {
                let enabled = active.desired.unwrap_or(false);
                self.auto_organize = enabled;
                if enabled {
                    self.migration_review = false;
                }
                self.auto_organize_status = if enabled {
                    "背景自動分類已啟用；即使關閉此工具視窗仍會持續監看"
                } else {
                    "自動分類已關閉"
                }
                .to_string();
            }
            RunKind::SetDuplicateTrash => {
                let enabled = active.desired.unwrap_or(false);
                self.dup_trash = enabled;
                self.dup_trash_status = if enabled {
                    "完全重複檔自動回收已啟用"
                } else {
                    "完全重複檔自動回收已關閉"
                }
                .to_string();
            }
            RunKind::Preview => {
                self.run_state = RunState::Success;
                self.message = "預覽計畫已建立".to_string();
                self.output = output;
                match parse_sort_plan(&stdout) {
                    Some(plan)
                        if plan["ok"].as_bool() != Some(false)
                            && !plan_id(&plan).is_empty() =>
                    {
                        self.append_history(
                            "預覽",
                            true,
                            format!(
                                "{} 個動作，plan {}",
                                plan_action_count(&plan),
                                plan_id(&plan)
                            ),
                        );
                        self.sort_plan = Some(plan);
                        self.plan_confirmed = false;
                    }
                    _ => {
                        self.run_state = RunState::Error;
                        self.message =
                            "後端未回傳可套用的 plan_id；沒有執行任何搬移"
                                .to_string();
                        self.append_history(
                            "預覽",
                            false,
                            "缺少可套用的 plan_id".to_string(),
                        );
                    }
                }
            }
            RunKind::ApplyPlan => {
                self.run_state = RunState::Success;
                self.message = "整理計畫已安全套用".to_string();
                self.output = output;
                self.append_history(
                    "套用計畫",
                    true,
                    format!("plan {}", plan_id(&self.sort_plan.clone().unwrap_or(Value::Null))),
                );
                self.sort_plan = None;
                self.plan_confirmed = false;
            }
            RunKind::Undo => {
                self.run_state = RunState::Success;
                self.message = "最近一次整理已復原".to_string();
                self.output = output;
                self.append_history("復原", true, "最近一次整理".to_string());
            }
            RunKind::History => {
                self.run_state = RunState::Success;
                self.message = "整理歷史已更新".to_string();
                self.history_output = if stdout.is_empty() {
                    message
                } else {
                    stdout
                };
            }
            RunKind::ListKeywords | RunKind::MutateKeywords => {
                self.run_state = RunState::Success;
                self.message = self.pending_labels.1.clone();
                self.output = output;
            }
            RunKind::Cleanup => {
                if cancelled || self.cleanup_stop_requested {
                    self.cleanup_state = RunState::Idle;
                    self.cleanup_message = "清理掃描已停止".to_string();
                    return;
                }
                match parse_tool_json(&stdout) {
                    Some(report) => {
                        let files = report["found_files"]
                            .as_array()
                            .cloned()
                            .unwrap_or_default();
                        let folder_count =
                            report["scan_folder_count"].as_u64().unwrap_or(0);
                        self.cleanup_progress = Some(json!({
                            "phase": "scan_completed",
                            "message": "清理掃描完成",
                            "folder_current": folder_count,
                            "folder_total": folder_count,
                            "source_file_count": report["source_file_count"],
                            "found_file_count": files.len(),
                        }));
                        self.cleanup_state = RunState::Success;
                        self.cleanup_message = if files.is_empty() {
                            "清理掃描完成，沒有找到候選項目".to_string()
                        } else {
                            format!(
                                "清理掃描完成，找到 {} 個候選項目",
                                files.len()
                            )
                        };
                        self.cleanup_files = files;
                        self.cleanup_output = output;
                    }
                    None => {
                        self.cleanup_state = RunState::Error;
                        self.cleanup_message =
                            "清理掃描回傳格式無法辨識".to_string();
                        self.cleanup_output = output;
                    }
                }
            }
        }
    }
}

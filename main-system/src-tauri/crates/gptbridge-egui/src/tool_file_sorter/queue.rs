//! tool_file_sorter/queue.rs — serial run queue and workspace/target state.

use std::path::Path;

use serde_json::json;

use super::*;

impl FileSorterWindow {
    pub(crate) fn enqueue(
        &mut self,
        kind: RunKind,
        args: Vec<String>,
        timeout_s: u64,
        running_label: &str,
        success_label: &str,
    ) {
        self.queue.push_back(QueuedRun {
            kind,
            args,
            timeout_s,
            running_label: running_label.to_string(),
            success_label: success_label.to_string(),
        });
    }

    /// Pop the next queued run once the socket is free — mirrors the
    /// serial ``queueRef`` chain of the retired web surface.
    pub(crate) fn pump_queue(&mut self) {
        if self.active.is_some() || !self.backend.connected() {
            return;
        }
        let Some(run) = self.queue.pop_front() else { return };
        let request_id = self
            .backend
            .send(
                "toolbox_run_tool",
                json!({
                    "tool_id": TOOL_ID,
                    "args": run.args,
                    "source": "tool_window",
                    "timeout_seconds": run.timeout_s,
                }),
            )
            .unwrap_or_default();
        if request_id.is_empty() {
            self.fail_run(&run, "後端尚未接收工具指令");
            return;
        }
        match run.kind {
            RunKind::Cleanup => {
                self.cleanup_state = RunState::Running;
                self.cleanup_message = run.running_label.clone();
            }
            _ => {
                self.run_state = RunState::Running;
                self.message = run.running_label.clone();
            }
        }
        let desired = match run.kind {
            RunKind::SetProfileEnabled | RunKind::SetDuplicateTrash => run
                .args
                .last()
                .map(|v| v == "true"),
            _ => None,
        };
        self.active = Some(ActiveRun {
            request_id,
            kind: run.kind,
            desired,
        });
        // Keep the labels for result handling.
        self.pending_labels = (run.running_label, run.success_label);
    }

    pub(crate) fn fail_run(&mut self, run: &QueuedRun, message: &str) {
        if run.kind == RunKind::Cleanup {
            self.cleanup_state = RunState::Error;
            self.cleanup_message = message.to_string();
        } else {
            self.run_state = RunState::Error;
            self.message = message.to_string();
        }
    }

    pub(crate) fn reset_workspace(&mut self) {
        self.target_validated = false;
        self.auto_organize = false;
        self.dup_trash = false;
        self.keyword_folder.clear();
        self.destination_folders.clear();
        self.folder_scan_status.clear();
        self.auto_organize_status.clear();
        self.dup_trash_status.clear();
        self.migration_review = false;
        self.folders_loaded = false;
        self.profile_loaded = false;
        self.run_state = RunState::Idle;
        self.message = "自動化檔案管理已就緒".to_string();
        self.output.clear();
        self.cleanup_state = RunState::Idle;
        self.cleanup_message = "清理掃描已併入此工具".to_string();
        self.cleanup_progress = None;
        self.cleanup_files.clear();
        self.cleanup_output.clear();
        self.sort_plan = None;
        self.plan_confirmed = false;
        self.history_open = false;
        self.history_output.clear();
    }

    /// Validate the typed target directory locally and start the
    /// follow-up profile/folder scans — replaces
    /// ``dialog:validate-folder`` + the web surface's debounced effects.
    pub(crate) fn validate_target(&mut self) {
        let candidate = self.target_dir.trim().to_string();
        if candidate.is_empty() || !Path::new(&candidate).is_dir() {
            self.target_validated = false;
            self.folder_scan_status = "目標資料夾不存在或無法存取".to_string();
            return;
        }
        if self.target_validated && candidate == self.target_dir {
            return;
        }
        self.target_dir = candidate.clone();
        self.reset_workspace();
        self.target_validated = true;
        self.enqueue(
            RunKind::SelectScanTarget,
            vec![candidate.clone(), "--select-scan-target".to_string()],
            SHORT_TIMEOUT_S,
            "正在切換掃描目標...",
            "掃描目標已更新",
        );
        self.enqueue(
            RunKind::Profiles,
            vec![candidate.clone(), "--profiles-json".to_string()],
            SHORT_TIMEOUT_S,
            "正在讀取自動分類設定...",
            "自動分類設定已更新",
        );
        if self.auto_scan_folders {
            self.enqueue_folder_scan();
        }
    }

    pub(crate) fn enqueue_folder_scan(&mut self) {
        let target = self.target_dir.trim().to_string();
        if target.is_empty() {
            return;
        }
        self.folder_scan_status = "正在掃描可用目的地資料夾...".to_string();
        self.enqueue(
            RunKind::ListFolders,
            vec![target, "--list-folders".to_string()],
            FOLDER_SCAN_TIMEOUT_S,
            "正在掃描目的地資料夾...",
            "目的地資料夾已更新",
        );
    }

    pub(crate) fn append_history(&mut self, action: &str, ok: bool, detail: String) {
        self.history_entries.insert(
            0,
            HistoryEntry {
                ok,
                action: action.to_string(),
                detail,
            },
        );
        self.history_entries.truncate(20);
    }
}

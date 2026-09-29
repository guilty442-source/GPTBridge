//! tool_file_sorter/profiles.rs — auto-organize profile and duplicate-trash toggles.

use serde_json::Value;

use super::*;

impl FileSorterWindow {
    pub(crate) fn apply_profile(&mut self, profile: &Value, enabled: bool) {
        let review = profile["migration_required_review"].as_bool() == Some(true);
        self.migration_review = review;
        self.auto_organize = enabled && !review;
        self.dup_trash = profile["duplicate_trash_enabled"].as_bool() == Some(true);
        self.dup_trash_status = if self.dup_trash {
            "完全重複檔自動回收已啟用"
        } else {
            "完全重複檔自動回收已關閉"
        }
        .to_string();
        self.auto_organize_status = if review {
            let rejected =
                profile["migration_rejected_rule_count"].as_u64().unwrap_or(0);
            if rejected > 0 {
                format!("舊規則已完整保留並隔離（{rejected} 筆需確認）；確認後才能重新啟用自動分類")
            } else {
                "舊規則已完整保留並隔離；確認後才能重新啟用自動分類".to_string()
            }
        } else if !enabled {
            "此資料夾的自動分類設定為關閉".to_string()
        } else {
            "背景自動分類已啟用；即使關閉此工具視窗仍會持續監看".to_string()
        };
    }

    pub(crate) fn set_profile_enabled(&mut self, enabled: bool) {
        if !self.backend.connected() || self.target_dir.trim().is_empty() {
            self.auto_organize_status =
                "後端連線後才能變更自動分類設定".to_string();
            return;
        }
        if enabled && self.migration_review && self.pending_confirm.is_none() {
            self.pending_confirm = Some((
                Confirm::EnableAutoOrganize,
                "舊版規則中有超出目前資料夾邊界的項目，原始資料已完整保留在隔離紀錄。確定要接受安全遷移結果並啟用自動分類嗎？"
                    .to_string(),
            ));
            return;
        }
        self.auto_organize_status = if enabled {
            "正在啟用背景自動分類..."
        } else {
            "正在關閉背景自動分類..."
        }
        .to_string();
        self.enqueue(
            RunKind::SetProfileEnabled,
            vec![
                self.target_dir.trim().to_string(),
                "--set-profile-enabled".to_string(),
                enabled.to_string(),
            ],
            SHORT_TIMEOUT_S,
            "正在變更自動分類設定...",
            "自動分類設定已更新",
        );
    }

    pub(crate) fn set_dup_trash(&mut self, enabled: bool) {
        if !self.backend.connected() || self.target_dir.trim().is_empty() {
            self.dup_trash_status = if self.target_dir.trim().is_empty() {
                "請先選擇目標資料夾".to_string()
            } else {
                "後端連線後才能變更重複檔回收設定".to_string()
            };
            return;
        }
        if enabled && self.pending_confirm.is_none() {
            self.pending_confirm = Some((
                Confirm::EnableDupTrash,
                "啟用後，系統只會將 SHA-256 完全相同且連續兩輪未變動的額外副本移至 Windows 資源回收筒。確定啟用嗎？"
                    .to_string(),
            ));
            return;
        }
        self.enqueue_dup_trash(enabled);
    }

    pub(crate) fn enqueue_dup_trash(&mut self, enabled: bool) {
        self.dup_trash_status = if enabled {
            "正在啟用完全重複檔自動回收..."
        } else {
            "正在關閉完全重複檔自動回收..."
        }
        .to_string();
        self.enqueue(
            RunKind::SetDuplicateTrash,
            vec![
                self.target_dir.trim().to_string(),
                "--set-duplicate-trash-enabled".to_string(),
                enabled.to_string(),
            ],
            SHORT_TIMEOUT_S,
            "正在變更重複檔回收設定...",
            "重複檔回收設定已更新",
        );
    }
}

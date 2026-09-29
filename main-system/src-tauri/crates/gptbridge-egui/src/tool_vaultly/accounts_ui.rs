//! tool_vaultly/accounts_ui.rs — tracked-account selection and filter-term panels.

use super::*;
use super::helpers::*;

impl VaultlyWindow {
    pub(crate) fn draw_platforms_accounts(&mut self, ui: &mut egui::Ui) {
        let busy = !self.busy_action.is_empty();
        // ---- platforms & accounts ------------------------------
        ui.heading("追蹤帳號");
        ui.horizontal(|ui| {
            ui.label("搜尋帳號");
            ui.add(
                egui::TextEdit::singleline(&mut self.account_search)
                    .desired_width(220.0),
            );
            if ui.button("全選可見").clicked() {
                for group in self.grouped_accounts() {
                    for account in group.1 {
                        if let Some(id) = account["account_id"].as_str() {
                            self.selected_ids.insert(id.to_string());
                        }
                    }
                }
            }
            if ui.button("清除可見").clicked() {
                for group in self.grouped_accounts() {
                    for account in group.1 {
                        if let Some(id) = account["account_id"].as_str() {
                            self.selected_ids.remove(id);
                        }
                    }
                }
            }
            if ui.button("全部取消").clicked() {
                self.selected_ids.clear();
            }
        });
        for (platform, accounts, total) in self.grouped_accounts() {
            let platform_id =
                platform["id"].as_str().unwrap_or("").to_string();
            let platform_name =
                platform["name"].as_str().unwrap_or(&platform_id).to_string();
            let scan_status = self.auto_scan[&platform_id].clone();
            egui::CollapsingHeader::new(format!(
                "{platform_name}（{} / {total}）",
                accounts.len()
            ))
            .default_open(true)
            .show(ui, |ui| {
                ui.label(
                    egui::RichText::new(format!(
                        "{} · {}",
                        auto_scan_label(&scan_status),
                        scan_status["message"].as_str().unwrap_or("等待服務啟動")
                    ))
                    .weak()
                    .small(),
                );
                ui.horizontal(|ui| {
                    if ui
                        .add_enabled(!busy, egui::Button::new("開啟登入頁"))
                        .clicked()
                    {
                        self.run_action(
                            "open",
                            "vaultly_open_platform",
                            json!({"platform": platform_id}),
                            VaultAction::OpenPlatform,
                            &platform_id,
                        );
                    }
                    if ui
                        .add_enabled(!busy, egui::Button::new("立即更新追蹤名單"))
                        .clicked()
                    {
                        self.run_action(
                            "scan",
                            "vaultly_scan_following",
                            json!({"platform": platform_id}),
                            VaultAction::ScanFollowing,
                            &platform_id,
                        );
                    }
                });
                for account in accounts {
                    let account_id = account["account_id"]
                        .as_str()
                        .unwrap_or("")
                        .to_string();
                    ui.horizontal(|ui| {
                        let mut selected =
                            self.selected_ids.contains(&account_id);
                        if ui
                            .checkbox(
                                &mut selected,
                                format!(
                                    "@{}  {}",
                                    account["handle"].as_str().unwrap_or(""),
                                    account["display_name"].as_str().unwrap_or("")
                                ),
                            )
                            .changed()
                        {
                            if selected {
                                self.selected_ids.insert(account_id.clone());
                            } else {
                                self.selected_ids.remove(&account_id);
                            }
                        }
                        if ui.small_button("移除").clicked() && !busy {
                            self.run_action(
                                "account-remove",
                                "vaultly_remove_accounts",
                                json!({"account_ids": [account_id]}),
                                VaultAction::RemoveAccounts,
                                "",
                            );
                        }
                    });
                }
            });
        }
        ui.horizontal(|ui| {
            ui.label(format!("已勾選 {} 個帳號", self.selected_ids.len()));
            if ui
                .add_enabled(!busy, egui::Button::new("儲存選取"))
                .clicked()
            {
                self.run_action(
                    "save",
                    "vaultly_save_selection",
                    json!({
                        "account_ids": self.selected_ids.iter().collect::<Vec<_>>()
                    }),
                    VaultAction::SaveSelection,
                    "",
                );
            }
            if ui
                .add_enabled(
                    !busy && !self.selected_ids.is_empty(),
                    egui::Button::new("掃描貼文建立索引"),
                )
                .clicked()
            {
                self.run_action(
                    "post-scan",
                    "vaultly_scan_posts",
                    json!({
                        "account_ids": self.selected_ids.iter().collect::<Vec<_>>(),
                        "limit_per_account": 12,
                    }),
                    VaultAction::ScanPosts,
                    "",
                );
            }
        });
        if !self.removed_accounts.is_empty() {
            egui::CollapsingHeader::new(format!(
                "已移除帳號（{}）",
                self.removed_accounts.len()
            ))
            .show(ui, |ui| {
                for account in &self.removed_accounts.clone() {
                    let account_id = account["account_id"]
                        .as_str()
                        .unwrap_or("")
                        .to_string();
                    ui.horizontal(|ui| {
                        ui.label(egui::RichText::new(format!(
                            "@{} · {}",
                            account["handle"].as_str().unwrap_or(""),
                            if account["source"].as_str() == Some("manual") {
                                "手動移除"
                            } else {
                                "自動篩選"
                            }
                        )).small());
                        if ui.small_button("還原").clicked() && !busy {
                            self.run_action(
                                "account-restore",
                                "vaultly_restore_accounts",
                                json!({"account_ids": [account_id]}),
                                VaultAction::RestoreAccounts,
                                "",
                            );
                        }
                    });
                }
            });
        }

    }

    pub(crate) fn draw_filter_terms(&mut self, ui: &mut egui::Ui) {
        let busy = !self.busy_action.is_empty();
        // ---- filter terms --------------------------------------
        ui.heading("篩選規則");
        ui.horizontal(|ui| {
            ui.add(
                egui::TextEdit::singleline(&mut self.filter_input)
                    .hint_text("要排除的帳號或關鍵字")
                    .desired_width(260.0),
            );
            if ui
                .add_enabled(!busy, egui::Button::new("新增篩選"))
                .clicked()
            {
                let terms = split_keywords(&self.filter_input);
                if terms.is_empty() {
                    self.message = "請輸入要排除的帳號或關鍵字".to_string();
                } else {
                    self.filter_input.clear();
                    self.run_action(
                        "filter-add",
                        "vaultly_add_filter_terms",
                        json!({"terms": terms}),
                        VaultAction::AddFilter,
                        "",
                    );
                }
            }
        });
        ui.horizontal_wrapped(|ui| {
            for term in self.filter_terms.clone() {
                if ui
                    .small_button(format!("{term} ✕"))
                    .clicked()
                    && !busy
                {
                    self.run_action(
                        "filter-remove",
                        "vaultly_remove_filter_terms",
                        json!({"terms": [term]}),
                        VaultAction::RemoveFilter,
                        "",
                    );
                }
            }
        });

    }
}

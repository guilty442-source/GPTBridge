//! tool_vaultly/posts_ui.rs — post library, diagnostics and footer panels.

use super::*;
use super::helpers::*;

impl VaultlyWindow {
    pub(crate) fn draw_post_library(&mut self, ui: &mut egui::Ui) {
        let busy = !self.busy_action.is_empty();
        // ---- post library --------------------------------------
        ui.heading("貼文庫");
        ui.horizontal(|ui| {
            ui.label("搜尋");
            let response = ui.add(
                egui::TextEdit::singleline(&mut self.post_search)
                    .desired_width(200.0),
            );
            if response.lost_focus() {
                self.post_page = 0;
                self.request_state();
            }
            egui::ComboBox::from_id_salt("post-platform")
                .selected_text(&self.post_platform_filter)
                .show_ui(ui, |ui| {
                    ui.selectable_value(
                        &mut self.post_platform_filter,
                        "all".to_string(),
                        "全部平台",
                    );
                    for (id, name) in DEFAULT_PLATFORMS {
                        ui.selectable_value(
                            &mut self.post_platform_filter,
                            id.to_string(),
                            *name,
                        );
                    }
                });
            egui::ComboBox::from_id_salt("post-status")
                .selected_text(&self.post_status_filter)
                .show_ui(ui, |ui| {
                    for (value, label) in [
                        ("all", "全部狀態"),
                        ("ready", "可瀏覽"),
                        ("discovered", "已發現"),
                        ("no_media", "無媒體"),
                        ("error", "索引失敗"),
                    ] {
                        ui.selectable_value(
                            &mut self.post_status_filter,
                            value.to_string(),
                            label,
                        );
                    }
                });
            if ui.button("重新整理").clicked() {
                self.post_page = 0;
                self.request_state();
            }
        });
        ui.horizontal(|ui| {
            let start = if self.posts_total == 0 {
                0
            } else {
                self.post_page * POST_PAGE_SIZE + 1
            };
            let end = (self.post_page * POST_PAGE_SIZE
                + self.posts.len() as i64)
                .min(self.posts_total);
            ui.label(
                egui::RichText::new(format!(
                    "{start}–{end} / {}",
                    self.posts_total
                ))
                .weak()
                .small(),
            );
            if ui
                .add_enabled(self.post_page > 0, egui::Button::new("上一頁"))
                .clicked()
            {
                self.post_page -= 1;
                self.request_state();
            }
            if ui
                .add_enabled(
                    (self.post_page + 1) * POST_PAGE_SIZE < self.posts_total,
                    egui::Button::new("下一頁"),
                )
                .clicked()
            {
                self.post_page += 1;
                self.request_state();
            }
            ui.checkbox(&mut self.multi_select_mode, "多選模式");
        });
        if self.posts.is_empty() {
            ui.label(egui::RichText::new("貼文庫為空；先掃描帳號建立索引。").weak().small());
        }
        for post in self.posts.clone() {
            let post_id = post["post_id"].as_str().unwrap_or("").to_string();
            let selected = self.selected_post_id == post_id;
            let status = post["scan_status"].as_str().unwrap_or("");
            let frame = egui::Frame::new()
                .inner_margin(egui::Margin::same(6))
                .stroke(if selected {
                    egui::Stroke::new(1.0, egui::Color32::from_rgb(125, 211, 252))
                } else {
                    ui.style().visuals.widgets.noninteractive.bg_stroke
                });
            frame.show(ui, |ui| {
                ui.horizontal(|ui| {
                    if self.multi_select_mode {
                        let mut checked = self.selected_post_id == post_id;
                        if ui.checkbox(&mut checked, "").changed() {
                            self.selected_post_id =
                                if checked { post_id.clone() } else { String::new() };
                        }
                    }
                    let badge = if post["media"]
                        .as_array()
                        .map(|m| m.iter().any(|i| i["media_type"].as_str() == Some("video")))
                        .unwrap_or(false)
                    {
                        "VIDEO"
                    } else {
                        "POST"
                    };
                    ui.label(egui::RichText::new(badge).monospace().small().weak());
                    if ui
                        .selectable_label(
                            selected,
                            format!(
                                "@{} · {}",
                                post["account_handle"].as_str().unwrap_or(""),
                                post_media_summary(&post)
                            ),
                        )
                        .clicked()
                    {
                        self.selected_post_id = post_id.clone();
                    }
                    ui.label(
                        egui::RichText::new(post_status_label(status)).weak().small(),
                    );
                });
                if selected {
                    if let Some(text) = post["text"].as_str() {
                        ui.label(egui::RichText::new(text).small());
                    }
                    ui.label(egui::RichText::new(format!(
                        "{} · 已下載 {}",
                        post["post_url"].as_str().unwrap_or(""),
                        post["downloaded_count"].as_u64().unwrap_or(0),
                    )).weak().small().monospace());
                    ui.horizontal(|ui| {
                        if ui
                            .add_enabled(!busy, egui::Button::new("預覽可下載項目"))
                            .clicked()
                        {
                            let url = post["post_url"].as_str().unwrap_or("").to_string();
                            self.create_link_job(true, vec![url]);
                        }
                        if ui
                            .add_enabled(!busy, egui::Button::new("下載此貼文"))
                            .clicked()
                        {
                            let url = post["post_url"].as_str().unwrap_or("").to_string();
                            self.create_link_job(false, vec![url]);
                        }
                    });
                }
            });
        }

    }

    pub(crate) fn draw_diagnostics(&mut self, ui: &mut egui::Ui) {
        let busy = !self.busy_action.is_empty();
        // ---- diagnostics ---------------------------------------
        if let Some(diag) = self.diagnostics.clone() {
            egui::CollapsingHeader::new("診斷").show(ui, |ui| {
                let state = diag["state"].as_str().unwrap_or("");
                ui.label(format!(
                    "狀態：{}",
                    diagnostic_state_text(state)
                ));
                if let Some(platforms) = diag["platforms"].as_array() {
                    for platform in platforms {
                        let health =
                            platform["health"].as_str().unwrap_or("");
                        ui.label(egui::RichText::new(format!(
                            "{} · {}{}",
                            platform["platform"].as_str().unwrap_or(""),
                            platform_health_text(health),
                            platform["message"]
                                .as_str()
                                .map(|m| format!(" · {m}"))
                                .unwrap_or_default()
                        )).weak().small());
                    }
                }
                if let Some(browser) = diag.get("browser") {
                    ui.label(egui::RichText::new(format!(
                        "瀏覽器：{}",
                        if browser["initialized"].as_bool() == Some(true) {
                            "已啟動"
                        } else {
                            "待啟動"
                        }
                    )).weak().small());
                }
                if let Some(categories) =
                    diag["failure_summary"]["categories"].as_object()
                {
                    for (category, count) in categories {
                        ui.label(egui::RichText::new(format!(
                            "{}：{}",
                            failure_category_text(category),
                            count
                        )).weak().small());
                    }
                }
                if let Some(latest) = diag["failure_summary"]["latest"].as_object() {
                    ui.label(egui::RichText::new(format!(
                        "最近失敗：{}",
                        latest["message"].as_str().unwrap_or("")
                    )).weak().small());
                }
                if ui
                    .add_enabled(!busy, egui::Button::new("匯出診斷報告"))
                    .clicked()
                {
                    self.run_action(
                        "diagnostic-report",
                        "vaultly_export_report",
                        json!({}),
                        VaultAction::ExportReport,
                        "",
                    );
                }
            });
        }

    }

    pub(crate) fn draw_footer(&mut self, ui: &mut egui::Ui) {
        ui.strong(&self.message);
        ui.label(
            egui::RichText::new(format!("模組：{}", self.paths.0))
                .weak()
                .small(),
        );
        ui.label(
            egui::RichText::new(format!("資料庫：{}", self.paths.1))
                .weak()
                .small(),
        );
        ui.label(
            egui::RichText::new(format!("登入資料：{}", self.paths.2))
                .weak()
                .small(),
        );
    }
}

//! tool_vaultly/ui.rs — eframe App shell, header, automation stats and download settings.

use std::time::Duration;

use super::*;
use super::helpers::*;
use crate::tool_window::ConnState;

impl eframe::App for VaultlyWindow {
    fn logic(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let was_disconnected = !self.backend.connected();
        let (batch, closed) = self.backend.tick(&self.cfg.ws_url);
        if closed {
            self.pending.clear();
            self.busy_action.clear();
        }
        if was_disconnected && self.backend.connected() {
            self.request_state();
            self.last_state_poll = Some(Instant::now());
        }
        for (event, payload) in batch {
            self.handle_event(&event, &payload);
        }
        if self.backend.connected() {
            let due = self
                .last_state_poll
                .map(|t| t.elapsed() >= STATE_POLL)
                .unwrap_or(true);
            if due {
                self.request_state();
                self.last_state_poll = Some(Instant::now());
            }
        }
        ctx.request_repaint_after(Duration::from_millis(250));
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.draw_header(ui);
        egui::CentralPanel::default().show(ui, |ui| {
            egui::ScrollArea::vertical()
                .auto_shrink([false; 2])
                .show(ui, |ui| {
                    self.draw_safety_notice(ui);
                    self.draw_automation_stats(ui);
                    self.draw_download_settings(ui);
                    ui.separator();
                    self.draw_platforms_accounts(ui);
                    ui.separator();
                    self.draw_filter_terms(ui);
                    ui.separator();
                    self.draw_queues(ui);
                    ui.separator();
                    self.draw_post_library(ui);
                    ui.separator();
                    self.draw_diagnostics(ui);
                    ui.separator();
                    self.draw_footer(ui);
                });
        });
    }
}

impl VaultlyWindow {
    pub(crate) fn draw_header(&mut self, ui: &mut egui::Ui) {
        let connected = self.backend.connected();
        let (dot, dot_color) = match self.backend.state {
            ConnState::Connected => ("● ", egui::Color32::from_rgb(96, 200, 128)),
            ConnState::Connecting => ("● ", egui::Color32::from_rgb(220, 180, 80)),
            ConnState::Disconnected => ("● ", egui::Color32::from_rgb(220, 96, 96)),
        };
        egui::Panel::top("header").show(ui, |ui| {
            ui.horizontal(|ui| {
                ui.vertical(|ui| {
                    ui.label(
                        egui::RichText::new("VAULTLY DOWNLOAD CENTER").weak().small(),
                    );
                    ui.heading("下載中心");
                });
                ui.with_layout(
                    egui::Layout::right_to_left(egui::Align::Center),
                    |ui| {
                        ui.label(if connected { "系統已連線" } else { "正在連線" });
                        ui.label(egui::RichText::new(dot).color(dot_color));
                        if !self.version.is_empty() {
                            ui.label(
                                egui::RichText::new(&self.version).weak().small(),
                            );
                        }
                    },
                );
            });
        });
    }

    pub(crate) fn draw_safety_notice(&mut self, ui: &mut egui::Ui) {
        if !self.safety_notice.is_empty() {
            egui::Frame::new()
                .fill(egui::Color32::from_rgb(8, 47, 73))
                .inner_margin(egui::Margin::same(8))
                .show(ui, |ui| {
                    ui.strong("正式版專屬瀏覽器工作階段");
                    ui.label(
                        egui::RichText::new(&self.safety_notice).small(),
                    );
                });
            ui.add_space(8.0);
        }
    }

    pub(crate) fn draw_automation_stats(&mut self, ui: &mut egui::Ui) {
        // ---- automation stats ----------------------------------
        ui.heading("自動化監控");
        let automation = self.download_automation.clone();
        let running = automation
            .as_ref()
            .and_then(|a| a["running_jobs"].as_u64())
            .unwrap_or_else(|| {
                self.jobs
                    .iter()
                    .filter(|j| j["status"].as_str() == Some("running"))
                    .count() as u64
            });
        let queued = automation
            .as_ref()
            .and_then(|a| a["queued_jobs"].as_u64())
            .unwrap_or_else(|| {
                self.jobs
                    .iter()
                    .filter(|j| j["status"].as_str() == Some("queued"))
                    .count() as u64
            });
        let scanning = automation
            .as_ref()
            .map(|a| {
                a["running_post_scans"].as_u64().unwrap_or(0)
                    + a["queued_post_scans"].as_u64().unwrap_or(0)
            })
            .unwrap_or(0);
        let downloaded = automation
            .as_ref()
            .and_then(|a| a["total_downloaded"].as_u64())
            .unwrap_or_else(|| {
                self.jobs
                    .iter()
                    .map(|j| j["downloaded"].as_u64().unwrap_or(0))
                    .sum()
            });
        let failed = automation
            .as_ref()
            .and_then(|a| a["total_failed"].as_u64())
            .unwrap_or_else(|| {
                self.jobs
                    .iter()
                    .map(|j| j["failed"].as_u64().unwrap_or(0))
                    .sum()
            });
        let success_rate = automation
            .as_ref()
            .and_then(|a| a["success_rate"].as_f64())
            .unwrap_or(0.0);
        ui.horizontal(|ui| {
            for (label, value) in [
                ("執行中", format!("{running}")),
                ("佇列", format!("{queued}")),
                ("掃描", format!("{scanning}")),
                ("已下載", format!("{downloaded}")),
                ("失敗", format!("{failed}")),
                ("成功率", format!("{:.0}%", success_rate)),
            ] {
                ui.vertical(|ui| {
                    ui.label(egui::RichText::new(label).weak().small());
                    ui.strong(value);
                });
                ui.separator();
            }
        });

        ui.add_space(8.0);
    }

    pub(crate) fn draw_download_settings(&mut self, ui: &mut egui::Ui) {
        let busy = !self.busy_action.is_empty();
        // ---- download settings ---------------------------------
        ui.heading("下載設定");
        ui.label(egui::RichText::new("下載資料夾").small());
        let editor = egui::TextEdit::singleline(&mut self.destination)
            .hint_text("貼上下載目的地路徑")
            .desired_width(f32::INFINITY);
        let response = ui.add_enabled(!busy, editor);
        if response.lost_focus() {
            self.check_destination();
        }
        if let Some(health) = &self.destination_health {
            let ok = health["ok"].as_bool() == Some(true);
            let detail = format!(
                "{} · {}",
                health["message"].as_str().unwrap_or(""),
                format_bytes(health["free_bytes"].as_f64().unwrap_or(0.0))
            );
            ui.colored_label(
                if ok {
                    egui::Color32::from_rgb(96, 200, 128)
                } else {
                    egui::Color32::from_rgb(220, 96, 96)
                },
                egui::RichText::new(detail).small(),
            );
        }
        ui.horizontal(|ui| {
            ui.checkbox(&mut self.conditions.photos, "照片");
            ui.checkbox(&mut self.conditions.videos, "影片");
            ui.checkbox(&mut self.conditions.skip_downloaded, "略過已下載");
        });
        ui.horizontal(|ui| {
            ui.label("日期起");
            ui.add(
                egui::TextEdit::singleline(&mut self.conditions.date_since)
                    .hint_text("YYYY-MM-DD")
                    .desired_width(110.0),
            );
            ui.label("迄");
            ui.add(
                egui::TextEdit::singleline(&mut self.conditions.date_until)
                    .hint_text("YYYY-MM-DD")
                    .desired_width(110.0),
            );
            ui.label("最低讚數");
            ui.add(egui::DragValue::new(&mut self.conditions.min_likes));
            ui.label("最低觀看");
            ui.add(egui::DragValue::new(&mut self.conditions.min_views));
            ui.label("每帳號上限");
            ui.add(
                egui::DragValue::new(&mut self.conditions.max_items_per_account)
                    .range(1..=200),
            );
        });
        ui.horizontal(|ui| {
            ui.label("包含關鍵字");
            ui.add(
                egui::TextEdit::singleline(&mut self.conditions.include_keywords)
                    .desired_width(220.0),
            );
            ui.label("排除關鍵字");
            ui.add(
                egui::TextEdit::singleline(&mut self.conditions.exclude_keywords)
                    .desired_width(220.0),
            );
        });
        ui.horizontal(|ui| {
            if ui
                .add_enabled(!busy, egui::Button::new("先預覽符合項目"))
                .clicked()
            {
                self.create_job(true);
            }
            if ui
                .add_enabled(!busy, egui::Button::new("開始背景自動下載"))
                .clicked()
            {
                self.create_job(false);
            }
        });
        ui.label(egui::RichText::new("快速連結（每行一個貼文 / Reel / Story / status URL）").small());
        ui.add_enabled(
            !busy,
            egui::TextEdit::multiline(&mut self.quick_links)
                .hint_text("https://…")
                .desired_width(f32::INFINITY)
                .desired_rows(3),
        );
        ui.horizontal(|ui| {
            if ui
                .add_enabled(!busy, egui::Button::new("預覽連結內容"))
                .clicked()
            {
                let links = split_links(&self.quick_links);
                self.create_link_job(true, links);
            }
            if ui
                .add_enabled(!busy, egui::Button::new("下載連結內容"))
                .clicked()
            {
                let links = split_links(&self.quick_links);
                self.create_link_job(false, links);
            }
        });

    }
}

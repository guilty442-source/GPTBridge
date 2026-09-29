//! tool_file_sorter/ui.rs — eframe App shell, confirm modal, header and workspace sections.

use std::time::Duration;

use super::*;
use crate::tool_window::ConnState;

impl eframe::App for FileSorterWindow {
    fn logic(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let (batch, closed) = self.backend.tick(&self.cfg.ws_url);
        for (event, payload) in batch {
            self.handle_event(&event, &payload);
        }
        if closed {
            if let Some(active) = self.active.take() {
                self.finish_run_error(active, "後端連線中斷，請重新送出指令。");
            }
        }
        self.pump_queue();
        ctx.request_repaint_after(Duration::from_millis(200));
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.draw_confirm_modal(ui);
        self.draw_header(ui);
        egui::CentralPanel::default().show(ui, |ui| {
            egui::ScrollArea::vertical()
                .auto_shrink([false; 2])
                .show(ui, |ui| {
                    self.draw_workspace(ui);
                    ui.separator();
                    self.draw_manual_sort(ui);
                    ui.separator();
                    self.draw_keyword_rules(ui);
                    ui.separator();
                    self.draw_cleanup(ui);
                });
        });
    }
}

impl FileSorterWindow {
    /// Confirm modal — replaces window.confirm for destructive or
    /// migration-acknowledging actions.
    pub(crate) fn draw_confirm_modal(&mut self, ui: &mut egui::Ui) {
        if let Some((confirm, text)) = self.pending_confirm.clone() {
            let mut open = true;
            egui::Window::new("確認操作")
                .collapsible(false)
                .resizable(false)
                .open(&mut open)
                .show(ui.ctx(), |ui| {
                    ui.label(text);
                    ui.add_space(8.0);
                    ui.horizontal(|ui| {
                        if ui.button("確認").clicked() {
                            match confirm {
                                Confirm::Undo => self.enqueue(
                                    RunKind::Undo,
                                    vec![
                                        self.target_dir.trim().to_string(),
                                        "--undo-last".to_string(),
                                    ],
                                    RUN_TIMEOUT_S,
                                    "正在復原最近一次整理...",
                                    "最近一次整理已復原",
                                ),
                                Confirm::EnableDupTrash => {
                                    self.enqueue_dup_trash(true);
                                }
                                Confirm::EnableAutoOrganize => {
                                    self.migration_review = false;
                                    self.set_profile_enabled(true);
                                }
                            }
                            self.pending_confirm = None;
                        }
                        if ui.button("取消").clicked() {
                            self.pending_confirm = None;
                        }
                    });
                });
            if !open {
                self.pending_confirm = None;
            }
        }
    }

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
                        egui::RichText::new("SMART FILE WORKSPACE").weak().small(),
                    );
                    ui.heading("自動化檔案管理");
                });
                ui.with_layout(
                    egui::Layout::right_to_left(egui::Align::Center),
                    |ui| {
                        ui.label(if connected { "系統已連線" } else { "正在連線" });
                        ui.label(egui::RichText::new(dot).color(dot_color));
                    },
                );
            });
            ui.horizontal(|ui| {
                ui.label(egui::RichText::new("目前工作區").weak().small());
                ui.label(
                    egui::RichText::new(if self.target_dir.trim().is_empty() {
                        "尚未選擇要整理的位置".to_string()
                    } else {
                        self.target_dir.clone()
                    })
                    .monospace()
                    .small(),
                );
            });
        });
    }

    pub(crate) fn draw_workspace(&mut self, ui: &mut egui::Ui) {
        let busy = self.busy();
        let can_run = self.can_run();
        ui.heading("工作區與自動分類");
        ui.label(
            "選定工作區後，自動分類會持續監看新檔案，不需要額外設定開關。",
        );
        ui.label(egui::RichText::new("目標資料夾").small());
        let editor = egui::TextEdit::singleline(&mut self.target_dir)
            .hint_text("貼上要管理的資料夾路徑")
            .desired_width(f32::INFINITY);
        let response = ui.add_enabled(!busy, editor);
        if response.lost_focus() {
            self.validate_target();
        }
        if !self.folder_scan_status.is_empty() {
            ui.label(egui::RichText::new(&self.folder_scan_status).weak().small());
        }
        ui.horizontal(|ui| {
            ui.label(if self.target_validated {
                "自動分類已納入流程"
            } else {
                "等待選擇工作區"
            });
        });
        // auto-organize toggle
        let mut auto_organize = self.auto_organize;
        if ui
            .checkbox(&mut auto_organize, "啟用背景自動分類")
            .changed()
            && can_run
        {
            self.set_profile_enabled(auto_organize);
        }
        if !self.auto_organize_status.is_empty() {
            ui.label(
                egui::RichText::new(&self.auto_organize_status)
                    .weak()
                    .small(),
            );
        }
        let mut dup_trash = self.dup_trash;
        if ui
            .checkbox(
                &mut dup_trash,
                "自動將完全重複檔移至 Windows 資源回收筒",
            )
            .changed()
            && can_run
        {
            self.set_dup_trash(dup_trash);
        }
        ui.label(
            egui::RichText::new(
                "預設關閉。只處理 SHA-256 完全相同、連續兩輪保持不變的副本；保留最早檔案，不會永久刪除。",
            )
            .weak()
            .small(),
        );
        if !self.dup_trash_status.is_empty() {
            ui.label(
                egui::RichText::new(&self.dup_trash_status).weak().small(),
            );
        }

    }
}

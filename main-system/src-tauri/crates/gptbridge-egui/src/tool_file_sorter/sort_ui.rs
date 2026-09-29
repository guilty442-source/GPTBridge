//! tool_file_sorter/sort_ui.rs — manual dry-run sort, plan apply and history panels.

use super::*;
use super::parse::{plan_action_count, plan_actions, plan_id};

impl FileSorterWindow {
    pub(crate) fn draw_manual_sort(&mut self, ui: &mut egui::Ui) {
        let can_run = self.can_run();
        // ---- manual sort ----------------------------------------
        ui.heading("安全整理工作流程");
        ui.label(
            egui::RichText::new(
                "手動整理只會先建立 dry-run 預覽；必須勾選確認後，才能套用同一個 plan_id。",
            )
            .weak()
            .small(),
        );
        ui.horizontal(|ui| {
            if ui
                .add_enabled(can_run, egui::Button::new("建立預覽（Dry run）"))
                .clicked()
            {
                self.sort_plan = None;
                self.plan_confirmed = false;
                self.enqueue(
                    RunKind::Preview,
                    vec![
                        self.target_dir.trim().to_string(),
                        "--preview-json".to_string(),
                    ],
                    SHORT_TIMEOUT_S,
                    "正在建立安全預覽...",
                    "預覽計畫已建立",
                );
            }
            if ui
                .add_enabled(can_run, egui::Button::new("復原上一次"))
                .clicked()
                && self.pending_confirm.is_none()
            {
                self.pending_confirm = Some((
                    Confirm::Undo,
                    "確定要復原最近一次已完成的檔案整理？".to_string(),
                ));
            }
            if ui
                .button(if self.history_open { "關閉歷史" } else { "整理歷史" })
                .clicked()
            {
                self.history_open = !self.history_open;
                if self.history_open && can_run {
                    self.enqueue(
                        RunKind::History,
                        vec![
                            self.target_dir.trim().to_string(),
                            "--history-json".to_string(),
                        ],
                        SHORT_TIMEOUT_S,
                        "正在讀取整理歷史...",
                        "整理歷史已更新",
                    );
                }
            }
        });
        ui.label(
            egui::RichText::new(&self.message)
                .weak()
                .small(),
        );
        if !self.output.is_empty() {
            egui::Frame::new()
                .fill(egui::Color32::from_gray(24))
                .inner_margin(egui::Margin::same(6))
                .show(ui, |ui| {
                    ui.label(
                        egui::RichText::new(&self.output)
                            .monospace()
                            .small(),
                    );
                });
        }

        // plan panel
        if let Some(plan) = self.sort_plan.clone() {
            let actions = plan_actions(&plan);
            let count = plan_action_count(&plan);
            egui::Frame::new()
                .stroke(ui.style().visuals.widgets.noninteractive.bg_stroke)
                .inner_margin(egui::Margin::same(8))
                .show(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.strong(format!("待套用計畫：{count} 個動作"));
                        ui.label(egui::RichText::new(plan_id(&plan)).weak().small());
                    });
                    if let Some(summary) = plan.get("summary") {
                        ui.label(egui::RichText::new(format!(
                            "可搬移 {} · 略過 {} · 未匹配 {} · 尚未穩定 {} · 已過濾 {}",
                            summary["ready"].as_u64().unwrap_or(count as u64),
                            summary["skipped"].as_u64().unwrap_or(0),
                            summary["unmatched"].as_u64().unwrap_or(0),
                            summary["unstable"].as_u64().unwrap_or(0),
                            summary["filtered"].as_u64().unwrap_or(0),
                        )).weak().small());
                    }
                    if let Some(warnings) = plan["warnings"].as_array() {
                        let joined: Vec<String> = warnings
                            .iter()
                            .filter_map(|w| w.as_str().map(str::to_string))
                            .collect();
                        if !joined.is_empty() {
                            ui.colored_label(
                                egui::Color32::from_rgb(251, 191, 36),
                                joined.join("；"),
                            );
                        }
                    }
                    if !actions.is_empty() {
                        egui::ScrollArea::vertical()
                            .max_height(220.0)
                            .show(ui, |ui| {
                                for (index, action) in actions.iter().take(50).enumerate() {
                                    let source = action["source"]
                                        .as_str()
                                        .map(str::to_string)
                                        .unwrap_or_else(|| format!("動作 {}", index + 1));
                                    let meta = action["destination"]
                                        .as_str()
                                        .or_else(|| action["status"].as_str())
                                        .or_else(|| action["reason"].as_str())
                                        .unwrap_or("");
                                    ui.horizontal(|ui| {
                                        ui.label(egui::RichText::new(source).monospace().small());
                                        ui.label(egui::RichText::new(meta).weak().small());
                                    });
                                }
                            });
                    }
                    let mut confirmed = self.plan_confirmed;
                    if ui
                        .checkbox(
                            &mut confirmed,
                            if count > 0 {
                                format!("我已檢查此計畫，確認套用 plan {}", plan_id(&plan))
                            } else {
                                "此計畫沒有可套用的搬移動作".to_string()
                            },
                        )
                        .changed()
                    {
                        self.plan_confirmed = confirmed;
                    }
                    if ui
                        .add_enabled(
                            can_run && self.plan_confirmed && count > 0,
                            egui::Button::new("確認並套用"),
                        )
                        .clicked()
                    {
                        let pid = plan_id(&plan);
                        self.enqueue(
                            RunKind::ApplyPlan,
                            vec![
                                self.target_dir.trim().to_string(),
                                "--apply-plan".to_string(),
                                pid,
                            ],
                            RUN_TIMEOUT_S,
                            "正在套用已確認的整理計畫...",
                            "整理計畫已安全套用",
                        );
                    }
                });
        }

        // history panel
        if self.history_open {
            egui::Frame::new()
                .stroke(ui.style().visuals.widgets.noninteractive.bg_stroke)
                .inner_margin(egui::Margin::same(8))
                .show(ui, |ui| {
                    ui.strong("最近操作");
                    if self.history_entries.is_empty() {
                        ui.label(
                            egui::RichText::new("本次視窗尚無操作記錄。")
                                .weak()
                                .small(),
                        );
                    } else {
                        for entry in &self.history_entries {
                            ui.horizontal(|ui| {
                                ui.label(egui::RichText::new(format!(
                                    "{} · {}",
                                    if entry.ok { "成功" } else { "失敗" },
                                    entry.action
                                )).small());
                                ui.label(egui::RichText::new(&entry.detail).weak().small());
                            });
                        }
                    }
                    if !self.history_output.is_empty() {
                        ui.label(
                            egui::RichText::new(&self.history_output)
                                .monospace()
                                .small(),
                        );
                    }
                });
        }
    }
}

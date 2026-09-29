//! tool_file_sorter/cleanup_ui.rs — read-only cleanup scan section.

use super::*;
use super::parse::{cleanup_file_summary, progress_text};

impl FileSorterWindow {
    pub(crate) fn draw_cleanup(&mut self, ui: &mut egui::Ui) {
        let can_run = self.can_run();
        // ---- cleanup scan ---------------------------------------
        ui.heading("清理掃描");
        ui.label(
            egui::RichText::new(
                "清理掃描屬於唯讀分析，不會搬移或刪除任何檔案；掃描完成後再逐一檢視候選項目。",
            )
            .weak()
            .small(),
        );
        ui.horizontal(|ui| {
            ui.checkbox(&mut self.cleanup_image_issues, "圖片問題");
            ui.checkbox(&mut self.cleanup_similar_images, "相似圖片");
            ui.checkbox(&mut self.cleanup_video_issues, "影片問題");
            ui.checkbox(&mut self.cleanup_similar_videos, "相似影片");
        });
        ui.horizontal(|ui| {
            ui.label("相似門檻");
            ui.add(egui::Slider::new(&mut self.cleanup_threshold, 50.0..=100.0));
            ui.label("分析速度");
            ui.add(egui::Slider::new(&mut self.cleanup_speed, 1.0..=100.0));
        });
        ui.horizontal(|ui| {
            ui.label("溫度");
            ui.add(egui::Slider::new(&mut self.model_temperature, 0.0..=2.0));
            ui.label("Top-p");
            ui.add(egui::Slider::new(&mut self.model_top_p, 0.0..=1.0));
        });
        ui.horizontal(|ui| {
            ui.label("上下文");
            ui.add(
                egui::DragValue::new(&mut self.model_context_window)
                    .range(512..=65536),
            );
            ui.label("輸出上限");
            ui.add(
                egui::DragValue::new(&mut self.model_max_tokens)
                    .range(32..=8192),
            );
            ui.checkbox(&mut self.cleanup_parallel, "平行分析");
        });
        let cleanup_enabled = self.cleanup_image_issues
            || self.cleanup_similar_images
            || self.cleanup_video_issues
            || self.cleanup_similar_videos;
        let can_cleanup = can_run && cleanup_enabled;
        ui.horizontal(|ui| {
            if self.cleanup_state == RunState::Running {
                if ui
                    .add_enabled(
                        !self.cleanup_stop_requested,
                        egui::Button::new("■ 停止掃描"),
                    )
                    .clicked()
                {
                    self.request_stop_cleanup();
                }
            } else if ui
                .add_enabled(can_cleanup, egui::Button::new("開始清理掃描"))
                .clicked()
            {
                self.start_cleanup();
            }
        });
        if let Some(progress) = self.cleanup_progress.clone() {
            let folder_total =
                progress["folder_total"].as_f64().unwrap_or(0.0);
            let folder_current =
                progress["folder_current"].as_f64().unwrap_or(0.0);
            let fraction = if folder_total > 0.0 {
                (folder_current / folder_total).min(1.0) as f32
            } else if self.cleanup_state == RunState::Success {
                1.0
            } else {
                0.0
            };
            ui.add(
                egui::ProgressBar::new(fraction)
                    .text(progress_text(&progress)),
            );
        }
        ui.label(egui::RichText::new(&self.cleanup_message).weak().small());
        if !self.cleanup_files.is_empty() {
            ui.strong(format!("候選項目（{}）", self.cleanup_files.len()));
            egui::ScrollArea::vertical()
                .max_height(260.0)
                .id_salt("cleanup-files")
                .show(ui, |ui| {
                    for file in self.cleanup_files.clone() {
                        let path = file["path"].as_str().unwrap_or("").to_string();
                        ui.horizontal(|ui| {
                            ui.label(
                                egui::RichText::new(&path).monospace().small(),
                            );
                            ui.label(
                                egui::RichText::new(cleanup_file_summary(&file))
                                    .weak()
                                    .small(),
                            );
                            if ui.small_button("顯示位置").clicked() {
                                self.reveal_path(&path);
                            }
                        });
                    }
                });
        }
        if !self.cleanup_output.is_empty() {
            egui::CollapsingHeader::new("掃描輸出").show(ui, |ui| {
                ui.label(
                    egui::RichText::new(&self.cleanup_output)
                        .monospace()
                        .small(),
                );
            });
        }
    }
}

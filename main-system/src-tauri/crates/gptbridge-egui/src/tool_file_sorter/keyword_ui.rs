//! tool_file_sorter/keyword_ui.rs — keyword classification rule editor.

use super::*;
use super::parse::{is_direct_child_folder_name, parse_keywords};

impl FileSorterWindow {
    pub(crate) fn draw_keyword_rules(&mut self, ui: &mut egui::Ui) {
        let busy = self.busy();
        let can_run = self.can_run();
        // ---- keyword rules --------------------------------------
        ui.heading("關鍵字分類規則");
        ui.label(
            egui::RichText::new(
                "輸入關鍵字，並從目前目標資料夾既有的第一層子資料夾中選擇目的地；分類不會離開此目標。",
            )
            .weak()
            .small(),
        );
        ui.label(egui::RichText::new("關鍵字").small());
        ui.add_enabled(
            !busy,
            egui::TextEdit::multiline(&mut self.keyword_input)
                .hint_text("idol, live, report")
                .desired_width(f32::INFINITY)
                .desired_rows(2),
        );
        ui.label(
            egui::RichText::new("分類目的地（目標內的第一層子資料夾）").small(),
        );
        ui.horizontal(|ui| {
            egui::ComboBox::from_id_salt("dest-folder")
                .selected_text(if self.keyword_folder.is_empty() {
                    "請先掃描並選擇目的地".to_string()
                } else {
                    self.keyword_folder.clone()
                })
                .show_ui(ui, |ui| {
                    for folder in self.destination_folders.clone() {
                        ui.selectable_value(
                            &mut self.keyword_folder,
                            folder.clone(),
                            folder,
                        );
                    }
                });
            if ui
                .add_enabled(can_run, egui::Button::new("掃描"))
                .clicked()
            {
                self.enqueue_folder_scan();
            }
            let mut auto_scan = self.auto_scan_folders;
            if ui
                .checkbox(&mut auto_scan, "自動掃描目的地資料夾")
                .changed()
            {
                self.auto_scan_folders = auto_scan;
                if auto_scan && self.target_validated {
                    self.enqueue_folder_scan();
                }
            }
        });
        let keywords = parse_keywords(&self.keyword_input);
        let has_dest = is_direct_child_folder_name(&self.keyword_folder)
            && self
                .destination_folders
                .contains(&self.keyword_folder.trim().to_string());
        ui.horizontal(|ui| {
            if ui
                .add_enabled(can_run, egui::Button::new("列出規則"))
                .clicked()
            {
                self.enqueue(
                    RunKind::ListKeywords,
                    vec![
                        self.target_dir.trim().to_string(),
                        "--list-keywords".to_string(),
                    ],
                    SHORT_TIMEOUT_S,
                    "正在讀取關鍵字規則...",
                    "關鍵字規則已讀取",
                );
            }
            if ui
                .add_enabled(
                    can_run && !keywords.is_empty() && has_dest,
                    egui::Button::new("新增/更新規則"),
                )
                .clicked()
            {
                let mut args = vec![self.target_dir.trim().to_string()];
                for keyword in keywords {
                    args.push("--upsert-keyword".to_string());
                    args.push(keyword);
                }
                args.push("--folder".to_string());
                args.push(self.keyword_folder.trim().to_string());
                self.enqueue(
                    RunKind::MutateKeywords,
                    args,
                    RUN_TIMEOUT_S,
                    "正在新增或更新關鍵字...",
                    "關鍵字規則已更新",
                );
            }
        });
        ui.label(egui::RichText::new("修改既有關鍵字").strong());
        ui.horizontal(|ui| {
            ui.add_enabled(
                !busy,
                egui::TextEdit::singleline(&mut self.current_keyword)
                    .hint_text("目前關鍵字")
                    .desired_width(160.0),
            );
            ui.add_enabled(
                !busy,
                egui::TextEdit::singleline(&mut self.updated_keyword)
                    .hint_text("新關鍵字")
                    .desired_width(160.0),
            );
            let enabled = can_run
                && !self.current_keyword.trim().is_empty()
                && !self.updated_keyword.trim().is_empty()
                && (self.keyword_folder.trim().is_empty() || has_dest);
            if ui
                .add_enabled(enabled, egui::Button::new("修改"))
                .clicked()
            {
                let mut args = vec![
                    self.target_dir.trim().to_string(),
                    "--update-keyword".to_string(),
                    self.current_keyword.trim().to_string(),
                    "--new-keyword".to_string(),
                    self.updated_keyword.trim().to_string(),
                ];
                if !self.keyword_folder.trim().is_empty() {
                    args.push("--folder".to_string());
                    args.push(self.keyword_folder.trim().to_string());
                }
                self.enqueue(
                    RunKind::MutateKeywords,
                    args,
                    RUN_TIMEOUT_S,
                    "正在修改關鍵字...",
                    "關鍵字規則已修改",
                );
            }
        });
    }
}

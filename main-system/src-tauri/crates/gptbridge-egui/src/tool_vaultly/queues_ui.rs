//! tool_vaultly/queues_ui.rs — download queue and post-scan job panels.

use super::*;
use super::helpers::*;

impl VaultlyWindow {
    pub(crate) fn draw_queues(&mut self, ui: &mut egui::Ui) {
        let busy = !self.busy_action.is_empty();
        // ---- queues --------------------------------------------
        ui.heading("下載佇列");
        if self.jobs.is_empty() {
            ui.label(egui::RichText::new("尚無工作").weak().small());
        }
        for job in self.jobs.clone() {
            let status = job["status"].as_str().unwrap_or("");
            let progress = job_progress(&job);
            egui::Frame::new()
                .stroke(ui.style().visuals.widgets.noninteractive.bg_stroke)
                .inner_margin(egui::Margin::same(6))
                .show(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.label(
                            egui::RichText::new("● ")
                                .color(status_color(status)),
                        );
                        ui.strong(format!(
                            "{} · {}",
                            if job["preview_only"].as_bool() == Some(true) {
                                "條件預覽"
                            } else {
                                "自動下載"
                            },
                            job["automation_summary"]["label"]
                                .as_str()
                                .unwrap_or(status)
                        ));
                        if matches!(status, "queued" | "running")
                            && ui.small_button("取消").clicked()
                        {
                            let job_id =
                                job["job_id"].as_str().unwrap_or("").to_string();
                            self.run_action(
                                "cancel",
                                "vaultly_cancel_job",
                                json!({"job_id": job_id}),
                                VaultAction::CancelJob,
                                &job_id,
                            );
                        }
                        if matches!(status, "completed" | "failed" | "cancelled")
                            && ui.small_button("重跑").clicked()
                            && !busy
                        {
                            let job_id =
                                job["job_id"].as_str().unwrap_or("").to_string();
                            self.run_action(
                                "retry",
                                "vaultly_retry_job",
                                json!({"job_id": job_id}),
                                VaultAction::RetryJob,
                                &job_id,
                            );
                        }
                    });
                    if let Some(message) = job["message"].as_str() {
                        ui.label(egui::RichText::new(message).weak().small());
                    }
                    ui.add(egui::ProgressBar::new(progress as f32 / 100.0));
                    ui.label(egui::RichText::new(format!(
                        "進度 {:.0}% · 帳號 {}/{} · 符合 {} · 成功 {} · 略過 {} · 失敗 {}",
                        progress,
                        job["progress_current"].as_u64().unwrap_or(0),
                        job["progress_total"].as_u64().unwrap_or(0),
                        job["matched"].as_u64().unwrap_or(0),
                        job["downloaded"].as_u64().unwrap_or(0),
                        job["skipped"].as_u64().unwrap_or(0),
                        job["failed"].as_u64().unwrap_or(0),
                    )).weak().small());
                    let rate = format_rate(
                        job["automation_summary"]["throughput_per_minute"]
                            .as_f64()
                            .unwrap_or(0.0),
                    );
                    let remaining =
                        job["automation_summary"]["remaining"].as_str().unwrap_or("");
                    ui.label(
                        egui::RichText::new(if remaining.is_empty() {
                            format!("速率 {rate}")
                        } else {
                            format!("速率 {rate} · 剩餘 {remaining}")
                        })
                        .weak()
                        .small(),
                    );
                });
        }
        for scan_job in self.post_scan_jobs.clone() {
            let status = scan_job["status"].as_str().unwrap_or("");
            if !matches!(status, "queued" | "running") {
                continue;
            }
            ui.horizontal(|ui| {
                ui.label(egui::RichText::new(format!(
                    "貼文掃描 {} · {}",
                    scan_job["scan_job_id"].as_str().unwrap_or(""),
                    status
                )).small());
                if ui.small_button("取消掃描").clicked() && !busy {
                    let scan_job_id = scan_job["scan_job_id"]
                        .as_str()
                        .unwrap_or("")
                        .to_string();
                    self.run_action(
                        "post-scan-cancel",
                        "vaultly_cancel_post_scan",
                        json!({"scan_job_id": scan_job_id}),
                        VaultAction::CancelPostScan,
                        &scan_job_id,
                    );
                }
            });
        }

    }
}

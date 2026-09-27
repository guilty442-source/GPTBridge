//! profiling.rs — perf-SLO panel.
//!
//! Renders the governed ``star-perf-baseline/v1`` evaluation produced by
//! gptbridge-core (``runtime/state/perf-baseline-latest.json``).  Fail-closed
//! semantics surface verbatim: unmeasurable fields stay null.

use gptbridge_core::native::paths;
use gptbridge_core::state::perf_slo;

pub struct ProfilingPanel;

impl ProfilingPanel {
    pub fn new() -> Self {
        Self
    }

    pub fn ui(&mut self, ui: &mut egui::Ui) {
        ui.heading("Profiling");
        ui.separator();

        let report = perf_slo::get_perf_slo(&paths::path_library().workspace_root);
        if report["available"].as_bool() != Some(true) {
            ui.label(
                egui::RichText::new(format!(
                    "SLO snapshot unavailable ({})",
                    report["error"].as_str().unwrap_or("unknown")
                ))
                .weak(),
            );
            return;
        }

        ui.label(format!(
            "baseline {} · captured {} · age {}s",
            report["baselineVersion"].as_str().unwrap_or("n/a"),
            report["capturedAt"].as_str().unwrap_or("n/a"),
            report["snapshotAgeS"]
                .as_i64()
                .map(|v| v.to_string())
                .unwrap_or_else(|| "n/a".to_string()),
        ));
        ui.separator();

        egui::Grid::new("slo_metrics").striped(true).show(ui, |ui| {
            ui.strong("metric");
            ui.strong("value");
            ui.strong("budget");
            ui.strong("status");
            ui.end_row();
            if let Some(list) = report["metrics"].as_array() {
                for m in list {
                    ui.label(m["key"].as_str().unwrap_or_default());
                    ui.label(
                        m["value"]
                            .as_f64()
                            .map(|v| format!("{v:.2} {}", m["unit"].as_str().unwrap_or("")))
                            .unwrap_or_else(|| "n/a".to_string()),
                    );
                    ui.label(
                        m["budget"]
                            .as_f64()
                            .map(|v| format!("{v:.2}"))
                            .unwrap_or_else(|| "—".to_string()),
                    );
                    let status = m["status"].as_str().unwrap_or("unknown");
                    let color = match status {
                        "ok" | "measured" => egui::Color32::from_rgb(0x7f, 0xc7, 0x7f),
                        "warn" => egui::Color32::from_rgb(0xe0, 0xaf, 0x68),
                        _ => egui::Color32::from_rgb(0xf7, 0x76, 0x6e),
                    };
                    ui.colored_label(color, status);
                    ui.end_row();
                }
            }
        });

        if let Some(list) = report["ipcPerCommand"].as_array() {
            if !list.is_empty() {
                ui.separator();
                ui.label("IPC p95 per command");
                egui::Grid::new("ipc_p95").striped(true).show(ui, |ui| {
                    for row in list.iter().take(16) {
                        ui.label(row["command"].as_str().unwrap_or_default());
                        ui.label(format!(
                            "{} ms (n={})",
                            row["p95_ms"]
                                .as_f64()
                                .map(|v| format!("{v:.1}"))
                                .unwrap_or_else(|| "n/a".to_string()),
                            row["samples"].as_f64().unwrap_or(0.0) as u64
                        ));
                        ui.end_row();
                    }
                });
            }
        }
    }
}

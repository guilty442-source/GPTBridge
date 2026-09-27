//! diagnostics.rs — live system + backend diagnostics panel.
//!
//! Sources: gptbridge-core metrics (sysinfo), lifecycle state, IPC gateway
//! probe.  Read-only.

use gptbridge_core::ipc;
use gptbridge_core::lifecycle;
use gptbridge_core::native::metrics;

pub struct DiagnosticsPanel;

impl DiagnosticsPanel {
    pub fn new() -> Self {
        Self
    }

    fn metric_row(ui: &mut egui::Ui, label: &str, value: String) {
        ui.horizontal(|ui| {
            ui.label(egui::RichText::new(label).weak().monospace());
            ui.label(egui::RichText::new(value).monospace());
        });
    }

    pub fn ui(&mut self, ui: &mut egui::Ui) {
        ui.heading("Diagnostics");
        ui.separator();

        let m = metrics::get_system_metrics();
        let fmt = |key: &str| {
            m[key]
                .as_f64()
                .map(|v| format!("{v:.1}"))
                .unwrap_or_else(|| "n/a".to_string())
        };
        let fmt_bytes = |key: &str| {
            m[key]
                .as_u64()
                .map(|v| format!("{:.1} GiB", v as f64 / 1_073_741_824.0))
                .unwrap_or_else(|| "n/a".to_string())
        };

        egui::CollapsingHeader::new("System metrics")
            .default_open(true)
            .show(ui, |ui| {
                Self::metric_row(ui, "cpu %", fmt("cpuUsagePercent"));
                Self::metric_row(ui, "ram %", fmt("ramUsagePercent"));
                Self::metric_row(ui, "ram total", fmt_bytes("ramTotalBytes"));
                Self::metric_row(ui, "disk %", fmt("diskUsagePercent"));
                Self::metric_row(ui, "disk root", fmt("diskRoot"));
            });

        let runtime = lifecycle::backend_runtime_info();
        egui::CollapsingHeader::new("Managed backend")
            .default_open(true)
            .show(ui, |ui| {
                Self::metric_row(
                    ui,
                    "status",
                    runtime["status"].as_str().unwrap_or("unknown").to_string(),
                );
                Self::metric_row(
                    ui,
                    "ready",
                    runtime["ready"].as_bool().unwrap_or(false).to_string(),
                );
                Self::metric_row(
                    ui,
                    "startup ms",
                    runtime["startupMs"]
                        .as_i64()
                        .map(|v| v.to_string())
                        .unwrap_or_else(|| "n/a".to_string()),
                );
                Self::metric_row(
                    ui,
                    "message",
                    runtime["message"].as_str().unwrap_or_default().to_string(),
                );
            });

        egui::CollapsingHeader::new("IPC gateway")
            .default_open(true)
            .show(ui, |ui| {
                Self::metric_row(ui, "alive", ipc::is_gateway_alive().to_string());
                Self::metric_row(
                    ui,
                    "resolved port",
                    ipc::resolve_backend_port().to_string(),
                );
            });
    }
}

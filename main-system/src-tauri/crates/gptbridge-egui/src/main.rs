//! gptbridge-egui — egui engineering console surface.
//!
//! Engineering-facing panels (Diagnostics / Profiling / Governance
//! Inspector / Engineering Console) rendered with egui/eframe.  All data
//! flows through gptbridge-core readers — this surface is read-only: it
//! never mutates governed state, never spawns, never connects directly.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod console;
mod diagnostics;
mod governance;
mod profiling;
mod tool_file_sorter;
mod tool_vaultly;
mod tool_window;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Panel {
    Diagnostics,
    Profiling,
    GovernanceInspector,
    EngineeringConsole,
}

struct ConsoleApp {
    panel: Panel,
    diagnostics: diagnostics::DiagnosticsPanel,
    profiling: profiling::ProfilingPanel,
    governance: governance::GovernanceInspectorPanel,
    console: console::EngineeringConsolePanel,
}

impl ConsoleApp {
    fn new() -> Self {
        Self {
            panel: Panel::Diagnostics,
            diagnostics: diagnostics::DiagnosticsPanel::new(),
            profiling: profiling::ProfilingPanel::new(),
            governance: governance::GovernanceInspectorPanel::new(),
            console: console::EngineeringConsolePanel::new(),
        }
    }
}

impl eframe::App for ConsoleApp {
    fn logic(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        // 2 s repaint cadence keeps the live panels fresh without a busy loop.
        ctx.request_repaint_after(std::time::Duration::from_secs(2));
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        egui::Panel::top("tabs").show(ui, |ui| {
            ui.horizontal(|ui| {
                ui.heading("GPTBridge");
                ui.separator();
                for (panel, label) in [
                    (Panel::Diagnostics, "Diagnostics"),
                    (Panel::Profiling, "Profiling"),
                    (Panel::GovernanceInspector, "Governance Inspector"),
                    (Panel::EngineeringConsole, "Engineering Console"),
                ] {
                    if ui.selectable_label(self.panel == panel, label).clicked() {
                        self.panel = panel;
                    }
                }
            });
        });
        egui::CentralPanel::default().show(ui, |ui| match self.panel {
            Panel::Diagnostics => self.diagnostics.ui(ui),
            Panel::Profiling => self.profiling.ui(ui),
            Panel::GovernanceInspector => self.governance.ui(ui),
            Panel::EngineeringConsole => self.console.ui(ui),
        });
    }
}

fn is_tool_window_mode() -> bool {
    std::env::args().any(|a| a == "--tool-window")
}

fn main() -> eframe::Result<()> {
    // Governed tool-window mode: `gptbridge-egui.exe --tool-window
    // --tool-id=<id>` replaces the retired WebView2 renderer for tools
    // with a registered native surface.
    if is_tool_window_mode() {
        let cfg = match tool_window::ToolWindowConfig::from_env() {
            Some(cfg) => cfg,
            None => {
                eprintln!("CONFIG_INVALID:tool-window-environment");
                std::process::exit(2);
            }
        };
        return tool_window::run_tool_window(cfg);
    }

    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default().with_inner_size([1100.0, 720.0]),
        ..Default::default()
    };
    eframe::run_native(
        "GPTBridge · Engineering Console",
        options,
        Box::new(|_cc| Ok(Box::new(ConsoleApp::new()))),
    )
}

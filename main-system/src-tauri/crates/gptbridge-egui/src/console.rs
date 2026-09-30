//! console.rs — Engineering Console panel.
//!
//! Read-only engineering facts: resolved path library, IPC state root,
//! workspace identity, embedded-worker state files.  No command execution —
//! this console observes, it does not operate.

use gptbridge_core::native::paths;
use gptbridge_core::security::token;

pub struct EngineeringConsolePanel;

impl EngineeringConsolePanel {
    pub fn new() -> Self {
        Self
    }

    fn row(ui: &mut egui::Ui, key: &str, value: String) {
        ui.horizontal(|ui| {
            ui.label(egui::RichText::new(key).weak().monospace());
            ui.label(egui::RichText::new(value).monospace());
        });
    }

    pub fn ui(&mut self, ui: &mut egui::Ui) {
        ui.heading("Engineering Console");
        ui.separator();

        let lib = paths::path_library();
        egui::CollapsingHeader::new("Path library")
            .default_open(true)
            .show(ui, |ui| {
                Self::row(ui, "mode", lib.mode.to_string());
                Self::row(
                    ui,
                    "workspace_root",
                    lib.workspace_root.display().to_string(),
                );
                Self::row(
                    ui,
                    "resources_root",
                    lib.resources_root.display().to_string(),
                );
                Self::row(ui, "app_root", lib.app_root.display().to_string());
                Self::row(
                    ui,
                    "renderer_entry",
                    lib.renderer_entry_html.display().to_string(),
                );
            });

        egui::CollapsingHeader::new("Identity")
            .default_open(true)
            .show(ui, |ui| {
                Self::row(ui, "workspace_instance_id", token::workspace_instance_id());
                Self::row(
                    ui,
                    "ipc_state_root",
                    token::ipc_state_root().display().to_string(),
                );
                Self::row(
                    ui,
                    "session token",
                    match token::backend_session_token() {
                        Ok(t) => format!("{}… ({} hex)", &t[..8.min(t.len())], t.len()),
                        Err(e) => format!("unavailable: {e}"),
                    },
                );
            });

        egui::CollapsingHeader::new("Environment")
            .default_open(false)
            .show(ui, |ui| {
                for key in [
                    "GPTBRIDGE_MANAGE_BACKEND",
                    "GPTBRIDGE_PROJECT_ROOT",
                    "GPTBRIDGE_RENDERER_DEV_URL",
                    "GPTBRIDGE_IPC_STATE_ROOT",
                ] {
                    Self::row(
                        ui,
                        key,
                        std::env::var(key).unwrap_or_else(|_| "<unset>".to_string()),
                    );
                }
            });
    }
}

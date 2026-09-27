//! governance.rs — Governance Inspector panel.
//!
//! Read-only view over the governed runtime state store
//! (``main-system/runtime/state/*.json``): file inventory, sizes, mtimes —
//! enough to eyeball which governed subsystems reported recently without
//! opening databases.

use std::fs;

use gptbridge_core::native::paths;

struct StateFile {
    name: String,
    bytes: u64,
    mtime_ms: i64,
}

pub struct GovernanceInspectorPanel;

impl GovernanceInspectorPanel {
    pub fn new() -> Self {
        Self
    }

    fn state_dir() -> std::path::PathBuf {
        paths::path_library()
            .workspace_root
            .join("main-system")
            .join("runtime")
            .join("state")
    }

    fn inventory() -> Vec<StateFile> {
        let mut out = Vec::new();
        if let Ok(entries) = fs::read_dir(Self::state_dir()) {
            for entry in entries.flatten() {
                let path = entry.path();
                if !path.is_file() {
                    continue;
                }
                let meta = match entry.metadata() {
                    Ok(m) => m,
                    Err(_) => continue,
                };
                out.push(StateFile {
                    name: entry.file_name().to_string_lossy().to_string(),
                    bytes: meta.len(),
                    mtime_ms: meta
                        .modified()
                        .ok()
                        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                        .map(|d| d.as_millis() as i64)
                        .unwrap_or(0),
                });
            }
        }
        out.sort_by(|a, b| b.mtime_ms.cmp(&a.mtime_ms));
        out
    }

    pub fn ui(&mut self, ui: &mut egui::Ui) {
        ui.heading("Governance Inspector");
        ui.separator();
        ui.label(
            egui::RichText::new(format!("state dir: {}", Self::state_dir().display()))
                .weak()
                .monospace(),
        );
        ui.separator();

        let files = Self::inventory();
        ui.label(format!("{} state files", files.len()));
        egui::Grid::new("state_files").striped(true).show(ui, |ui| {
            ui.strong("file");
            ui.strong("bytes");
            ui.strong("mtime (epoch ms)");
            ui.end_row();
            for f in files.iter().take(64) {
                ui.label(egui::RichText::new(&f.name).monospace());
                ui.label(f.bytes.to_string());
                ui.label(f.mtime_ms.to_string());
                ui.end_row();
            }
        });
    }
}

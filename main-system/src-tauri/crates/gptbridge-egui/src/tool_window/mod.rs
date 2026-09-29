//! tool_window — native egui tool-window surface.
//!
//! E180/C116 native-UI migration, increment 1: ``model-dialogue``
//! (star-chat).  The window speaks the governed tool backend protocol
//! directly — JSON text frames over ``ws://127.0.0.1`` through
//! ``gptbridge_core::ipc::ws`` — replacing the retired React/WebView2
//! renderer for this tool.  The launch contract is identical to the
//! Tauri tool window: ``--tool-window --tool-id=<id>`` plus the
//! ``GPTBRIDGE_SOURCE_UI_*`` environment block.

mod backend;
mod star_chat;

pub(crate) use backend::{Backend, ConnState};
pub use star_chat::StarChatWindow;

pub struct ToolWindowConfig {
    pub tool_id: String,
    pub title: String,
    pub ws_url: String,
    pub width: f32,
    pub height: f32,
}

impl ToolWindowConfig {
    /// Read the governed tool-window environment contract.  Returns
    /// ``None`` when required fields are absent or non-loopback.
    pub fn from_env() -> Option<Self> {
        let tool_id = std::env::var("GPTBRIDGE_SOURCE_UI_TOOL_ID")
            .ok()?
            .trim()
            .to_string();
        let ws_url = std::env::var("GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL")
            .ok()?
            .trim()
            .to_string();
        if tool_id.is_empty() || !ws_url.starts_with("ws://127.0.0.1:") {
            return None;
        }
        let env_f32 = |key: &str, default: f32| {
            std::env::var(key)
                .ok()
                .and_then(|v| v.trim().parse::<f32>().ok())
                .filter(|v| *v >= 240.0)
                .unwrap_or(default)
        };
        Some(Self {
            tool_id,
            title: std::env::var("GPTBRIDGE_SOURCE_UI_TITLE")
                .ok()
                .filter(|v| !v.trim().is_empty())
                .unwrap_or_else(|| "GPTBridge Tool".to_string()),
            ws_url,
            width: env_f32("GPTBRIDGE_SOURCE_UI_WIDTH", 900.0),
            height: env_f32("GPTBRIDGE_SOURCE_UI_HEIGHT", 720.0),
        })
    }
}

/// Run the native tool window for a tool id (``--tool-window`` mode).
pub fn run_tool_window(cfg: ToolWindowConfig) -> eframe::Result<()> {
    let title = format!("GPTBridge · {}", cfg.title);
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([cfg.width, cfg.height])
            .with_min_inner_size([720.0, 560.0]),
        ..Default::default()
    };
    match cfg.tool_id.as_str() {
        "model-dialogue" | "star-chat" => {
            let app = StarChatWindow::new(cfg);
            eframe::run_native(&title, options, Box::new(|_cc| Ok(Box::new(app))))
        }
        "file-sorter" => {
            let app = crate::tool_file_sorter::FileSorterWindow::new(cfg);
            eframe::run_native(&title, options, Box::new(|_cc| Ok(Box::new(app))))
        }
        "vaultly" => {
            let app = crate::tool_vaultly::VaultlyWindow::new(cfg);
            eframe::run_native(&title, options, Box::new(|_cc| Ok(Box::new(app))))
        }
        other => {
            eprintln!("NATIVE_UI_UNSUPPORTED:{other}");
            std::process::exit(2);
        }
    }
}

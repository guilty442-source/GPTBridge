//! gptbridge-gpui — GPUI native views surface.
//!
//! Hosts the GPU-accelerated native views of the UI stack:
//!   --surface model-dialogue    Model Dialogue (conversation + streaming)
//!   --surface coding-workspace  Coding Workspace (file tree + editor pane)
//!   --surface streaming-text    Streaming Text (token-stream demo view)
//!   --surface native-views      High-performance Native Views gallery
//!
//! State flows through gptbridge-core (backend session discovery, runtime
//! state readers); this crate never owns domain state — it renders
//! projections delivered by the governed information channel.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod coding_workspace;
mod model_dialogue;
mod native_views;
mod streaming_text;

use gpui::{px, size, AppContext, Application, Bounds, WindowBounds, WindowOptions};

use coding_workspace::CodingWorkspaceView;
use model_dialogue::ModelDialogueView;
use native_views::NativeViewsGallery;
use streaming_text::StreamingTextView;

fn surface_arg() -> String {
    let args: Vec<String> = std::env::args().collect();
    args.iter()
        .position(|a| a == "--surface")
        .and_then(|i| args.get(i + 1))
        .cloned()
        .unwrap_or_else(|| "model-dialogue".to_string())
}

fn main() {
    let surface = surface_arg();
    let title: gpui::SharedString = format!("GPTBridge · {surface}").into();

    Application::new().run(move |cx| {
        let bounds = Bounds::centered(None, size(px(960.0), px(640.0)), cx);
        let options = WindowOptions {
            window_bounds: Some(WindowBounds::Windowed(bounds)),
            titlebar: Some(gpui::TitlebarOptions {
                title: Some(title.clone()),
                ..Default::default()
            }),
            ..Default::default()
        };
        match surface.as_str() {
            "coding-workspace" => {
                cx.open_window(options, |window, cx| {
                    cx.new(|cx| CodingWorkspaceView::new(window, cx))
                })
                .expect("coding-workspace window");
            }
            "streaming-text" => {
                cx.open_window(options, |window, cx| {
                    cx.new(|cx| StreamingTextView::new(window, cx))
                })
                .expect("streaming-text window");
            }
            "native-views" => {
                cx.open_window(options, |window, cx| {
                    cx.new(|cx| NativeViewsGallery::new(window, cx))
                })
                .expect("native-views window");
            }
            _ => {
                cx.open_window(options, |window, cx| {
                    cx.new(|cx| ModelDialogueView::new(window, cx))
                })
                .expect("model-dialogue window");
            }
        }
        cx.activate(true);
    });
}

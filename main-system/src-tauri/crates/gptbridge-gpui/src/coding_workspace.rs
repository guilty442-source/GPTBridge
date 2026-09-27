//! coding_workspace.rs — GPUI Coding Workspace view.
//!
//! Two-pane workspace: workspace file listing on the left, focused-file
//! preview on the right.  The listing is a read-only projection of the
//! governed workspace root (gptbridge-core path library) — never a write
//! surface.

use std::path::PathBuf;

use gpui::{Context, IntoElement, Render, ScrollHandle, Window, div, prelude::*, px, rgb};

pub struct CodingWorkspaceView {
    root: PathBuf,
    entries: Vec<String>,
    selected: Option<usize>,
    preview: String,
    files_scroll: ScrollHandle,
    code_scroll: ScrollHandle,
}

impl CodingWorkspaceView {
    pub fn new(_window: &mut Window, _cx: &mut Context<Self>) -> Self {
        let root = gptbridge_core::native::paths::path_library().workspace_root.clone();
        let mut entries = Vec::new();
        // Bounded listing: top-level dirs only, capped — this is a
        // navigation aid, not a filesystem index.
        if let Ok(read) = std::fs::read_dir(&root) {
            for entry in read.flatten().take(128) {
                let name = entry.file_name().to_string_lossy().to_string();
                let kind = if entry.path().is_dir() { "/" } else { "" };
                entries.push(format!("{name}{kind}"));
            }
        }
        entries.sort();
        Self {
            root,
            entries,
            selected: None,
            preview: "Select a workspace entry.".to_string(),
            files_scroll: ScrollHandle::new(),
            code_scroll: ScrollHandle::new(),
        }
    }

    fn render_entry(&self, index: usize, name: &str) -> impl IntoElement {
        let active = self.selected == Some(index);
        div()
            .px_3()
            .py_1()
            .rounded_sm()
            .text_sm()
            .text_color(if active { rgb(0xe6e9ef) } else { rgb(0x8a94a6) })
            .when(active, |d| d.bg(rgb(0x243244)))
            .child(name.to_string())
    }
}

impl Render for CodingWorkspaceView {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .flex()
            .flex_col()
            .size_full()
            .bg(rgb(0x14181f))
            .child(
                div()
                    .px_4()
                    .py_2()
                    .border_b_1()
                    .border_color(rgb(0x2a3140))
                    .text_sm()
                    .text_color(rgb(0xaeb7c6))
                    .child(format!("GPTBridge · Coding Workspace — {}", self.root.display())),
            )
            .child(
                div()
                    .flex()
                    .flex_1()
                    .child(
                        div()
                            .id("files")
                            .w(px(260.0))
                            .border_r_1()
                            .border_color(rgb(0x2a3140))
                            .p_2()
                            .overflow_y_scroll()
                            .track_scroll(&self.files_scroll)
                            .children(
                                self.entries
                                    .iter()
                                    .enumerate()
                                    .map(|(i, e)| self.render_entry(i, e)),
                            ),
                    )
                    .child(
                        div()
                            .id("code")
                            .flex_1()
                            .p_4()
                            .overflow_y_scroll()
                            .track_scroll(&self.code_scroll)
                            .text_sm()
                            .text_color(rgb(0xd6dbe4))
                            .child(self.preview.clone()),
                    ),
            )
    }
}

//! model_dialogue.rs — GPUI Model Dialogue view.
//!
//! Conversation surface for the local model: scrollable turn list with a
//! streaming assistant turn, plus a composer.  Token delivery arrives
//! through the governed backend channel (gptbridge-core session
//! descriptor); this view renders the projection only.

use gpui::{div, prelude::*, rgb, Context, IntoElement, Render, ScrollHandle, Window};

use crate::streaming_text::StreamingBuffer;

struct Turn {
    role: &'static str,
    buffer: StreamingBuffer,
}

pub struct ModelDialogueView {
    turns: Vec<Turn>,
    scroll: ScrollHandle,
    composer: String,
}

impl ModelDialogueView {
    pub fn new(_window: &mut Window, cx: &mut Context<Self>) -> Self {
        let mut view = Self {
            turns: Vec::new(),
            scroll: ScrollHandle::new(),
            composer: String::new(),
        };
        // Seed a deterministic greeting turn so the surface is verifiably
        // alive before the first backend stream arrives.
        let mut greeting = StreamingBuffer::new();
        greeting.push_str("Model dialogue surface ready. Waiting for backend stream…");
        view.turns.push(Turn {
            role: "assistant",
            buffer: greeting,
        });
        cx.notify();
        view
    }

    /// Append stream delta to the active assistant turn (called by the
    /// stream subscription once the governed channel delivers events).
    #[allow(dead_code)]
    pub fn push_delta(&mut self, delta: &str, cx: &mut Context<Self>) {
        match self.turns.last_mut() {
            Some(turn) if turn.role == "assistant" => turn.buffer.push_str(delta),
            _ => {
                let mut buffer = StreamingBuffer::new();
                buffer.push_str(delta);
                self.turns.push(Turn {
                    role: "assistant",
                    buffer,
                });
            }
        }
        cx.notify();
    }

    fn render_turn(&self, turn: &Turn) -> impl IntoElement {
        let (bg, label) = match turn.role {
            "user" => (rgb(0x243244), "you"),
            _ => (rgb(0x1d2b21), "model"),
        };
        div()
            .flex()
            .flex_col()
            .gap_1()
            .p_3()
            .rounded_md()
            .bg(bg)
            .child(
                div()
                    .text_xs()
                    .text_color(rgb(0x8a94a6))
                    .child(label.to_string()),
            )
            .child(
                div()
                    .text_sm()
                    .text_color(rgb(0xe6e9ef))
                    .child(turn.buffer.text().to_string()),
            )
    }
}

impl Render for ModelDialogueView {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .flex()
            .flex_col()
            .size_full()
            .bg(rgb(0x14181f))
            .child(
                div()
                    .flex()
                    .items_center()
                    .px_4()
                    .py_2()
                    .border_b_1()
                    .border_color(rgb(0x2a3140))
                    .child(
                        div()
                            .text_sm()
                            .text_color(rgb(0xaeb7c6))
                            .child("GPTBridge · Model Dialogue".to_string()),
                    ),
            )
            .child(
                div()
                    .id("turns")
                    .flex_1()
                    .flex()
                    .flex_col()
                    .gap_2()
                    .p_4()
                    .overflow_y_scroll()
                    .track_scroll(&self.scroll)
                    .children(self.turns.iter().map(|t| self.render_turn(t))),
            )
            .child(
                div()
                    .flex()
                    .items_center()
                    .gap_2()
                    .px_4()
                    .py_3()
                    .border_t_1()
                    .border_color(rgb(0x2a3140))
                    .child(
                        div()
                            .flex_1()
                            .rounded_md()
                            .bg(rgb(0x1c2230))
                            .px_3()
                            .py_2()
                            .text_sm()
                            .text_color(rgb(0x8a94a6))
                            .child(if self.composer.is_empty() {
                                "Type a message — sends through the governed channel…".to_string()
                            } else {
                                self.composer.clone()
                            }),
                    ),
            )
    }
}

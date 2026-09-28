//! streaming_text.rs — Streaming Text surface + shared streaming buffer.
//!
//! ``StreamingBuffer`` is the incremental text accumulator every dialogue
//! surface reuses (model tokens arrive as deltas).  ``StreamingTextView``
//! is the standalone visual-verification surface for it.

use gpui::{div, prelude::*, rgb, Context, IntoElement, Render, ScrollHandle, Window};

/// Incremental UTF-8 text accumulator for token-at-a-time stream delivery.
/// Bounded: keeps the newest ``CAPACITY`` bytes so a runaway stream cannot
/// grow memory without bound.
pub struct StreamingBuffer {
    text: String,
    complete: bool,
}

impl StreamingBuffer {
    const CAPACITY: usize = 1 << 20;

    pub fn new() -> Self {
        Self {
            text: String::new(),
            complete: false,
        }
    }

    pub fn push_str(&mut self, delta: &str) {
        self.text.push_str(delta);
        if self.text.len() > Self::CAPACITY {
            let cut = self.text.len() - Self::CAPACITY;
            let boundary = self
                .text
                .char_indices()
                .map(|(i, _)| i)
                .find(|i| *i >= cut)
                .unwrap_or(self.text.len());
            self.text.drain(..boundary);
        }
    }

    pub fn mark_complete(&mut self) {
        self.complete = true;
    }

    pub fn text(&self) -> &str {
        &self.text
    }

    pub fn is_complete(&self) -> bool {
        self.complete
    }
}

impl Default for StreamingBuffer {
    fn default() -> Self {
        Self::new()
    }
}

pub struct StreamingTextView {
    buffer: StreamingBuffer,
    scroll: ScrollHandle,
}

impl StreamingTextView {
    pub fn new(_window: &mut Window, _cx: &mut Context<Self>) -> Self {
        let mut buffer = StreamingBuffer::new();
        buffer.push_str(
            "Streaming text surface ready.\n\nToken deltas append here at line rate; \
             the buffer is bounded (newest 1 MiB) so a runaway stream cannot grow \
             memory without bound.",
        );
        buffer.mark_complete();
        Self {
            buffer,
            scroll: ScrollHandle::new(),
        }
    }
}

impl Render for StreamingTextView {
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
                    .child("GPTBridge · Streaming Text".to_string()),
            )
            .child(
                div()
                    .id("stream")
                    .flex_1()
                    .p_4()
                    .overflow_y_scroll()
                    .track_scroll(&self.scroll)
                    .text_sm()
                    .text_color(rgb(0xe6e9ef))
                    .child(self.buffer.text().to_string()),
            )
            .child(
                div()
                    .px_4()
                    .py_2()
                    .border_t_1()
                    .border_color(rgb(0x2a3140))
                    .text_xs()
                    .text_color(rgb(0x8a94a6))
                    .child(if self.buffer.is_complete() {
                        "stream complete".to_string()
                    } else {
                        "streaming…".to_string()
                    }),
            )
    }
}

//! native_views.rs — High-performance Native Views gallery.
//!
//! A frame-rate-verification surface: many small elements, a live tick
//! counter, and governed runtime facts (system metrics via gptbridge-core)
//! to prove the native path end-to-end.

use gpui::{Context, IntoElement, Render, Window, div, prelude::*, px, rgb};

pub struct NativeViewsGallery {
    ticks: u64,
}

impl NativeViewsGallery {
    pub fn new(_window: &mut Window, cx: &mut Context<Self>) -> Self {
        // 1 Hz tick — proves the view invalidates on the native event loop.
        cx.spawn(async move |this, cx| {
            loop {
                cx.background_executor()
                    .timer(std::time::Duration::from_secs(1))
                    .await;
                let _ = this.update(&mut *cx, |v, cx| {
                    v.ticks += 1;
                    cx.notify();
                });
            }
        })
        .detach();
        Self { ticks: 0 }
    }

    fn cell(&self, label: &str, value: String) -> impl IntoElement {
        div()
            .flex()
            .flex_col()
            .gap_1()
            .rounded_md()
            .bg(rgb(0x1c2230))
            .p_3()
            .min_w(px(180.0))
            .child(
                div()
                    .text_xs()
                    .text_color(rgb(0x8a94a6))
                    .child(label.to_string()),
            )
            .child(div().text_sm().text_color(rgb(0xe6e9ef)).child(value))
    }
}

impl Render for NativeViewsGallery {
    fn render(&mut self, _window: &mut Window, _cx: &mut Context<Self>) -> impl IntoElement {
        let metrics = gptbridge_core::native::metrics::get_system_metrics();
        let gateway = gptbridge_core::ipc::is_gateway_alive();
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
                    .child("GPTBridge · Native Views".to_string()),
            )
            .child(
                div()
                    .flex()
                    .flex_wrap()
                    .gap_3()
                    .p_4()
                    .child(self.cell("event ticks", format!("{}", self.ticks)))
                    .child(self.cell(
                        "cpu %",
                        metrics["cpuUsagePercent"]
                            .as_f64()
                            .map(|v| format!("{v:.1}"))
                            .unwrap_or_else(|| "n/a".to_string()),
                    ))
                    .child(self.cell(
                        "ram %",
                        metrics["ramUsagePercent"]
                            .as_f64()
                            .map(|v| format!("{v:.1}"))
                            .unwrap_or_else(|| "n/a".to_string()),
                    ))
                    .child(self.cell(
                        "disk %",
                        metrics["diskUsagePercent"]
                            .as_f64()
                            .map(|v| format!("{v:.1}"))
                            .unwrap_or_else(|| "n/a".to_string()),
                    ))
                    .child(self.cell(
                        "backend gateway",
                        if gateway { "live" } else { "down" }.to_string(),
                    )),
            )
    }
}

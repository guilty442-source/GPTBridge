//! geometry.rs — window-content geometry cache and bounds clamping.
//!
//! The main window's logical content size is cached from Resized event
//! payloads: reading ``inner_size``/``scale_factor`` mid-handler touches
//! tao's per-window ``window_state`` lock — an AB-BA hazard against
//! in-flight WndProc dispatch — so geometry is event-fed instead.

use std::sync::Mutex;
use std::sync::OnceLock;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct BrowserBounds {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

/// Last logical content size delivered by the main window's Resized events.
fn content_size() -> &'static Mutex<Option<(f64, f64, f64)>> {
    static SIZE: OnceLock<Mutex<Option<(f64, f64, f64)>>> = OnceLock::new();
    SIZE.get_or_init(|| Mutex::new(None))
}

pub fn record_content_size(size: tauri::PhysicalSize<u32>, scale_factor: f64) {
    // A hidden/not-yet-realised window can report 0x0 — a zero size must
    // never poison the cache (every bounds clamp would fail-closed-hide).
    if size.width == 0 || size.height == 0 {
        return;
    }
    let logical = size.to_logical::<f64>(scale_factor);
    *content_size().lock().unwrap() = Some((logical.width, logical.height, scale_factor));
}

/// Cached logical content size for callers that must not touch window
/// state (resize handlers, bridge workers).
pub fn current_content_size() -> Option<(f64, f64)> {
    content_size().lock().unwrap().map(|(w, h, _)| (w, h))
}

fn current_scale_factor() -> f64 {
    content_size()
        .lock()
        .unwrap()
        .map(|(_, _, s)| s)
        .unwrap_or(1.0)
}

pub(crate) fn clamp_bounds(app: &AppHandle, bounds: BrowserBounds) -> Option<BrowserBounds> {
    if app.get_window(super::host().window_label).is_none() {
        return None;
    }
    let (content_w, content_h) = current_content_size()?;
    let x = bounds.x.round().max(0.0).min(content_w);
    let y = bounds.y.round().max(0.0).min(content_h);
    let width = bounds.width.round().max(0.0).min(content_w - x);
    let height = bounds.height.round().max(0.0).min(content_h - y);
    if width < 1.0 || height < 1.0 {
        return None;
    }
    Some(BrowserBounds {
        x,
        y,
        width,
        height,
    })
}

/// Logical → physical bounds for the worker-side ``set_position``/``set_size``
/// pair (workers run in physical pixels against the parent HWND).
pub(crate) fn physical_bounds(bounds: &BrowserBounds) -> serde_json::Value {
    let scale = current_scale_factor();
    serde_json::json!({
        "x": bounds.x * scale,
        "y": bounds.y * scale,
        "width": bounds.width * scale,
        "height": bounds.height * scale,
    })
}

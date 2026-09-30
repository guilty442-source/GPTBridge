//! main_window — native egui main dashboard (E180/C116 native-UI).
//!
//! Replaces the retired WebView2/JS main renderer, reproducing the
//! original product design in pure Rust: dark ``#060a12`` canvas,
//! brand-lockup header, hero stat cards, and the governed toolbox card
//! grid.  The shell spawns ``gptbridge-egui.exe --main-window`` under
//! the same ``GPTBRIDGE_SOURCE_UI_*`` contract as tool windows; data
//! flows over the authenticated loopback WebSocket.
//!
//! Removed JS-era surfaces: third-party panel, saga visualiser,
//! capacity/SLO drawers, sovereign dashboard, detail drawers and HMR
//! plumbing — the governed operator surface only.

use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::tool_window::{Backend, ConnState, ToolWindowConfig};

// ── Design tokens (mirror of renderer App.css :root) ──────────────
const BG: egui::Color32 = egui::Color32::from_rgb(6, 10, 18);
const CARD_TOP: egui::Color32 = egui::Color32::from_rgb(14, 21, 38);
const CARD_BOTTOM: egui::Color32 = egui::Color32::from_rgb(9, 15, 26);
const FIELD_FILL: egui::Color32 = egui::Color32::from_rgb(8, 13, 22);
const TEXT: egui::Color32 = egui::Color32::from_rgb(247, 249, 252);
const TEXT_SOFT: egui::Color32 = egui::Color32::from_rgb(238, 243, 255);
const MUTED: egui::Color32 = egui::Color32::from_rgb(148, 163, 184);
const SUBTLE: egui::Color32 = egui::Color32::from_rgb(100, 116, 139);
const DESC: egui::Color32 = egui::Color32::from_rgb(155, 169, 189);
const BRAND_STRONG: egui::Color32 = egui::Color32::from_rgb(55, 109, 244);
const BRAND_MARK_TOP: egui::Color32 = egui::Color32::from_rgb(109, 153, 255);
const BRAND_MARK_BOTTOM: egui::Color32 = egui::Color32::from_rgb(49, 94, 213);
const SUCCESS: egui::Color32 = egui::Color32::from_rgb(61, 214, 165);
const WARNING: egui::Color32 = egui::Color32::from_rgb(243, 185, 85);
const DANGER: egui::Color32 = egui::Color32::from_rgb(255, 123, 139);
const BTN_SECONDARY: egui::Color32 = egui::Color32::from_rgb(30, 41, 59);

fn line() -> egui::Color32 {
    egui::Color32::from_rgba_unmultiplied(148, 163, 184, 36)
}
fn line_strong() -> egui::Color32 {
    egui::Color32::from_rgba_unmultiplied(148, 163, 184, 66)
}
fn brand_soft() -> egui::Color32 {
    egui::Color32::from_rgba_unmultiplied(91, 140, 255, 31)
}
fn hairline() -> egui::Color32 {
    egui::Color32::from_rgba_unmultiplied(125, 163, 255, 41)
}
/// Neon accent for HUD slashes / scan slivers.
const NEON: egui::Color32 = egui::Color32::from_rgb(96, 165, 250);
fn tag_line() -> egui::Color32 {
    egui::Color32::from_rgba_unmultiplied(96, 140, 190, 92)
}
fn dot_grid() -> egui::Color32 {
    egui::Color32::from_rgba_unmultiplied(148, 180, 255, 9)
}
fn hatch_line() -> egui::Color32 {
    egui::Color32::from_rgba_unmultiplied(125, 163, 255, 12)
}

const LIST_REFRESH: Duration = Duration::from_secs(5);

/// Monogram per tool id — same map as the retired webview card.
fn tool_mark(id: &str, name: &str) -> String {
    match id {
        "ai-assistant" => "投".to_string(),
        "xingcheng" | "local-model" => "星".to_string(),
        "ai-collaboration" => "外".to_string(),
        "project-cleaner" => "救".to_string(),
        "vaultly" => "安".to_string(),
        "file-sorter" => "檔".to_string(),
        _ => name.chars().next().unwrap_or('工').to_string(),
    }
}

fn status_label(status: &str) -> &'static str {
    match status {
        "running" => "執行中",
        "starting" => "啟動中",
        "stopping" => "強制關閉中",
        "error" => "需要處理",
        _ => "已停止",
    }
}

fn status_color(status: &str) -> egui::Color32 {
    match status {
        "running" => SUCCESS,
        "starting" | "stopping" => WARNING,
        "error" => DANGER,
        _ => MUTED,
    }
}

/// zh-TW byte formatting — mirrors the retired ``formatBytes``.
fn format_bytes(bytes: u64) -> String {
    const UNITS: [&str; 5] = ["B", "KB", "MB", "GB", "TB"];
    let mut v = bytes as f64;
    let mut u = 0usize;
    while v >= 1024.0 && u < UNITS.len() - 1 {
        v /= 1024.0;
        u += 1;
    }
    if u == 0 {
        format!("{bytes} B")
    } else {
        format!("{v:.1} {}", UNITS[u])
    }
}

struct ToolCard {
    id: String,
    name: String,
    description: String,
    status: String,
    runtime_available: bool,
    launchable: bool,
    lifecycle_locked: bool,
    error: String,
    data_boundary: String,
    runtime_mode: String,
}

impl ToolCard {
    fn from_value(v: &Value) -> Option<Self> {
        let id = v["id"].as_str()?.trim().to_string();
        if id.is_empty() {
            return None;
        }
        let db = &v["data_boundary"];
        let data_boundary = match db["database_scope"].as_str() {
            Some("tool-database-only") => "工具專屬資料庫".to_string(),
            Some(scope) => scope.to_string(),
            None if db["code_scope"].as_str() == Some("tool-root-only") => {
                "工具根目錄".to_string()
            }
            None if db["standalone"].as_bool() == Some(true) => "獨立".to_string(),
            _ => "未宣告".to_string(),
        };
        let auto = v["automatic_runtime_mode"].as_str().unwrap_or("");
        let selected = match auto {
            "governed-source" => "治理來源碼",
            "executable" => "執行檔",
            _ => "",
        };
        let runtime_mode = match v["runtime_mode"].as_str() {
            Some("dual-runtime") if !selected.is_empty() => {
                format!("雙執行模式（目前：{selected}）")
            }
            Some("dual-runtime") => "雙執行模式".to_string(),
            Some("governed-source") => "治理來源碼".to_string(),
            Some("executable") => "執行檔".to_string(),
            _ => "未宣告".to_string(),
        };
        Some(Self {
            name: v["name"]
                .as_str()
                .filter(|s| !s.trim().is_empty())
                .unwrap_or(&id)
                .to_string(),
            description: v["description"].as_str().unwrap_or("").to_string(),
            status: v["status"].as_str().unwrap_or("stopped").to_string(),
            runtime_available: v["runtime_available"].as_bool().unwrap_or(false),
            launchable: v["launchable"].as_bool().unwrap_or(true),
            lifecycle_locked: v["lifecycle_locked"].as_bool().unwrap_or(false),
            error: v["error"].as_str().unwrap_or("").to_string(),
            data_boundary,
            runtime_mode,
            id,
        })
    }

    fn running(&self) -> bool {
        matches!(self.status.as_str(), "running" | "starting")
    }
    fn busy(&self) -> bool {
        matches!(self.status.as_str(), "starting" | "stopping")
    }
}

/// Cut-corner silhouette — top-left and bottom-right corners clipped,
/// the signature HUD panel shape shared with the native tool surfaces.
fn chamfer_pts(rect: egui::Rect, cut: f32) -> Vec<egui::Pos2> {
    let (l, t, r, b) =
        (rect.left(), rect.top(), rect.right(), rect.bottom());
    vec![
        egui::pos2(l + cut, t),
        egui::pos2(r, t),
        egui::pos2(r, b - cut),
        egui::pos2(r - cut, b),
        egui::pos2(l, b),
        egui::pos2(l, t + cut),
    ]
}

/// Small L-shaped corner tick (aiming-reticle detail).
fn corner_tick(
    painter: &egui::Painter,
    x: f32,
    y: f32,
    dx: f32,
    dy: f32,
    len: f32,
    color: egui::Color32,
) {
    let stroke = egui::Stroke::new(1.0, color);
    painter
        .line_segment([egui::pos2(x + dx * len, y), egui::pos2(x, y)], stroke);
    painter
        .line_segment([egui::pos2(x, y), egui::pos2(x, y + dy * len)], stroke);
}

/// Filled diamond — HUD status marker replacing the round dot.
fn diamond(
    painter: &egui::Painter,
    center: egui::Pos2,
    r: f32,
    color: egui::Color32,
) {
    painter.add(egui::Shape::convex_polygon(
        vec![
            center + egui::vec2(0.0, -r),
            center + egui::vec2(r, 0.0),
            center + egui::vec2(0.0, r),
            center + egui::vec2(-r, 0.0),
        ],
        color,
        egui::Stroke::NONE,
    ));
}

/// Staggered dot-grid backdrop — low-alpha tech texture behind cards.
fn paint_dot_grid(painter: &egui::Painter, rect: egui::Rect) {
    let clipped = painter.with_clip_rect(rect);
    let mut y = rect.top() + 8.0;
    while y < rect.bottom() {
        let mut x = rect.left() + 8.0;
        while x < rect.right() {
            clipped.circle_filled(egui::pos2(x, y), 1.0, dot_grid());
            x += 24.0;
        }
        y += 24.0;
    }
}

/// Diagonal hatch marks — header-band texture.
fn paint_diag_hatch(
    painter: &egui::Painter,
    rect: egui::Rect,
    step: f32,
    slope: f32,
    color: egui::Color32,
) {
    let stroke = egui::Stroke::new(1.0, color);
    let clipped = painter.with_clip_rect(rect);
    let mut x = rect.left() - slope;
    while x < rect.right() {
        clipped.line_segment(
            [egui::pos2(x, rect.bottom()), egui::pos2(x + slope, rect.top())],
            stroke,
        );
        x += step;
    }
}

/// Vertical-gradient HUD card — the webview cards ran
/// ``rgba(17,27,46,.7) → rgba(10,16,28,.7)`` at 160deg over ``#060a12``;
/// the silhouette is chamfered, with a neon slash on the cut corner and
/// aiming ticks on the sharp corners when ``detail`` is set.
fn paint_card(
    painter: &egui::Painter,
    rect: egui::Rect,
    top: egui::Color32,
    bottom: egui::Color32,
    cut: f32,
    stroke: egui::Color32,
    detail: bool,
) {
    use egui::epaint::{Mesh, Vertex};
    let mut mesh = Mesh::default();
    for (pos, col) in [
        (rect.left_top(), top),
        (rect.right_top(), top),
        (rect.right_bottom(), bottom),
        (rect.left_bottom(), bottom),
    ] {
        mesh.vertices.push(Vertex {
            pos,
            uv: egui::epaint::WHITE_UV,
            color: col,
        });
    }
    mesh.add_triangle(0, 1, 2);
    mesh.add_triangle(0, 2, 3);
    painter
        .with_clip_rect(rect)
        .add(egui::Shape::mesh(mesh));
    let (l, t, r, b) =
        (rect.left(), rect.top(), rect.right(), rect.bottom());
    // Carve the clipped corners back to the canvas colour.
    for tri in [
        [
            egui::pos2(l, t),
            egui::pos2(l + cut, t),
            egui::pos2(l, t + cut),
        ],
        [
            egui::pos2(r, b),
            egui::pos2(r - cut, b),
            egui::pos2(r, b - cut),
        ],
    ] {
        painter.add(egui::Shape::convex_polygon(
            tri.to_vec(),
            BG,
            egui::Stroke::NONE,
        ));
    }
    painter.add(egui::Shape::closed_line(
        chamfer_pts(rect, cut),
        egui::Stroke::new(1.0, stroke),
    ));
    if !detail {
        return;
    }
    // Neon slash inside the top-left cut edge.
    painter.line_segment(
        [
            egui::pos2(l + 2.0, t + cut + 4.0),
            egui::pos2(l + cut + 4.0, t + 2.0),
        ],
        egui::Stroke::new(2.0, NEON),
    );
    // Aiming ticks on the two sharp corners.
    corner_tick(painter, r - 10.0, t + 10.0, -1.0, 1.0, 12.0, tag_line());
    corner_tick(painter, l + 10.0, b - 10.0, 1.0, -1.0, 12.0, tag_line());
}

pub struct MainWindow {
    cfg: ToolWindowConfig,
    backend: Backend,
    last_list_poll: Option<Instant>,
    tools: Vec<ToolCard>,
    /// tool id → (bytes, file_count) from ``app:get-platform-tool-sizes``.
    sizes: HashMap<String, (u64, u64)>,
    busy: HashSet<String>,
    status: Option<Value>,
    notice: Option<String>,
    search: String,
    filter_issues: bool,
}

impl MainWindow {
    pub fn new(cfg: ToolWindowConfig) -> Self {
        Self {
            cfg,
            backend: Backend::new("main-window"),
            last_list_poll: None,
            tools: Vec::new(),
            sizes: HashMap::new(),
            busy: HashSet::new(),
            status: None,
            notice: None,
            search: String::new(),
            filter_issues: false,
        }
    }

    fn request_list(&mut self) {
        if self.backend.send("toolbox_list_tools", json!({})).is_none() {
            self.notice = Some("無法送出工具清單請求".to_string());
        }
        let _ = self
            .backend
            .send("app:get-platform-tool-sizes", json!({"forceRefresh": false}));
    }

    fn start_tool(&mut self, tool_id: &str) {
        if self.backend.connected() && self.busy.insert(tool_id.to_string()) {
            if self
                .backend
                .send("toolbox_start_tool", json!({"tool_id": tool_id}))
                .is_none()
            {
                self.busy.remove(tool_id);
            }
        }
    }

    fn stop_tool(&mut self, tool_id: &str) {
        if self.backend.connected() && self.busy.insert(tool_id.to_string()) {
            if self
                .backend
                .send("toolbox_stop_tool", json!({"tool_id": tool_id}))
                .is_none()
            {
                self.busy.remove(tool_id);
            }
        }
    }

    fn handle_event(&mut self, event: &str, payload: &Value) {
        match event {
            "runtime_status_push" => {
                self.status = Some(payload.clone());
            }
            "toolbox_list_tools_result" => {
                if payload["ok"].as_bool() != Some(false) {
                    self.tools = payload["tools"]
                        .as_array()
                        .map(|a| a.iter().filter_map(ToolCard::from_value).collect())
                        .unwrap_or_default();
                }
            }
            "app:get-platform-tool-sizes_result" => {
                if let Some(arr) = payload["tools"].as_array() {
                    self.sizes = arr
                        .iter()
                        .filter_map(|t| {
                            let id = t["id"].as_str()?.to_string();
                            let bytes = t["project_size_bytes"].as_u64()?;
                            let files = t["file_count"].as_u64().unwrap_or(0);
                            Some((id, (bytes, files)))
                        })
                        .collect();
                }
            }
            "toolbox_start_tool_result" | "toolbox_stop_tool_result"
            | "toolbox_force_close_tool_result" => {
                let tool_id = payload["tool_id"].as_str().unwrap_or("").to_string();
                self.busy.remove(&tool_id);
                if payload["ok"].as_bool() == Some(false) {
                    let code = payload["error"]
                        .as_str()
                        .or_else(|| payload["status"].as_str())
                        .unwrap_or("unknown");
                    self.notice = Some(format!("{tool_id}: {code}"));
                } else {
                    self.notice = None;
                }
                self.last_list_poll = None; // refresh roster on next tick
            }
            "error" => {
                self.notice = payload["message"]
                    .as_str()
                    .map(|s| s.to_string())
                    .or_else(|| Some("後端回報錯誤".to_string()));
            }
            _ => {}
        }
    }

    fn connected_ready(&self) -> bool {
        let status = self.status.as_ref();
        self.backend.connected()
            && status.and_then(|s| s["ok"].as_bool()).unwrap_or(false)
            && status.and_then(|s| s["runtime_state"].as_str()) == Some("ready")
    }

    /// Header connection pill — (label, detail, tone color).
    fn connection(&self) -> (&'static str, &'static str, egui::Color32) {
        match self.backend.state {
            ConnState::Connected if self.connected_ready() => {
                ("系統正常", "安全連線與啟動維護均已完成", SUCCESS)
            }
            ConnState::Connected => ("系統審查中", "即時通道連接或修復中", WARNING),
            ConnState::Connecting => ("正在連線", "正在檢查並恢復後端服務", WARNING),
            ConnState::Disconnected => {
                ("系統異常", "後端即時通道中斷，正在自動重連", DANGER)
            }
        }
    }

    fn paint_mark(painter: &egui::Painter, rect: egui::Rect, ch: &str, size: f32) {
        painter.add(egui::Shape::convex_polygon(
            chamfer_pts(rect, 9.0),
            brand_soft(),
            egui::Stroke::new(
                1.0,
                egui::Color32::from_rgba_unmultiplied(125, 163, 255, 61),
            ),
        ));
        painter.text(
            rect.center(),
            egui::Align2::CENTER_CENTER,
            ch,
            egui::FontId::proportional(size),
            egui::Color32::from_rgb(189, 208, 255),
        );
    }

    fn pill(
        ui: &mut egui::Ui,
        label: &str,
        detail: &str,
        color: egui::Color32,
    ) {
        egui::Frame::new()
            .corner_radius(egui::CornerRadius::same(3))
            .inner_margin(egui::Margin::symmetric(10, 5))
            .fill(egui::Color32::from_rgba_unmultiplied(12, 19, 34, 204))
            .stroke(egui::Stroke::new(1.0, line()))
            .show(ui, |ui| {
                ui.horizontal(|ui| {
                    let (dot, _) = ui.allocate_exact_size(
                        egui::vec2(8.0, 8.0),
                        egui::Sense::hover(),
                    );
                    diamond(ui.painter(), dot.center(), 4.0, color);
                    ui.vertical(|ui| {
                        ui.label(
                            egui::RichText::new(label)
                                .size(10.0)
                                .strong()
                                .color(egui::Color32::from_rgb(231, 237, 248)),
                        );
                        if !detail.is_empty() {
                            ui.label(
                                egui::RichText::new(detail).size(9.0).color(MUTED),
                            );
                        }
                    });
                });
            });
    }

    fn hero_card(
        ui: &mut egui::Ui,
        width: f32,
        label: &str,
        value: &str,
        value_color: egui::Color32,
        hint: &str,
        primary: bool,
        warning: bool,
    ) {
        let (rect, _) = ui.allocate_exact_size(
            egui::vec2(width, 96.0),
            egui::Sense::hover(),
        );
        let stroke = if warning {
            egui::Color32::from_rgba_unmultiplied(243, 185, 85, 51)
        } else if primary {
            egui::Color32::from_rgba_unmultiplied(91, 140, 255, 51)
        } else {
            line()
        };
        let (top, bottom) = if primary {
            (
                egui::Color32::from_rgba_unmultiplied(30, 46, 82, 190),
                CARD_BOTTOM,
            )
        } else {
            (CARD_TOP, CARD_BOTTOM)
        };
        paint_card(ui.painter(), rect, top, bottom, 12.0, stroke, true);
        let mut inner = ui.new_child(
            egui::UiBuilder::new()
                .max_rect(rect.shrink2(egui::vec2(18.0, 14.0)))
                .layout(egui::Layout::top_down(egui::Align::Min)),
        );
        inner.label(
            egui::RichText::new(label.to_uppercase())
                .size(10.0)
                .strong()
                .color(MUTED),
        );
        inner.add_space(10.0);
        inner.label(
            egui::RichText::new(value).size(30.0).strong().color(value_color),
        );
        inner.add_space(4.0);
        inner.label(egui::RichText::new(hint).size(10.0).color(SUBTLE));
    }

    fn tool_card(
        &mut self,
        ui: &mut egui::Ui,
        index: usize,
        width: f32,
        ready: bool,
        start_requests: &mut Vec<String>,
        stop_requests: &mut Vec<String>,
    ) {
        let tool = &self.tools[index];
        let busy = tool.busy() || self.busy.contains(&tool.id);
        let (rect, _) = ui.allocate_exact_size(
            egui::vec2(width, 176.0),
            egui::Sense::hover(),
        );
        let stroke = match tool.status.as_str() {
            "running" => egui::Color32::from_rgba_unmultiplied(61, 214, 165, 41),
            "error" => egui::Color32::from_rgba_unmultiplied(255, 123, 139, 51),
            _ => line(),
        };
        paint_card(ui.painter(), rect, CARD_TOP, CARD_BOTTOM, 14.0, stroke, true);

        let mut inner = ui.new_child(
            egui::UiBuilder::new()
                .max_rect(rect.shrink2(egui::vec2(16.0, 14.0)))
                .layout(egui::Layout::top_down(egui::Align::Min)),
        );
        let ui = &mut inner;

        // Topline: monogram + status pill.
        ui.horizontal(|ui| {
            let (mark_rect, _) = ui.allocate_exact_size(
                egui::vec2(34.0, 34.0),
                egui::Sense::hover(),
            );
            Self::paint_mark(
                ui.painter(),
                mark_rect,
                &tool_mark(&tool.id, &tool.name),
                14.0,
            );
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                egui::Frame::new()
                    .corner_radius(egui::CornerRadius::same(3))
                    .inner_margin(egui::Margin::symmetric(9, 4))
                    .fill(egui::Color32::from_rgba_unmultiplied(6, 10, 18, 160))
                    .stroke(egui::Stroke::new(1.0, line()))
                    .show(ui, |ui| {
                        ui.horizontal(|ui| {
                            let (dot, _) = ui.allocate_exact_size(
                                egui::vec2(6.0, 6.0),
                                egui::Sense::hover(),
                            );
                            diamond(
                                ui.painter(),
                                dot.center(),
                                3.5,
                                status_color(&tool.status),
                            );
                            ui.label(
                                egui::RichText::new(status_label(&tool.status))
                                    .size(10.0)
                                    .strong()
                                    .color(status_color(&tool.status)),
                            );
                        });
                    });
            });
        });
        ui.add_space(8.0);

        ui.label(egui::RichText::new(&tool.name).size(15.0).strong().color(TEXT));
        if !tool.description.is_empty() {
            ui.label(egui::RichText::new(&tool.description).size(11.0).color(DESC));
        }
        ui.label(egui::RichText::new(&tool.runtime_mode).size(9.0).color(SUBTLE));
        ui.add_space(6.0);

        // Quick meta: 資料夾總大小 / 資料邊界 (same cells as the webview card).
        let size_text = self
            .sizes
            .get(&tool.id)
            .map(|(b, _)| format_bytes(*b))
            .unwrap_or_else(|| "計算中".to_string());
        ui.horizontal(|ui| {
            let cell_w = (ui.available_width() - 8.0) / 2.0;
            for (label, value) in [
                ("資料夾總大小", size_text.as_str()),
                ("資料邊界", tool.data_boundary.as_str()),
            ] {
                egui::Frame::new()
                    .corner_radius(egui::CornerRadius::same(4))
                    .inner_margin(egui::Margin::symmetric(10, 8))
                    .fill(FIELD_FILL)
                    .stroke(egui::Stroke::new(
                        1.0,
                        egui::Color32::from_rgba_unmultiplied(148, 163, 184, 20),
                    ))
                    .show(ui, |ui| {
                        ui.set_min_width(cell_w - 20.0);
                        ui.label(
                            egui::RichText::new(label).size(9.0).strong().color(SUBTLE),
                        );
                        ui.label(
                            egui::RichText::new(value)
                                .size(12.0)
                                .strong()
                                .color(egui::Color32::from_rgb(219, 228, 242)),
                        );
                    });
            }
        });

        if !tool.error.is_empty() {
            ui.add_space(6.0);
            ui.label(egui::RichText::new(&tool.error).size(10.0).color(DANGER));
        }

        // Footer actions pinned to card bottom.
        let actions_h = 34.0;
        let footer_top = rect.bottom() - 14.0 - actions_h;
        let footer_rect = egui::Rect::from_min_size(
            egui::pos2(rect.left() + 16.0, footer_top),
            egui::vec2(rect.width() - 32.0, actions_h),
        );
        let mut fui = ui.new_child(
            egui::UiBuilder::new()
                .max_rect(footer_rect)
                .layout(egui::Layout::right_to_left(egui::Align::Center)),
        );
        if !tool.lifecycle_locked {
            let id = tool.id.clone();
            let can_start =
                ready && tool.runtime_available && tool.launchable && !tool.running() && !busy;
            let can_stop = ready && tool.running() && !busy;
            if fui
                .add_enabled(
                    can_stop,
                    egui::Button::new(
                        egui::RichText::new(if tool.status == "stopping" {
                            "強制關閉中…"
                        } else {
                            "強制關閉"
                        })
                        .size(11.0)
                        .strong()
                        .color(egui::Color32::from_rgb(216, 225, 239)),
                    )
                    .fill(BTN_SECONDARY)
                    .stroke(egui::Stroke::new(1.0, line_strong()))
                    .corner_radius(egui::CornerRadius::same(3)),
                )
                .clicked()
            {
                stop_requests.push(id.clone());
            }
            fui.add_space(8.0);
            if fui
                .add_enabled(
                    can_start,
                    egui::Button::new(
                        egui::RichText::new(if tool.status == "starting" {
                            "啟動中…"
                        } else {
                            "啟動"
                        })
                        .size(11.0)
                        .strong()
                        .color(egui::Color32::WHITE),
                    )
                    .fill(BRAND_STRONG)
                    .stroke(egui::Stroke::new(
                        1.0,
                        egui::Color32::from_rgba_unmultiplied(140, 180, 255, 140),
                    ))
                    .corner_radius(egui::CornerRadius::same(3)),
                )
                .clicked()
            {
                start_requests.push(id);
            }
            if busy {
                fui.spinner();
            }
        }
    }
}

impl eframe::App for MainWindow {
    fn logic(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let was_disconnected = !self.backend.connected();
        let (batch, _closed) = self.backend.tick(&self.cfg.ws_url);
        if was_disconnected && self.backend.connected() {
            self.request_list();
            self.last_list_poll = Some(Instant::now());
        }
        if self.backend.connected() {
            let due = self
                .last_list_poll
                .map(|t| t.elapsed() >= LIST_REFRESH)
                .unwrap_or(true);
            if due {
                self.request_list();
                self.last_list_poll = Some(Instant::now());
            }
        }
        for (event, payload) in batch {
            self.handle_event(&event, &payload);
        }
        ctx.request_repaint_after(Duration::from_millis(400));
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ready = self.connected_ready();
        let maintenance = self
            .status
            .as_ref()
            .and_then(|s| s["maintenance_ready"].as_bool())
            .unwrap_or(false);
        let running = self.tools.iter().filter(|t| t.running()).count();
        let issues = self
            .tools
            .iter()
            .filter(|t| t.status == "error" || !t.runtime_available)
            .count();
        let total = self.tools.len();
        let version = self
            .status
            .as_ref()
            .and_then(|s| s["version"].as_str())
            .unwrap_or("1.0")
            .to_string();
        let (conn_label, conn_detail, conn_color) = self.connection();
        let (review_state, review_hint, review_warn) = if issues > 0 {
            (
                format!("{issues} 處異常"),
                "偵測到異常的工具".to_string(),
                true,
            )
        } else if ready {
            ("正常".to_string(), "已依據法典完成全域唯讀審查".to_string(), false)
        } else if self.backend.connected() {
            ("審查中".to_string(), "即時通道連接或修復中".to_string(), false)
        } else {
            ("系統異常".to_string(), "後端即時通道中斷".to_string(), true)
        };

        // ── Header ────────────────────────────────────────────────
        egui::Panel::top("header")
            .exact_size(40.0)
            .frame(
                egui::Frame::new()
                    .fill(egui::Color32::from_rgba_unmultiplied(6, 10, 18, 235))
                    .inner_margin(egui::Margin::symmetric(16, 4)),
            )
            .show(ui, |ui| {
                // Tech texture + baseline + sweeping scan sliver.
                let r = ui.max_rect();
                paint_diag_hatch(ui.painter(), r, 14.0, 9.0, hatch_line());
                ui.painter().hline(
                    r.x_range(),
                    r.bottom() - 0.5,
                    egui::Stroke::new(1.0, line()),
                );
                let t = ui.ctx().input(|i| i.time) as f32;
                let phase = (t * 0.10).fract();
                let x0 = r.left() + phase * r.width();
                let seg = 110.0f32.min(r.width() * 0.2);
                ui.painter().line_segment(
                    [
                        egui::pos2(x0, r.bottom() - 0.5),
                        egui::pos2(x0 + seg, r.bottom() - 0.5),
                    ],
                    egui::Stroke::new(
                        2.0,
                        egui::Color32::from_rgba_unmultiplied(96, 165, 250, 150),
                    ),
                );
                ui.horizontal(|ui| {
                    // Brand lockup — chamfered mark.
                    let (mark_rect, _) = ui.allocate_exact_size(
                        egui::vec2(26.0, 26.0),
                        egui::Sense::hover(),
                    );
                    paint_card(
                        ui.painter(),
                        mark_rect,
                        BRAND_MARK_TOP,
                        BRAND_MARK_BOTTOM,
                        7.0,
                        egui::Color32::from_rgba_unmultiplied(125, 163, 255, 92),
                        false,
                    );
                    ui.painter().text(
                        mark_rect.center(),
                        egui::Align2::CENTER_CENTER,
                        "G",
                        egui::FontId::proportional(13.0),
                        egui::Color32::WHITE,
                    );
                    ui.add_space(6.0);
                    ui.vertical(|ui| {
                        ui.label(egui::RichText::new("GPTBridge").size(12.0).strong().color(TEXT));
                        ui.label(
                            egui::RichText::new("應用程式控制中心").size(10.0).color(MUTED),
                        );
                    });

                    ui.with_layout(
                        egui::Layout::right_to_left(egui::Align::Center),
                        |ui| {
                            // Version badge.
                            egui::Frame::new()
                                .corner_radius(egui::CornerRadius::same(3))
                                .inner_margin(egui::Margin::symmetric(9, 5))
                                .fill(egui::Color32::from_rgba_unmultiplied(17, 27, 46, 217))
                                .stroke(egui::Stroke::new(1.0, line_strong()))
                                .show(ui, |ui| {
                                    ui.label(
                                        egui::RichText::new(format!("v{version}"))
                                            .size(10.0)
                                            .strong()
                                            .color(egui::Color32::from_rgb(219, 231, 255)),
                                    );
                                });
                            ui.add_space(8.0);
                            Self::pill(ui, conn_label, conn_detail, conn_color);
                            ui.add_space(10.0);
                            ui.label(
                                egui::RichText::new("// SYS-CONSOLE")
                                    .size(9.0)
                                    .color(SUBTLE)
                                    .family(egui::FontFamily::Monospace),
                            );
                        },
                    );
                });
            });

        // ── Footer ────────────────────────────────────────────────
        egui::Panel::bottom("footer")
            .exact_size(34.0)
            .frame(
                egui::Frame::new()
                    .fill(BG)
                    .inner_margin(egui::Margin::symmetric(24, 4)),
            )
            .show(ui, |ui| {
                let r = ui.max_rect();
                ui.painter().hline(
                    r.x_range(),
                    r.top() + 0.5,
                    egui::Stroke::new(1.0, line()),
                );
                ui.horizontal(|ui| {
                    ui.label(
                        egui::RichText::new(format!("GPTBridge v{version}"))
                            .size(10.0)
                            .color(SUBTLE),
                    );
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        ui.label(
                            egui::RichText::new("Windows 11 · 本機優先")
                                .size(10.0)
                                .color(SUBTLE),
                        );
                    });
                });
            });

        // ── Main ──────────────────────────────────────────────────
        egui::CentralPanel::default()
            .frame(egui::Frame::new().fill(BG).inner_margin(egui::Margin::symmetric(24, 18)))
            .show(ui, |ui| {
                // Dot-grid tech texture behind everything.
                paint_dot_grid(ui.painter(), ui.max_rect());
                egui::ScrollArea::vertical()
                    .auto_shrink([false; 2])
                    .show(ui, |ui| {
                // Notice — offline safe mode / maintenance gate.
                if !ready || !maintenance {
                    let (text, detail) = if !self.backend.connected() {
                        ("離線安全模式", "後端連線中斷時，狀態變更指令不會送出或排隊；系統會自動修正並重新連線。")
                    } else {
                        ("啟動維護尚未完成", "正在檢查版本相容性並執行主系統穩定性修正，完成前不開放狀態變更。")
                    };
                    egui::Frame::new()
                        .corner_radius(egui::CornerRadius::same(4))
                        .inner_margin(egui::Margin::symmetric(16, 10))
                        .fill(egui::Color32::from_rgba_unmultiplied(110, 76, 21, 36))
                        .stroke(egui::Stroke::new(
                            1.0,
                            egui::Color32::from_rgba_unmultiplied(243, 185, 85, 66),
                        ))
                        .show(ui, |ui| {
                            ui.horizontal(|ui| {
                                ui.label(
                                    egui::RichText::new(text).size(12.0).strong().color(
                                        egui::Color32::from_rgb(244, 213, 152),
                                    ),
                                );
                                ui.label(
                                    egui::RichText::new(detail)
                                        .size(11.0)
                                        .color(egui::Color32::from_rgb(203, 189, 159)),
                                );
                            });
                        });
                    ui.add_space(12.0);
                }
                if let Some(notice) = self.notice.clone() {
                    egui::Frame::new()
                        .corner_radius(egui::CornerRadius::same(4))
                        .inner_margin(egui::Margin::symmetric(16, 10))
                        .fill(egui::Color32::from_rgba_unmultiplied(125, 35, 51, 38))
                        .stroke(egui::Stroke::new(
                            1.0,
                            egui::Color32::from_rgba_unmultiplied(255, 123, 139, 71),
                        ))
                        .show(ui, |ui| {
                            ui.label(
                                egui::RichText::new(&notice)
                                    .size(11.0)
                                    .color(egui::Color32::from_rgb(255, 173, 184)),
                            );
                        });
                    ui.add_space(12.0);
                }

                // Hero grid — five stat cards.
                let gap = 12.0;
                let avail = ui.available_width();
                let hero_w = ((avail - gap * 4.0) / 5.0).max(140.0);
                ui.horizontal(|ui| {
                    Self::hero_card(
                        ui, hero_w, "可用工具", &total.to_string(), TEXT_SOFT,
                        "已註冊的獨立工具", true, false,
                    );
                    ui.add_space(gap);
                    Self::hero_card(
                        ui, hero_w, "執行中", &running.to_string(), TEXT_SOFT,
                        "目前由主系統管理", false, false,
                    );
                    ui.add_space(gap);
                    Self::hero_card(
                        ui, hero_w, "需要處理", &issues.to_string(), TEXT_SOFT,
                        "偵測到異常的工具", false, issues > 0,
                    );
                    ui.add_space(gap);
                    Self::hero_card(
                        ui, hero_w, "指令策略", "請求工具執行", TEXT_SOFT,
                        "治理驗證後交由權責工具執行", false, false,
                    );
                    ui.add_space(gap);
                    Self::hero_card(
                        ui, hero_w, "星澄助理", &review_state,
                        if review_warn { WARNING } else { SUCCESS },
                        &review_hint, false, review_warn,
                    );
                });
                ui.add_space(18.0);

                // ── Toolbox panel ─────────────────────────────────
                let needle = self.search.trim().to_lowercase();
                let filtered: Vec<usize> = self
                    .tools
                    .iter()
                    .enumerate()
                    .filter(|(_, t)| {
                        (!self.filter_issues
                            || t.status == "error"
                            || !t.runtime_available)
                            && (needle.is_empty()
                                || t.name.to_lowercase().contains(&needle)
                                || t.description.to_lowercase().contains(&needle)
                                || t.id.to_lowercase().contains(&needle))
                    })
                    .map(|(i, _)| i)
                    .collect();
                let shown = filtered.len();
                egui::Frame::new()
                    .corner_radius(egui::CornerRadius::same(8))
                    .inner_margin(egui::Margin::same(24))
                    .fill(egui::Color32::from_rgba_unmultiplied(10, 16, 28, 153))
                    .stroke(egui::Stroke::new(1.0, line()))
                    .show(ui, |ui| {
                        // Header row.
                        ui.horizontal(|ui| {
                            ui.vertical(|ui| {
                                ui.label(
                                    egui::RichText::new("// 獨立工具")
                                        .size(10.0)
                                        .strong()
                                        .color(egui::Color32::from_rgb(145, 175, 255))
                                        .family(egui::FontFamily::Monospace),
                                );
                                ui.label(
                                    egui::RichText::new("獨立工具").size(20.0).strong().color(TEXT),
                                );
                                ui.label(
                                    egui::RichText::new(
                                        "由主系統統一請求啟動、停止與更新；各工具的功能與資料維持清楚邊界。",
                                    )
                                    .size(11.0)
                                    .color(MUTED),
                                );
                            });
                            ui.with_layout(
                                egui::Layout::right_to_left(egui::Align::Center),
                                |ui| {
                                    if ui
                                        .add(
                                            egui::Button::new(
                                                egui::RichText::new("重新整理")
                                                    .size(11.0)
                                                    .strong()
                                                    .color(egui::Color32::from_rgb(
                                                        216, 225, 239,
                                                    )),
                                                )
                                                .fill(egui::Color32::from_rgba_unmultiplied(
                                                    30, 41, 59, 128,
                                                ))
                                                .stroke(egui::Stroke::new(1.0, line_strong()))
                                                .corner_radius(egui::CornerRadius::same(3)),
                                        )
                                        .clicked()
                                        && self.backend.connected()
                                    {
                                        self.request_list();
                                    }
                                    ui.add_space(10.0);
                                    ui.label(
                                        egui::RichText::new(format!("顯示 {shown} / {total}"))
                                            .size(10.0)
                                            .color(SUBTLE),
                                    );
                                },
                            );
                        });
                        ui.add_space(14.0);
                        ui.painter().hline(
                            ui.available_rect_before_wrap().x_range(),
                            ui.cursor().top(),
                            egui::Stroke::new(1.0, line()),
                        );
                        ui.add_space(14.0);

                        // Search + filter row (原 webview toolbar).
                        ui.horizontal(|ui| {
                            egui::Frame::new()
                                .corner_radius(egui::CornerRadius::same(3))
                                .inner_margin(egui::Margin::symmetric(10, 6))
                                .fill(FIELD_FILL)
                                .stroke(egui::Stroke::new(1.0, line()))
                                .show(ui, |ui| {
                                    ui.add_sized(
                                        [208.0, 18.0],
                                        egui::TextEdit::singleline(&mut self.search)
                                            .hint_text(
                                                egui::RichText::new("搜尋工具或功能")
                                                    .color(SUBTLE),
                                            )
                                            .text_color(TEXT)
                                            .frame(egui::Frame::NONE),
                                    );
                                });
                            ui.add_space(10.0);
                            for (label, active) in
                                [("全部", !self.filter_issues), ("需處理", self.filter_issues)]
                            {
                                let (fill, text_col) = if active {
                                    (
                                        egui::Color32::from_rgba_unmultiplied(91, 140, 255, 36),
                                        egui::Color32::from_rgb(189, 208, 255),
                                    )
                                } else {
                                    (
                                        egui::Color32::from_rgba_unmultiplied(12, 19, 34, 153),
                                        MUTED,
                                    )
                                };
                                if ui
                                    .add(
                                        egui::Button::new(
                                            egui::RichText::new(label).size(11.0).color(text_col),
                                        )
                                        .fill(fill)
                                        .stroke(egui::Stroke::new(1.0, line()))
                                        .corner_radius(egui::CornerRadius::same(3)),
                                    )
                                    .clicked()
                                {
                                    self.filter_issues = label == "需處理";
                                }
                            }
                        });
                        ui.add_space(14.0);

                        if self.tools.is_empty() {
                            egui::Frame::new()
                                .corner_radius(egui::CornerRadius::same(4))
                                .inner_margin(egui::Margin::symmetric(24, 32))
                                .stroke(egui::Stroke::new(
                                    1.0,
                                    line_strong(),
                                ))
                                .show(ui, |ui| {
                                    ui.centered_and_justified(|ui| {
                                        ui.label(
                                            egui::RichText::new("尚未發現可用的獨立工具。")
                                                .size(12.0)
                                                .color(MUTED),
                                        );
                                    });
                                });
                            return;
                        }
                        if filtered.is_empty() {
                            egui::Frame::new()
                                .corner_radius(egui::CornerRadius::same(14))
                                .inner_margin(egui::Margin::symmetric(24, 32))
                                .stroke(egui::Stroke::new(1.0, line_strong()))
                                .show(ui, |ui| {
                                    ui.vertical_centered(|ui| {
                                        ui.label(
                                            egui::RichText::new("找不到符合條件的工具")
                                                .size(12.0)
                                                .color(MUTED),
                                        );
                                        ui.label(
                                            egui::RichText::new("請調整搜尋文字或篩選條件。")
                                                .size(10.0)
                                                .color(SUBTLE),
                                        );
                                    });
                                });
                            return;
                        }

                        let cols = 3usize;
                        let card_gap = 12.0;
                        let card_w =
                            ((ui.available_width() - card_gap * (cols - 1) as f32) / cols as f32)
                                .max(200.0);
                        let mut start_requests: Vec<String> = Vec::new();
                        let mut stop_requests: Vec<String> = Vec::new();
                        for row in 0..filtered.len().div_ceil(cols) {
                            ui.horizontal(|ui| {
                                for col in 0..cols {
                                    let Some(&i) = filtered.get(row * cols + col) else {
                                        ui.allocate_space(egui::vec2(card_w, 1.0));
                                        continue;
                                    };
                                    self.tool_card(
                                        ui, i, card_w, ready,
                                        &mut start_requests,
                                        &mut stop_requests,
                                    );
                                    ui.add_space(card_gap);
                                }
                            });
                            ui.add_space(card_gap);
                        }
                        for id in start_requests {
                            self.start_tool(&id);
                        }
                        for id in stop_requests {
                            self.stop_tool(&id);
                        }
                    });
                    });
            });
    }
}

/// Run the native main dashboard (``--main-window`` mode).  Reuses the
/// governed ``GPTBRIDGE_SOURCE_UI_*`` contract; ``tool_id`` is cosmetic
/// for the main surface (``main-system``).
pub fn run_main_window(cfg: ToolWindowConfig) -> eframe::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([cfg.width, cfg.height])
            .with_min_inner_size([960.0, 640.0])
            .with_title("GPTBridge"),
        ..Default::default()
    };
    eframe::run_native(
        "GPTBridge",
        options,
        Box::new(|cc| {
            crate::fonts::install_ui_fonts(&cc.egui_ctx);
            Ok(Box::new(MainWindow::new(cfg)))
        }),
    )
}

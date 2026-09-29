use std::time::{Duration, Instant};

use serde_json::{json, Value};

use super::{Backend, ConnState, ToolWindowConfig};

const STATUS_REFRESH: Duration = Duration::from_secs(15);

struct Turn {
    role: &'static str,
    content: String,
    model: String,
    failed: bool,
}

struct Pending {
    request_id: String,
    command: String,
    streamed: bool,
    assistant_index: usize,
}

pub struct StarChatWindow {
    cfg: ToolWindowConfig,
    backend: Backend,
    last_status_poll: Option<Instant>,
    turns: Vec<Turn>,
    draft: String,
    generating: bool,
    stage: String,
    thinking_since: Option<Instant>,
    pending: Option<Pending>,
    models: Vec<(String, String)>,
    selected_model: String,
    scroll_to_end: bool,
}

impl StarChatWindow {
    pub fn new(cfg: ToolWindowConfig) -> Self {
        Self {
            cfg,
            backend: Backend::new("star-chat"),
            last_status_poll: None,
            turns: Vec::new(),
            draft: String::new(),
            generating: false,
            stage: "準備處理".to_string(),
            thinking_since: None,
            pending: None,
            models: Vec::new(),
            selected_model: String::new(),
            scroll_to_end: false,
        }
    }

    fn send_request(&mut self, command: &str, payload: Value) -> Option<String> {
        self.backend.send(command, payload)
    }

    fn send_message(&mut self) {
        let message = self.draft.trim().to_string();
        if message.is_empty() || self.generating || !self.backend.connected() {
            return;
        }
        self.turns.push(Turn {
            role: "user",
            content: message.clone(),
            model: String::new(),
            failed: false,
        });
        self.turns.push(Turn {
            role: "assistant",
            content: "正在準備回覆…".to_string(),
            model: String::new(),
            failed: false,
        });
        let assistant_index = self.turns.len() - 1;
        let history: Vec<Value> = self
            .turns
            .iter()
            .rev()
            .skip(1)
            .take(8)
            .filter(|t| !t.content.is_empty() && !t.failed)
            .map(|t| json!({"role": t.role, "content": t.content}))
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        let request_id = self
            .send_request(
                "star_chat_send_message",
                json!({
                    "message": message,
                    "history": history,
                    "max_output_tokens": 512,
                    "runtime_model": self.selected_model,
                    "context_budget_characters": 10000,
                    "autonomous_agent": true,
                    "programming_folder": "",
                }),
            )
            .unwrap_or_default();
        if request_id.is_empty() {
            self.turns[assistant_index].content = "後端連線中斷，請重新送出訊息。".to_string();
            self.turns[assistant_index].failed = true;
            return;
        }
        self.pending = Some(Pending {
            request_id,
            command: "star_chat_send_message".to_string(),
            streamed: false,
            assistant_index,
        });
        self.draft.clear();
        self.generating = true;
        self.stage = "準備處理".to_string();
        self.thinking_since = Some(Instant::now());
        self.scroll_to_end = true;
    }

    fn run_diagnostic(&mut self, command: &str, label: &str) {
        if self.generating || !self.backend.connected() {
            return;
        }
        self.turns.push(Turn {
            role: "user",
            content: label.to_string(),
            model: String::new(),
            failed: false,
        });
        self.turns.push(Turn {
            role: "assistant",
            content: "檢查中…".to_string(),
            model: String::new(),
            failed: false,
        });
        let assistant_index = self.turns.len() - 1;
        let request_id = self.send_request(command, json!({})).unwrap_or_default();
        if request_id.is_empty() {
            self.turns[assistant_index].content = "後端連線中斷。".to_string();
            self.turns[assistant_index].failed = true;
            return;
        }
        self.pending = Some(Pending {
            request_id,
            command: command.to_string(),
            streamed: false,
            assistant_index,
        });
        self.generating = true;
        self.stage = "法典檢查".to_string();
        self.thinking_since = Some(Instant::now());
        self.scroll_to_end = true;
    }

    fn stop_generating(&mut self) {
        if let Some(pending) = self.pending.take() {
            if pending.command == "star_chat_send_message" {
                self.send_request(
                    "toolbox_cancel_tool_run",
                    json!({"request_id": pending.request_id}),
                );
            }
            if let Some(turn) = self.turns.get_mut(pending.assistant_index) {
                turn.content = "已停止產生回答。".to_string();
            }
        }
        self.generating = false;
    }

    fn finish_pending(&mut self, payload: &Value) {
        let Some(pending) = self.pending.take() else { return };
        let ok = payload["ok"].as_bool().unwrap_or(true);
        let text = payload["response"]
            .as_str()
            .or_else(|| payload["message"].as_str())
            .map(str::trim)
            .filter(|t| !t.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| {
                if ok {
                    "模型已完成處理。".to_string()
                } else {
                    "所選模型目前無法完成這項要求。".to_string()
                }
            });
        let model = payload["generation"]["model"]
            .as_str()
            .or_else(|| payload["model"].as_str())
            .unwrap_or("")
            .to_string();
        if let Some(turn) = self.turns.get_mut(pending.assistant_index) {
            turn.content = text;
            turn.failed = !ok;
            turn.model = model;
        }
        self.generating = false;
        self.scroll_to_end = true;
    }

    fn handle_progress(&mut self, payload: &Value) {
        let Some(pending) = self.pending.as_mut() else { return };
        let phase = payload["phase"].as_str().unwrap_or("");
        self.stage = match phase {
            "command-understanding" => "繁中命令理解".to_string(),
            "workflow-frontend" => "流程規劃".to_string(),
            "command-planned" => "專家模型處理".to_string(),
            "generating" => "生成回答".to_string(),
            _ => payload["message"]
                .as_str()
                .unwrap_or("處理中")
                .to_string(),
        };
        let text = payload["text"].as_str().unwrap_or("");
        if !text.is_empty() {
            pending.streamed = true;
            if let Some(turn) = self.turns.get_mut(pending.assistant_index) {
                turn.content = text.to_string();
                turn.model = payload["model"].as_str().unwrap_or("").to_string();
            }
            self.scroll_to_end = true;
        }
    }

    fn handle_event(&mut self, event: &str, payload: &Value) {
        let request_id = payload["request_id"].as_str().unwrap_or("");
        let matches_pending = self
            .pending
            .as_ref()
            .map(|p| p.request_id == request_id)
            .unwrap_or(false);
        match event {
            "error" if matches_pending || self.pending.is_some() => {
                let message = payload["message"]
                    .as_str()
                    .unwrap_or("後端處理失敗。")
                    .to_string();
                if let Some(pending) = self.pending.take() {
                    if let Some(turn) = self.turns.get_mut(pending.assistant_index) {
                        turn.content = message;
                        turn.failed = true;
                    }
                }
                self.generating = false;
            }
            "star_chat_status_result" => self.apply_status(payload),
            e if e.ends_with("_progress") && matches_pending => {
                self.handle_progress(payload);
            }
            e if e.ends_with("_result") && matches_pending => {
                self.finish_pending(payload);
            }
            _ => {}
        }
    }

    fn apply_status(&mut self, payload: &Value) {
        let catalog = payload["native_runtime"]["selectable_models"].as_array();
        if let Some(items) = catalog {
            let mut options: Vec<(String, String)> = vec![(
                String::new(),
                "自動模型路由（速度、推理與能力強度）".to_string(),
            )];
            for item in items {
                if item["installed"].as_bool() != Some(true) {
                    continue;
                }
                let name = item["name"].as_str().unwrap_or("").to_string();
                if name.is_empty() {
                    continue;
                }
                let label = item["label"].as_str().unwrap_or(&name).to_string();
                options.push((name, label));
            }
            if options.len() > 1 {
                self.models = options;
            }
        }
    }
}

impl eframe::App for StarChatWindow {
    fn logic(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        // Reconnect cadence — mirrors the web surface's 500 ms retry.
        let was_disconnected = !self.backend.connected();
        let (batch, closed) = self.backend.tick(&self.cfg.ws_url);
        if was_disconnected && self.backend.connected() {
            self.send_request("star_chat_status", json!({}));
        }
        if self.backend.connected() {
            let poll_due = self
                .last_status_poll
                .map(|t| t.elapsed() >= STATUS_REFRESH)
                .unwrap_or(true);
            if poll_due {
                self.send_request("star_chat_status", json!({}));
                self.last_status_poll = Some(Instant::now());
            }
        }
        for (event, payload) in batch {
            self.handle_event(&event, &payload);
        }
        if closed {
            if let Some(pending) = self.pending.take() {
                if let Some(turn) = self.turns.get_mut(pending.assistant_index) {
                    turn.content = "後端連線中斷，請重新送出訊息。".to_string();
                    turn.failed = true;
                }
                self.generating = false;
            }
        }
        // Live surfaces (spinner, event feed, reconnect) need a repaint
        // cadence; egui only repaints on input by default.
        ctx.request_repaint_after(Duration::from_millis(200));
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let status_label = match self.backend.state {
            ConnState::Connected => "模型服務已連線",
            ConnState::Connecting => "等待連線",
            ConnState::Disconnected => "連線中斷・自動重試",
        };

        egui::Panel::left("sidebar").resizable(false).show(ui, |ui| {
            ui.heading("模型對話");
            ui.label(egui::RichText::new("本機模型對話入口").weak());
            ui.separator();
            let (text, color) = match self.backend.state {
                ConnState::Connected => ("● ", egui::Color32::from_rgb(96, 200, 128)),
                ConnState::Connecting => ("● ", egui::Color32::from_rgb(220, 180, 80)),
                ConnState::Disconnected => ("● ", egui::Color32::from_rgb(220, 96, 96)),
            };
            ui.horizontal(|ui| {
                ui.label(egui::RichText::new(text).color(color));
                ui.label(status_label);
            });
            ui.separator();
            ui.label(egui::RichText::new("外部協作已停用").weak().small());
            ui.label(
                egui::RichText::new("訓練與能力編成由星澄原生模型內部自行處理").weak().small(),
            );
        });

        egui::Panel::top("header").show(ui, |ui| {
            ui.horizontal(|ui| {
                ui.label(egui::RichText::new("GOVERNED LOCAL INTELLIGENCE").weak().small());
                ui.separator();
                if !self.models.is_empty() {
                    egui::ComboBox::from_id_salt("model-picker")
                        .selected_text(
                            self.models
                                .iter()
                                .find(|(name, _)| *name == self.selected_model)
                                .map(|(_, label)| label.clone())
                                .unwrap_or_else(|| "自動模型路由".to_string()),
                        )
                        .show_ui(ui, |ui| {
                            for (name, label) in self.models.clone() {
                                ui.selectable_value(&mut self.selected_model, name, label);
                            }
                        });
                }
                if ui
                    .button("清除本次對話")
                    .on_hover_text("清空目前對話紀錄")
                    .clicked()
                    && !self.generating
                {
                    self.turns.clear();
                }
            });
        });

        egui::Panel::bottom("composer").show(ui, |ui| {
            ui.horizontal(|ui| {
                if ui
                    .button("法典 × 實作對齊")
                    .clicked()
                {
                    self.run_diagnostic("star_chat_codex_alignment", "法典 × 實作對齊檢查");
                }
                if ui
                    .button("法典 × 架構圖同步")
                    .clicked()
                {
                    self.run_diagnostic(
                        "star_chat_architecture_sync",
                        "中文法典 × 架構圖同步檢查",
                    );
                }
            });
            ui.horizontal(|ui| {
                let editor = egui::TextEdit::singleline(&mut self.draft)
                    .hint_text("輸入想聊的內容、程式需求或開發指令…")
                    .desired_width(f32::INFINITY);
                let response = ui.add_enabled(!self.generating, editor);
                let enter_send = response.lost_focus()
                    && ui.input(|i| i.key_pressed(egui::Key::Enter));
                if self.generating {
                    if ui.button("■ 停止").clicked() {
                        self.stop_generating();
                    }
                } else if ui.button("↑ 送出").clicked() || enter_send {
                    self.send_message();
                }
            });
            ui.label(
                egui::RichText::new("Enter 送出 · 上下文依本機負載自動調整").weak().small(),
            );
        });

        egui::CentralPanel::default().show(ui, |ui| {
            egui::ScrollArea::vertical()
                .auto_shrink([false; 2])
                .stick_to_bottom(true)
                .show(ui, |ui| {
                    if self.turns.is_empty() {
                        ui.add_space(24.0);
                        ui.vertical_centered(|ui| {
                            ui.heading("現在可以開始聊聊。");
                            ui.label(
                                "問答、討論、整理想法、撰寫內容與程式任務都在對話內完成。",
                            );
                            for suggestion in [
                                "幫我整理今天的想法",
                                "用簡單方式解釋一個概念",
                                "幫我潤飾這段文字",
                                "設計一個 Python API 並附測試",
                            ] {
                                if ui.button(suggestion).clicked() {
                                    self.draft = suggestion.to_string();
                                }
                            }
                        });
                    }
                    for turn in &self.turns {
                        let (tag, tint) = match turn.role {
                            "user" => ("你", egui::Color32::from_rgb(70, 110, 170)),
                            _ => ("模", egui::Color32::from_rgb(52, 96, 68)),
                        };
                        egui::Frame::new()
                            .fill(tint.gamma_multiply(0.35))
                            .corner_radius(6.0)
                            .inner_margin(egui::Margin::same(8))
                            .show(ui, |ui| {
                                ui.set_max_width(ui.available_width());
                                ui.label(
                                    egui::RichText::new(format!(
                                        "{tag} {}",
                                        if turn.model.is_empty() {
                                            String::new()
                                        } else {
                                            format!("· {}", turn.model)
                                        }
                                    ))
                                    .weak()
                                    .small(),
                                );
                                ui.label(egui::RichText::new(&turn.content));
                                if turn.failed {
                                    ui.colored_label(
                                        egui::Color32::from_rgb(220, 96, 96),
                                        "回覆失敗",
                                    );
                                }
                            });
                        ui.add_space(6.0);
                    }
                    if self.generating {
                        let elapsed = self
                            .thinking_since
                            .map(|t| t.elapsed().as_secs())
                            .unwrap_or(0);
                        ui.label(
                            egui::RichText::new(format!(
                                "{} · 已處理 {} 秒",
                                self.stage, elapsed
                            ))
                            .weak(),
                        );
                    }
                    if self.scroll_to_end {
                        ui.scroll_to_cursor(Some(egui::Align::BOTTOM));
                        self.scroll_to_end = false;
                    }
                });
        });
    }
}

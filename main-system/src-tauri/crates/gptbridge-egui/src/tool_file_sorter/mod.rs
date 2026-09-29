//! tool_file_sorter — native egui surface for ``file-sorter``. — native egui surface for ``file-sorter``.
//!
//! E180/C116 native-UI migration, increment 2.  Replaces the retired
//! React ``FileSorterWindowApp`` renderer; speaks the same governed
//! ``toolbox_run_tool`` / ``toolbox_run_tool_result`` /
//! ``toolbox_run_tool_progress`` / ``toolbox_cancel_tool_run`` protocol
//! over the loopback WebSocket.  Runs stay strictly serial — the web
//! surface queued every request behind ``queueRef``; the same ordering
//! is kept here so the backend never sees concurrent tool runs.
//!
//! Differences vs the retired web surface (no governed equivalent):
//! the target folder is typed/validated in-window (no native folder
//! picker yet) and the last-target-dir localStorage pref is not
//! persisted.

mod cleanup_ui;
mod keyword_ui;
mod parse;
mod profiles;
mod queue;
mod runs;
mod sort_ui;
mod ui;

use std::collections::VecDeque;

use serde_json::Value;

use crate::tool_window::{Backend, ToolWindowConfig};

pub(crate) const TOOL_ID: &str = "file-sorter";
pub(crate) const RUN_TIMEOUT_S: u64 = 30 * 60;
pub(crate) const SHORT_TIMEOUT_S: u64 = 20;
pub(crate) const FOLDER_SCAN_TIMEOUT_S: u64 = 120;
pub(crate) const FOLDERS_JSON_PREFIX: &str = "FILE_SORTER_FOLDERS_JSON=";
pub(crate) const PLAN_JSON_PREFIXES: [&str; 2] =
    ["FILE_SORTER_PREVIEW_JSON=", "FILE_SORTER_PLAN_JSON="];
pub(crate) const PROFILES_JSON_PREFIXES: [&str; 1] = ["FILE_SORTER_PROFILES_JSON="];

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum RunKind {
    ListFolders,
    Profiles,
    SelectScanTarget,
    SetProfileEnabled,
    SetDuplicateTrash,
    Preview,
    ApplyPlan,
    Undo,
    History,
    ListKeywords,
    MutateKeywords,
    Cleanup,
}

pub(crate) struct QueuedRun {
    pub(crate) kind: RunKind,
    pub(crate) args: Vec<String>,
    pub(crate) timeout_s: u64,
    pub(crate) running_label: String,
    pub(crate) success_label: String,
}

pub(crate) struct ActiveRun {
    pub(crate) request_id: String,
    pub(crate) kind: RunKind,
    /// The toggle value the run is expected to land (profile/dup-trash).
    pub(crate) desired: Option<bool>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum RunState {
    Idle,
    Running,
    Success,
    Error,
}

#[derive(Clone, Copy)]
pub(crate) enum Confirm {
    Undo,
    EnableDupTrash,
    EnableAutoOrganize,
}

pub(crate) struct HistoryEntry {
    pub(crate) ok: bool,
    pub(crate) action: String,
    pub(crate) detail: String,
}

pub struct FileSorterWindow {
    pub(crate) cfg: ToolWindowConfig,
    pub(crate) backend: Backend,
    pub(crate) queue: VecDeque<QueuedRun>,
    pub(crate) active: Option<ActiveRun>,
    // Workspace / target
    pub(crate) target_dir: String,
    pub(crate) target_validated: bool,
    // Auto-profile state
    pub(crate) auto_organize: bool,
    pub(crate) auto_organize_status: String,
    pub(crate) dup_trash: bool,
    pub(crate) dup_trash_status: String,
    pub(crate) migration_review: bool,
    // Destination folders
    pub(crate) destination_folders: Vec<String>,
    pub(crate) auto_scan_folders: bool,
    pub(crate) folder_scan_status: String,
    pub(crate) folders_loaded: bool,
    pub(crate) profile_loaded: bool,
    // Keyword rules
    pub(crate) keyword_input: String,
    pub(crate) keyword_folder: String,
    pub(crate) current_keyword: String,
    pub(crate) updated_keyword: String,
    // Manual sort
    pub(crate) run_state: RunState,
    pub(crate) message: String,
    pub(crate) output: String,
    pub(crate) sort_plan: Option<Value>,
    pub(crate) plan_confirmed: bool,
    pub(crate) history_open: bool,
    pub(crate) history_entries: Vec<HistoryEntry>,
    pub(crate) history_output: String,
    // Cleanup scan
    pub(crate) cleanup_state: RunState,
    pub(crate) cleanup_message: String,
    pub(crate) cleanup_output: String,
    pub(crate) cleanup_progress: Option<Value>,
    pub(crate) cleanup_files: Vec<Value>,
    pub(crate) cleanup_stop_requested: bool,
    pub(crate) cleanup_image_issues: bool,
    pub(crate) cleanup_similar_images: bool,
    pub(crate) cleanup_video_issues: bool,
    pub(crate) cleanup_similar_videos: bool,
    pub(crate) cleanup_parallel: bool,
    pub(crate) cleanup_threshold: f64,
    pub(crate) cleanup_speed: f64,
    pub(crate) model_temperature: f64,
    pub(crate) model_top_p: f64,
    pub(crate) model_context_window: u32,
    pub(crate) model_max_tokens: u32,
    // Modal confirm (replaces window.confirm)
    pub(crate) pending_confirm: Option<(Confirm, String)>,
    // Labels of the run currently being sent so the result handler can
    // reuse them; separate from ``active`` because ``pump_queue`` moves
    // the ``QueuedRun`` before the send completes.
    pub(crate) pending_labels: (String, String),
}

impl FileSorterWindow {
    pub fn new(cfg: ToolWindowConfig) -> Self {
        Self {
            cfg,
            backend: Backend::new("file-sorter"),
            queue: VecDeque::new(),
            active: None,
            target_dir: String::new(),
            target_validated: false,
            auto_organize: false,
            auto_organize_status: String::new(),
            dup_trash: false,
            dup_trash_status: String::new(),
            migration_review: false,
            destination_folders: Vec::new(),
            auto_scan_folders: true,
            folder_scan_status: String::new(),
            folders_loaded: false,
            profile_loaded: false,
            keyword_input: String::new(),
            keyword_folder: String::new(),
            current_keyword: String::new(),
            updated_keyword: String::new(),
            run_state: RunState::Idle,
            message: "自動化檔案管理已就緒".to_string(),
            output: String::new(),
            sort_plan: None,
            plan_confirmed: false,
            history_open: false,
            history_entries: Vec::new(),
            history_output: String::new(),
            cleanup_state: RunState::Idle,
            cleanup_message: "清理掃描已併入此工具".to_string(),
            cleanup_output: String::new(),
            cleanup_progress: None,
            cleanup_files: Vec::new(),
            cleanup_stop_requested: false,
            cleanup_image_issues: false,
            cleanup_similar_images: false,
            cleanup_video_issues: true,
            cleanup_similar_videos: false,
            cleanup_parallel: true,
            cleanup_threshold: 96.0,
            cleanup_speed: 50.0,
            model_temperature: 0.0,
            model_top_p: 0.9,
            model_context_window: 8192,
            model_max_tokens: 512,
            pending_confirm: None,
            pending_labels: (String::new(), String::new()),
        }
    }

    pub(crate) fn busy(&self) -> bool {
        self.run_state == RunState::Running || self.cleanup_state == RunState::Running
    }

    pub(crate) fn can_run(&self) -> bool {
        !self.busy()
            && self.target_validated
            && !self.target_dir.trim().is_empty()
            && self.backend.connected()
    }
}

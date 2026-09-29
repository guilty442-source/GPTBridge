//! tool_vaultly — native egui surface for ``vaultly``. — native egui surface for ``vaultly``.
//!
//! E180/C116 native-UI migration, increment 3.  Replaces the retired
//! React ``VaultlyDownloadCenter`` renderer; speaks the same governed
//! ``vaultly_*`` command protocol over the loopback WebSocket with the
//! 4 s state-poll cadence the web surface used.
//!
//! Intentional deltas vs the web surface (no governed equivalent):
//! avatar/thumbnail images render as text badges (egui has no governed
//! image fetch path), the destination folder is typed/validated via the
//! backend ``vaultly_check_destination`` command rather than a native
//! picker, and clipboard-paste is replaced by a plain multiline edit.

mod accounts_ui;
mod helpers;
mod logic;
mod posts_ui;
mod queues_ui;
mod ui;

use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::tool_window::{Backend, ToolWindowConfig};

use self::helpers::split_keywords;

pub(crate) const STATE_POLL: Duration = Duration::from_secs(4);
pub(crate) const POST_PAGE_SIZE: i64 = 40;

pub(crate) const DEFAULT_PLATFORMS: &[(&str, &str)] =
    &[("instagram", "Instagram"), ("x", "X")];

#[derive(Clone, Copy)]
pub(crate) enum VaultAction {
    State,
    CheckDest,
    ExportReport,
    CreateJob,
    CreateLinkJob,
    ScanPosts,
    CancelPostScan,
    RetryJob,
    SaveSelection,
    AddFilter,
    RemoveFilter,
    RemoveAccounts,
    RestoreAccounts,
    CancelJob,
    OpenPlatform,
    ScanFollowing,
}

pub(crate) struct Pending {
    pub(crate) action: VaultAction,
    pub(crate) context: String,
}

#[derive(Default)]
pub(crate) struct Conditions {
    pub(crate) photos: bool,
    pub(crate) videos: bool,
    pub(crate) date_since: String,
    pub(crate) date_until: String,
    pub(crate) include_keywords: String,
    pub(crate) exclude_keywords: String,
    pub(crate) min_likes: u32,
    pub(crate) min_views: u32,
    pub(crate) max_items_per_account: u32,
    pub(crate) skip_downloaded: bool,
}

impl Conditions {
    pub(crate) fn initial() -> Self {
        Self {
            photos: true,
            videos: true,
            max_items_per_account: 20,
            skip_downloaded: true,
            ..Default::default()
        }
    }

    pub(crate) fn media_types(&self) -> Vec<&'static str> {
        let mut out = Vec::new();
        if self.photos {
            out.push("photo");
        }
        if self.videos {
            out.push("video");
        }
        out
    }

    pub(crate) fn to_json(&self, full: bool) -> Value {
        let mut map = json!({
            "media_types": self.media_types(),
            "include_keywords": split_keywords(&self.include_keywords),
            "exclude_keywords": split_keywords(&self.exclude_keywords),
            "skip_downloaded": self.skip_downloaded,
        });
        if full {
            map["date_since"] = json!(self.date_since);
            map["date_until"] = json!(self.date_until);
            map["min_likes"] = json!(self.min_likes);
            map["min_views"] = json!(self.min_views);
            map["max_items_per_account"] =
                json!(self.max_items_per_account);
        }
        map
    }
}

pub struct VaultlyWindow {
    pub(crate) cfg: ToolWindowConfig,
    pub(crate) backend: Backend,
    pub(crate) pending: HashMap<String, Pending>,
    pub(crate) last_state_poll: Option<Instant>,
    pub(crate) selection_loaded: bool,
    pub(crate) busy_action: String,
    // Server state (vaultly_get_state)
    pub(crate) platforms: Vec<Value>,
    pub(crate) accounts: Vec<Value>,
    pub(crate) filter_terms: Vec<String>,
    pub(crate) removed_accounts: Vec<Value>,
    pub(crate) auto_scan: Value,
    pub(crate) jobs: Vec<Value>,
    pub(crate) post_scan_jobs: Vec<Value>,
    pub(crate) download_automation: Option<Value>,
    pub(crate) posts: Vec<Value>,
    pub(crate) posts_total: i64,
    pub(crate) diagnostics: Option<Value>,
    pub(crate) destination_health: Option<Value>,
    pub(crate) version: String,
    pub(crate) paths: (String, String, String),
    pub(crate) safety_notice: String,
    // Local UI state
    pub(crate) account_search: String,
    pub(crate) filter_input: String,
    pub(crate) post_page: i64,
    pub(crate) post_search: String,
    pub(crate) post_platform_filter: String,
    pub(crate) post_status_filter: String,
    pub(crate) selected_post_id: String,
    pub(crate) selected_ids: HashSet<String>,
    pub(crate) multi_select_mode: bool,
    pub(crate) destination: String,
    pub(crate) conditions: Conditions,
    pub(crate) quick_links: String,
    pub(crate) message: String,
}

impl VaultlyWindow {
    pub fn new(cfg: ToolWindowConfig) -> Self {
        Self {
            cfg,
            backend: Backend::new("vaultly"),
            pending: HashMap::new(),
            last_state_poll: None,
            selection_loaded: false,
            busy_action: String::new(),
            platforms: DEFAULT_PLATFORMS
                .iter()
                .map(|(id, name)| {
                    json!({"id": id, "name": name})
                })
                .collect(),
            accounts: Vec::new(),
            filter_terms: Vec::new(),
            removed_accounts: Vec::new(),
            auto_scan: Value::Null,
            jobs: Vec::new(),
            post_scan_jobs: Vec::new(),
            download_automation: None,
            posts: Vec::new(),
            posts_total: 0,
            diagnostics: None,
            destination_health: None,
            version: String::new(),
            paths: (String::new(), String::new(), String::new()),
            safety_notice: String::new(),
            account_search: String::new(),
            filter_input: String::new(),
            post_page: 0,
            post_search: String::new(),
            post_platform_filter: "all".to_string(),
            post_status_filter: "all".to_string(),
            selected_post_id: String::new(),
            selected_ids: HashSet::new(),
            multi_select_mode: false,
            destination: String::new(),
            conditions: Conditions::initial(),
            quick_links: String::new(),
            message: "載入下載中心中…".to_string(),
        }
    }
}

//! resource_governor_host.rs — resource-governor 監督接入。
//!
//! 主系統唯一啟動入口是桌面 `專案程式庫.exe`
//! （專案程式庫 → GPTBridge.Bootstrap → gptbridge-shell → backend）；
//! 因此資源管制器不得經 `--install-task`/`--install-logon` 自建第二
//! 入口——由 backend 監督引擎以 `--watch` 拉起成受管行程，與
//! automation-host/channel-host 同一監督模式（A170）。
//!
//! 重啟契約沿用既訂值：`auto_restart`、五次嘗試、一秒退避、外部
//! force-close 照樣重啟。單一實例仲裁採雙層：governor 自身對
//! `resource-governor.lock` 持有獨佔 handle（share=0），監督層先以
//! 唯寫 probe 判定外部持有（defer 一個 tick，不耗重啟額度）；檔案
//! 不存在或可開啟（stale 鎖）才放行 spawn——**probe 不得
//! `create(true)`**：建立出的無 pid 空檔會被 governor
//! `owner_alive` 的 `age < 10s` 後援誤判為活鎖。stale 鎖由
//! governor 自身的 acquire 路徑清理。
//!
//! Fail-closed：找不到 `native/resource_governor/bin/resource-governor.exe`
//! 時回報 audited terminal 狀態、不 spawn。
//! `GPTBRIDGE_GOVERNOR_DISABLE` kill switch 經 env_allow 透傳。

use std::time::Duration;

use crate::resident::{self, ServiceSpec};
use crate::tools::workspace_root;

// 服務名取 `resource-governor-host` 而非 `resource-governor`——
// 監督層狀態檔是 `runtime/state/<name>.json`，同名會與
// governor 自身快照檔 `resource-governor.json` 互相覆寫。
const SERVICE_NAME: &str = "resource-governor-host";
const SERVICE_LABEL: &str = "resource-governor";
const ENTRY_RELATIVE: &str =
    "native/resource_governor/bin/resource-governor.exe";

/// 允許透傳的環境鍵：governor kill switch（GPTBRIDGE_GOVERNOR_DISABLE）
/// 必須能從主系統環境壓制整個管制器。
const ENV_ALLOW: &[&str] = &["GPTBRIDGE_GOVERNOR_DISABLE"];

const MAX_RESTART_ATTEMPTS: u32 = 5;
const RESTART_BACKOFF: Duration = Duration::from_secs(1);

/// 外部持有仲裁：governor 對 lock 檔持有 share=0 獨佔 handle，活體
/// 實例讓唯寫 open 失敗（ERROR_SHARING_VIOLATION 32/33）→ defer。
/// 檔案不存在（尚未跑過）或可開啟（stale 鎖，governor 自清）→
/// None 放行。注意絕對不可 create(true)——新造的空鎖會被
/// owner_alive 的 mtime 後援誤判成活體，反而堵死自己的 spawn。
fn external_watch_holder() -> Option<String> {
    let lock = workspace_root()
        .join("main-system")
        .join("runtime")
        .join("state")
        .join("resource-governor.lock");
    match std::fs::OpenOptions::new().write(true).open(&lock) {
        Ok(_) => None,
        Err(e) if matches!(e.raw_os_error(), Some(32) | Some(33)) => {
            Some("external-watch-holder".to_string())
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(e) => Some(format!("lock-probe-failed:{e}")),
    }
}

/// Resolve the governed host spec.  Fail-closed: ``Err(reason)`` when
/// the published executable is unavailable.
fn load_contract() -> Result<ServiceSpec, String> {
    let root = workspace_root();
    let entry = root.join(ENTRY_RELATIVE);
    if !entry.is_file() {
        return Err(format!("GOVERNOR_ENTRY_UNAVAILABLE:{ENTRY_RELATIVE}"));
    }
    Ok(ServiceSpec {
        name: SERVICE_NAME,
        entry,
        args: vec![
            "--watch".to_string(),
            "--root".to_string(),
            root.to_string_lossy().into_owned(),
        ],
        working_dir: root.clone(),
        env: vec![
            (
                "GPTBRIDGE_ROOT".to_string(),
                root.to_string_lossy().into_owned(),
            ),
            (
                "GPTBRIDGE_PROJECT_ROOT".to_string(),
                root.to_string_lossy().into_owned(),
            ),
        ],
        env_allow: ENV_ALLOW.iter().map(|s| s.to_string()).collect(),
        label: SERVICE_LABEL.to_string(),
        auto_restart: true,
        max_restart_attempts: MAX_RESTART_ATTEMPTS,
        restart_backoff: RESTART_BACKOFF,
        force_close_suppresses_restart: false,
        preflight: Some(external_watch_holder),
    })
}

/// Start resource-governor supervision.  Idempotent; every
/// precondition failure is fail-closed (audited terminal state, no
/// spawn).
pub fn start() {
    match load_contract() {
        Ok(spec) => resident::start(spec),
        Err(code) => {
            resident::unavailable(SERVICE_NAME, SERVICE_LABEL, &code);
        }
    }
}

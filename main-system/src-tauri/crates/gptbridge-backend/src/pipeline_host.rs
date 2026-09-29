//! pipeline_host.rs — codex/SQL pipeline automation host supervision.
//!
//! Resolves the ``GPTBridge.CodexPipeline.exe --watch`` resident host —
//! the C# successor to the retired Python codex sync drivers
//! (B167/B38) — into a ``resident::ServiceSpec`` and hands it to the
//! shared supervision engine.  The host owns the governed flows from
//! ``main-system/config/automation-flows.json``:
//!
//! - ``codex-amendment-intake``   (pausable=false, governance-critical)
//! - ``codex-pin-sync``           (release-contract pin convergence)
//! - ``codex-maintenance``        (artifact parity + projection health)
//!
//! Restart contract mirrors the channel-host background_service budget:
//! ``auto_restart``, five attempts, one-second backoff, external
//! force-close still restarts (governance-critical plane).  The
//! per-flow ``enabled`` kill switches live inside the watch loop — the
//! supervisor owns process lifetime, the host owns flow cadence.
//!
//! Fail-closed: missing executable or absent ``GPTBRIDGE_POSTGRES_DSN``
//! ends supervision with an audited terminal state — the pipeline host
//! throws ``GPTBRIDGE_POSTGRES_DSN_REQUIRED`` without it, so spawning
//! would just burn the restart budget.  State publishes to
//! ``main-system/runtime/state/codex-pipeline.json``; lifecycle events
//! append to the ``codex-pipeline`` audit ledger.

use std::time::Duration;

use crate::resident::{self, ServiceSpec};
use crate::tools::workspace_root;

const SERVICE_NAME: &str = "codex-pipeline";
const SERVICE_LABEL: &str = "codex-automation";
const ENTRY_RELATIVE: &str =
    "shared-layer/csharp/GPTBridge.CodexPipeline/publish/GPTBridge.CodexPipeline.exe";

/// Environment keys the pipeline host is permitted to inherit — the
/// PostgreSQL DSNs are its only credential boundary (PgDsn.cs).
const ENV_ALLOW: &[&str] = &[
    "GPTBRIDGE_POSTGRES_DSN",
    "GPTBRIDGE_POSTGRES_ADMIN_DSN",
    "GPTBRIDGE_POSTGRES_READER_ROLE",
];

/// Restart budget mirroring the shared-layer background_service
/// contract (auto_restart, five attempts, one-second backoff).
const MAX_RESTART_ATTEMPTS: u32 = 5;
const RESTART_BACKOFF: Duration = Duration::from_secs(1);

/// Single-instance arbitration: the watch host enforces one instance
/// via ``codex-automation.lock`` (exclusive ``FileShare.None``).  An
/// already-running external holder means automation is live — defer
/// this cycle instead of spawning a doomed competitor that would burn
/// the restart budget.  When the holder exits the next cycle adopts
/// ownership and spawns our supervised child.
fn external_watch_holder() -> Option<String> {
    let lock = workspace_root()
        .join("main-system")
        .join("runtime")
        .join("state")
        .join("codex-automation.lock");
    match std::fs::OpenOptions::new().write(true).create(true).open(&lock)
    {
        Ok(_) => None,
        Err(e) if matches!(e.raw_os_error(), Some(32) | Some(33)) => {
            Some("external-watch-holder".to_string())
        }
        Err(e) => Some(format!("lock-probe-failed:{e}")),
    }
}

/// Resolve the governed host spec.  Fail-closed: ``Err(reason)`` when
/// the published executable or the runtime DSN is unavailable.
fn load_contract() -> Result<ServiceSpec, String> {
    let root = workspace_root();
    let entry = root.join(ENTRY_RELATIVE);
    if !entry.is_file() {
        return Err(format!("PIPELINE_ENTRY_UNAVAILABLE:{ENTRY_RELATIVE}"));
    }
    if std::env::var("GPTBRIDGE_POSTGRES_DSN")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .is_none()
    {
        return Err("GPTBRIDGE_POSTGRES_DSN_REQUIRED".to_string());
    }
    Ok(ServiceSpec {
        name: SERVICE_NAME,
        entry,
        args: vec!["--watch".to_string()],
        working_dir: root.clone(),
        env: vec![(
            "GPTBRIDGE_ROOT".to_string(),
            root.to_string_lossy().into_owned(),
        )],
        env_allow: ENV_ALLOW.iter().map(|s| s.to_string()).collect(),
        label: SERVICE_LABEL.to_string(),
        auto_restart: true,
        max_restart_attempts: MAX_RESTART_ATTEMPTS,
        restart_backoff: RESTART_BACKOFF,
        force_close_suppresses_restart: false,
        preflight: Some(external_watch_holder),
    })
}

/// Start pipeline-host supervision.  Idempotent; every precondition
/// failure is fail-closed (audited terminal state, no spawn).
pub fn start() {
    match load_contract() {
        Ok(spec) => resident::start(spec),
        Err(code) => {
            resident::unavailable(SERVICE_NAME, SERVICE_LABEL, &code);
        }
    }
}

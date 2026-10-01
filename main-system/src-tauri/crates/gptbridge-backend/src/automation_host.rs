//! automation_host.rs — unified automation host supervision.
//!
//! Resolves the ``GPTBridge.Automation.exe --watch`` resident host —
//! the single deadline-driven ``periodic_scheduler`` named by
//! ``main-system/config/resident-core.json`` (R3 consolidation) — into
//! a ``resident::ServiceSpec`` and hands it to the shared supervision
//! engine.  One supervised process replaces the retired per-plane
//! hosts (``codex-pipeline`` + ``permission-host``) and hosts every
//! governed periodic flow from ``automation-flows.json`` in-process:
//!
//! - ``git-automation``             sweep+sync plane
//! - ``codex-amendment-intake``     (pausable=false, governance-critical)
//! - ``codex-pin-sync``             release-contract pin convergence
//! - ``codex-maintenance``          artifact parity + projection health
//! - ``permission-automation-*``    six permission duty flows
//!
//! Restart contract mirrors the retired hosts: ``auto_restart``, five
//! attempts, one-second backoff, external force-close still restarts.
//! Inside the host each plane is an isolated failure domain — a plane
//! fault retries under a bounded per-plane budget and parks instead of
//! disturbing sibling planes; per-flow ``enabled`` kill switches live
//! inside the watch loops.  The supervisor owns process lifetime, the
//! host owns flow cadence.
//!
//! Single-instance arbitration is layered: the host holds
//! ``automation-host.lock`` for its lifetime (preflight-probed here so
//! an externally-launched unified host defers supervision instead of
//! burning the restart budget), and each plane keeps its own lock
//! (``codex-automation.lock`` / ``permission-automation.lock`` /
//! ``git-automation.lock``) so a standalone watcher still owns just
//! that plane until it exits.
//!
//! Fail-closed: a missing published executable ends supervision with
//! an audited terminal state.  A missing ``GPTBRIDGE_POSTGRES_DSN`` no
//! longer blocks the whole host — the codex plane degrades per flow
//! while git/permission planes stay live; the codex flow errors are
//! still recorded in ``codex-automation.json``.  State publishes to
//! ``main-system/runtime/state/automation-host.json``; lifecycle
//! events append to the ``automation-host`` audit ledger, and
//! per-plane transitions append to
//! ``automation-host-planes.jsonl``.

use std::time::Duration;

use crate::resident::{self, ServiceSpec};
use crate::tools::workspace_root;

const SERVICE_NAME: &str = "automation-host";
const SERVICE_LABEL: &str = "automation-host";
const ENTRY_RELATIVE: &str =
    "shared-layer/csharp/GPTBridge.Automation/publish/GPTBridge.Automation.exe";

/// Environment keys the unified host is permitted to inherit — the
/// union of the retired per-plane allowlists; the PostgreSQL DSNs are
/// the codex plane's credential boundary (PgDsn.cs) and feed the
/// permission self-healing connectivity probe.
const ENV_ALLOW: &[&str] = &[
    "GPTBRIDGE_POSTGRES_DSN",
    "GPTBRIDGE_POSTGRES_ADMIN_DSN",
    "GPTBRIDGE_POSTGRES_READER_ROLE",
];

/// Restart budget mirroring the retired host contracts (auto_restart,
/// five attempts, one-second backoff).
const MAX_RESTART_ATTEMPTS: u32 = 5;
const RESTART_BACKOFF: Duration = Duration::from_secs(1);

/// Single-instance arbitration: the unified host enforces one
/// instance via ``automation-host.lock`` (exclusive
/// ``FileShare.None``).  An already-running external holder means
/// automation is live — defer this cycle instead of spawning a doomed
/// competitor that would burn the restart budget.  When the holder
/// exits the next cycle adopts ownership and spawns our supervised
/// child.
fn external_watch_holder() -> Option<String> {
    let lock = workspace_root()
        .join("main-system")
        .join("runtime")
        .join("state")
        .join("automation-host.lock");
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
/// the published executable is unavailable.
fn load_contract() -> Result<ServiceSpec, String> {
    let root = workspace_root();
    let entry = root.join(ENTRY_RELATIVE);
    if !entry.is_file() {
        return Err(format!("AUTOMATION_ENTRY_UNAVAILABLE:{ENTRY_RELATIVE}"));
    }
    Ok(ServiceSpec {
        name: SERVICE_NAME,
        entry,
        args: vec!["--watch".to_string()],
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

/// Start unified automation-host supervision.  Idempotent; every
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

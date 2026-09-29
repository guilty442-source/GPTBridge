//! permission_host.rs — permission-plane automation host supervision.
//!
//! Resolves the ``GPTBridge.Permission.exe --watch`` resident host —
//! the C# successor to the retired Python permission duty drivers
//! (B167/B38) — into a ``resident::ServiceSpec`` and hands it to the
//! shared supervision engine.  The host owns the six governed flows
//! from ``main-system/config/automation-flows.json``:
//!
//! - ``permission-automation-lifecycle``  (grant expiry/revoke sweeps)
//! - ``permission-automation-directory``  (registry sync-hash drift)
//! - ``permission-automation-compliance`` (violation + A436 drift)
//! - ``permission-automation-healing``    (health checks, A297 repairs)
//! - ``permission-automation-audit``      (bounded audit-engine runs)
//! - ``permission-automation-identity``   (sealed-group reconcile)
//!
//! Restart contract mirrors the pipeline host: ``auto_restart``, five
//! attempts, one-second backoff, external force-close still restarts.
//! The per-flow ``enabled`` kill switches live inside the watch loop —
//! the supervisor owns process lifetime, the host owns flow cadence.
//!
//! Fail-closed: a missing published executable ends supervision with an
//! audited terminal state — the flows stay ``disabled`` in the registry
//! until the host binary exists.  State publishes to
//! ``main-system/runtime/state/permission-host.json``; lifecycle events
//! append to the ``permission-host`` audit ledger.

use std::time::Duration;

use crate::resident::{self, ServiceSpec};
use crate::tools::workspace_root;

const SERVICE_NAME: &str = "permission-host";
const SERVICE_LABEL: &str = "permission-automation";
const ENTRY_RELATIVE: &str =
    "shared-layer/csharp/GPTBridge.Permission/publish/GPTBridge.Permission.exe";

/// Environment keys the permission host is permitted to inherit — the
/// PostgreSQL DSN feeds the self-healing governance-connectivity probe.
const ENV_ALLOW: &[&str] = &["GPTBRIDGE_POSTGRES_DSN"];

/// Restart budget mirroring the pipeline-host contract (auto_restart,
/// five attempts, one-second backoff).
const MAX_RESTART_ATTEMPTS: u32 = 5;
const RESTART_BACKOFF: Duration = Duration::from_secs(1);

/// Single-instance arbitration: the watch host enforces one instance
/// via ``permission-automation.lock`` (exclusive ``FileShare.None``).
/// An already-running external holder means automation is live — defer
/// this cycle instead of spawning a doomed competitor that would burn
/// the restart budget.  When the holder exits the next cycle adopts
/// ownership and spawns our supervised child.
fn external_watch_holder() -> Option<String> {
    let lock = workspace_root()
        .join("main-system")
        .join("runtime")
        .join("state")
        .join("permission-automation.lock");
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
        return Err(format!("PERMISSION_ENTRY_UNAVAILABLE:{ENTRY_RELATIVE}"));
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

/// Start permission-host supervision.  Idempotent; every precondition
/// failure is fail-closed (audited terminal state, no spawn).
pub fn start() {
    match load_contract() {
        Ok(spec) => resident::start(spec),
        Err(code) => {
            resident::unavailable(SERVICE_NAME, SERVICE_LABEL, &code);
        }
    }
}

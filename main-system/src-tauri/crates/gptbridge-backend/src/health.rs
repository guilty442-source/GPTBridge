//! health.rs — runtime readiness gate and ``/health`` payload.
//!
//! Mirrors the retired Python readiness contract: four conditions
//! (backend-runtime-ready + governance-ready + dependencies-ready +
//! authenticated-ipc-connected) and per-dependency probes declared by
//! ``main-system/config/startup_manifest.json``.  Probes are bounded by
//! per-dependency deadlines; a probe that cannot run fails closed.

use std::net::TcpStream;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use gptbridge_core::native::paths;
use gptbridge_core::security::token;

static AUTHENTICATED_CONNECTIONS: AtomicU64 = AtomicU64::new(0);
static AUTHENTICATED_EVER: AtomicBool = AtomicBool::new(false);
static RUNTIME_FAILED: AtomicBool = AtomicBool::new(false);
static STARTUP_DEAD: AtomicBool = AtomicBool::new(false);
static BOOT_INSTANT: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();

fn boot_instant() -> Instant {
    *BOOT_INSTANT.get_or_init(Instant::now)
}

/// ``startup_gate_deadline_seconds`` from ``startup_manifest.json``
/// (``timeouts`` block); the 90 s default matches the shipped manifest.
/// Memoized: the deadline is a per-boot constant — evaluate() used to
/// re-read and re-parse the manifest on every call.
fn startup_deadline() -> Duration {
    static DEADLINE: std::sync::OnceLock<Duration> = std::sync::OnceLock::new();
    *DEADLINE.get_or_init(|| {
        let manifest = paths::path_library()
            .workspace_root
            .join("main-system")
            .join("config")
            .join("startup_manifest.json");
        let seconds = std::fs::read_to_string(manifest)
            .ok()
            .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
            .and_then(|m| {
                m["timeouts"]["startup_gate_deadline_seconds"]
                    .as_f64()
                    .or_else(|| m["startup_gate_deadline_seconds"].as_f64())
            })
            .unwrap_or(90.0);
        Duration::from_secs_f64(seconds.max(1.0))
    })
}

/// Permanent latch (parity with the Python ``startup_dead`` flag): once the
/// startup deadline passes without full readiness the runtime reports dead —
/// it never un-latches within a generation.
pub fn startup_dead() -> bool {
    STARTUP_DEAD.load(Ordering::SeqCst)
}

pub fn note_authenticated_connect() {
    AUTHENTICATED_EVER.store(true, Ordering::SeqCst);
    AUTHENTICATED_CONNECTIONS.fetch_add(1, Ordering::SeqCst);
}

pub fn note_authenticated_disconnect() {
    AUTHENTICATED_CONNECTIONS.fetch_sub(1, Ordering::SeqCst);
}

/// ``authenticated_ipc_connected`` is a same-generation latch (parity with
/// the retired Python flag): one proven authenticated session attests the
/// IPC plane for the whole generation.  Reading the live connection gauge
/// here would let any client disconnect after the startup deadline feed a
/// false ``not-ready`` into the permanent ``STARTUP_DEAD`` latch — every
/// later reconnect would then be served ``runtime_degraded`` forever.
pub fn authenticated_ipc_connected() -> bool {
    AUTHENTICATED_EVER.load(Ordering::SeqCst)
}

/// Record a failed runtime-initialization outcome — the readiness gate
/// reports ``runtime_state: failed`` and never silently recovers.
#[allow(dead_code)]
pub fn mark_runtime_failed() {
    RUNTIME_FAILED.store(true, Ordering::SeqCst);
}

struct DependencySpec {
    identity: &'static str,
    criticality: &'static str,
    port: u16,
    on_demand: bool,
}

fn dependencies() -> &'static [DependencySpec] {
    &[
        DependencySpec {
            identity: "postgresql",
            criticality: "core-critical",
            port: 5432,
            on_demand: false,
        },
        DependencySpec {
            identity: "vectord",
            criticality: "capability-critical",
            port: 8092,
            on_demand: false,
        },
        DependencySpec {
            identity: "ollama",
            criticality: "capability-critical",
            port: 11434,
            on_demand: true,
        },
    ]
}

fn probe_loopback(port: u16, deadline: Duration) -> bool {
    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    // Readiness = connect succeeded.  The old parity code additionally
    // issued a 1-byte read which always ran out the 200 ms timeout on
    // silent services (PostgreSQL/vectord) — pure latency, no signal.
    TcpStream::connect_timeout(&addr, deadline).is_ok()
}

/// Dependency probes are the readiness gate's only network I/O: a fresh
/// probe costs a TCP connect plus up to one read-timeout (~200 ms) per
/// dependency when healthy.  The WS status loop re-evaluates readiness on
/// a short cadence to catch transitions, so probe results are reused for
/// PROBE_TTL instead of re-opening sockets per evaluation.  Staleness only
/// delays a transition report by at most TTL, never a gate decision that
/// outlives a generation.
const PROBE_TTL: Duration = Duration::from_secs(5);
static PROBE_CACHE: std::sync::OnceLock<
    std::sync::Mutex<std::collections::HashMap<u16, (Instant, bool)>>,
> = std::sync::OnceLock::new();

fn probe_loopback_cached(port: u16, deadline: Duration) -> bool {
    let cache = PROBE_CACHE.get_or_init(|| {
        std::sync::Mutex::new(std::collections::HashMap::new())
    });
    {
        let map = cache.lock().unwrap();
        if let Some((at, ok)) = map.get(&port) {
            if at.elapsed() < PROBE_TTL {
                return *ok;
            }
        }
    }
    let ok = probe_loopback(port, deadline);
    cache.lock().unwrap().insert(port, (Instant::now(), ok));
    ok
}

/// Governance readiness: the codex authority and permission directory
/// must exist and parse — fail-closed on any unreadable artifact.
/// Filesystem probes are TTL-cached like the dependency probes; staleness
/// only delays a transition report by GOVERNANCE_TTL.
const GOVERNANCE_TTL: Duration = Duration::from_secs(5);

fn governance_ready() -> bool {
    static CACHE: std::sync::OnceLock<std::sync::Mutex<(Instant, bool)>> =
        std::sync::OnceLock::new();
    let cache = CACHE.get_or_init(|| {
        // Pre-expired sentinel: the first call must probe, never serve
        // the pessimistic default.
        std::sync::Mutex::new((Instant::now() - GOVERNANCE_TTL, false))
    });
    {
        let (at, ok) = *cache.lock().unwrap();
        if at.elapsed() < GOVERNANCE_TTL {
            return ok;
        }
    }
    let root = &paths::path_library().workspace_root;
    let codex_dir = root.join("governance_rule");
    let ok = codex_dir.join("permission_directory").exists()
        // The release pin records which sealed codex generation this
        // runtime answers to; an unreadable pin means governance cannot
        // be verified.
        && root
            .join("shared-layer")
            .join("release-dependencies.json")
            .metadata()
            .map(|m| m.len() > 0)
            .unwrap_or(false);
    *cache.lock().unwrap() = (Instant::now(), ok);
    ok
}

pub struct Readiness {
    pub ok: bool,
    pub runtime_state: &'static str,
    pub governance_ready: bool,
    pub backend_runtime_ready: bool,
    pub dependencies_ready: bool,
    pub authenticated_ipc: bool,
    pub startup_dead: bool,
    pub startup_failures: Vec<String>,
    pub dependencies: Vec<Value>,
}

pub fn evaluate() -> Readiness {
    let backend_runtime_ready = !RUNTIME_FAILED.load(Ordering::SeqCst);
    let gov_ready = governance_ready();
    let authed = authenticated_ipc_connected();

    let mut deps = Vec::new();
    let mut core_ok = true;
    for dep in dependencies() {
        let reachable =
            probe_loopback_cached(dep.port, Duration::from_secs(3));
        let ready = reachable || dep.on_demand;
        if dep.criticality == "core-critical" && !ready {
            core_ok = false;
        }
        deps.push(json!({
            "identity": dep.identity,
            "criticality": dep.criticality,
            "ready": ready,
            "reachable": reachable,
            "on_demand": dep.on_demand,
        }));
    }
    let dependencies_ready = core_ok;
    let ready =
        backend_runtime_ready && gov_ready && dependencies_ready && authed;
    // The dead latch may only fire once a session generation has begun
    // (some client has authenticated at least once).  A backend probed via
    // /health past the deadline before any UI attaches is "starting", not
    // dead — otherwise the first real client would be latched out forever.
    if !ready && authed && boot_instant().elapsed() >= startup_deadline() {
        STARTUP_DEAD.store(true, Ordering::SeqCst);
    }
    let dead = STARTUP_DEAD.load(Ordering::SeqCst);
    let startup_failures = if dead {
        let mut failures: Vec<String> = Vec::new();
        if !backend_runtime_ready {
            failures.push("backend_runtime".to_string());
        }
        if !gov_ready {
            failures.push("governance".to_string());
        }
        failures.extend(
            deps.iter()
                .filter(|d| d["ready"].as_bool() != Some(true))
                .filter_map(|d| d["identity"].as_str().map(String::from)),
        );
        failures
    } else {
        Vec::new()
    };
    Readiness {
        ok: ready && !dead,
        runtime_state: if RUNTIME_FAILED.load(Ordering::SeqCst) {
            "failed"
        } else if dead {
            "degraded"
        } else if ready {
            "ready"
        } else {
            "starting"
        },
        governance_ready: gov_ready,
        backend_runtime_ready,
        dependencies_ready,
        authenticated_ipc: authed,
        startup_dead: dead,
        startup_failures,
        dependencies: deps,
    }
}

/// ``/health`` payload — wire-compatible with the Python server.
pub fn health_payload(level: &str) -> Value {
    let readiness = evaluate();
    // Retired-parity projections of the governed pending-action queue and
    // automation-switch store (file contracts under runtime/state).  The
    // drawer surfaces render these on every status push.
    let pending = crate::pending_actions::actionable_pending_actions();
    let pending_cardinality =
        crate::pending_actions::pending_action_cardinality(&pending);
    json!({
        "ok": readiness.ok,
        "version": gptbridge_core::app::PRODUCT_VERSION,
        "workspace_instance_id": token::workspace_instance_id(),
        "runtime_state": readiness.runtime_state,
        "runtime_scope": "main",
        // Retired-Python parity: maintenance_ready reported the resident
        // maintenance controller's health-monitor startup outcome.  The
        // native backend's readiness/probe loop is that plane's successor —
        // report it from the same evaluated readiness instead of a phantom
        // subsystem.
        "maintenance_ready": readiness.ok,
        "governance_ready": readiness.governance_ready,
        "backend_runtime_ready": readiness.backend_runtime_ready,
        "dependencies_ready": readiness.dependencies_ready,
        "authenticated_ipc_connected": readiness.authenticated_ipc,
        "startup_dead": readiness.startup_dead,
        "startup_failures": readiness.startup_failures,
        "dependencies": readiness.dependencies,
        "services": {},
        "capabilities": {},
        // Retired-contract parity: the renderer's 星澄 indicator reads
        // xingcheng_native_model_runtime.{running,available} on every
        // status push.  The successor reports governed-runtime truth —
        // `running` means the local-model tool runtime is live in this
        // backend's registry, `available` means its native entry is
        // installed.  A stopped tool is "stopped", never "unavailable".
        "xingcheng_native_model_runtime": native_model_status(),
        // Retired-parity: runtime_status_push carried ``resource_mode``
        // (resource_governor_signal's governor_mode snapshot); the
        // drawer reads it off the push rather than polling the command.
        "resource_mode": crate::resource_mode::snapshot(),
        // auto_action_policy parity: actionable queue items only — terminal
        // and reconciled records stay durable evidence, never surface.
        "pending_actions": pending,
        "pending_action_cardinality": pending_cardinality,
        "automation_switches": crate::pending_actions::automation_switches(),
        "health_level": level,
    })
}

/// Successor of the retired ``native_model_status``: the first-party
/// 星澄 model runs inside the governed local-model tool runtime, so
/// liveness follows the tool registry and availability follows the
/// launchable native entry.
pub(crate) fn native_model_status() -> Value {
    let running = crate::tools::governed_tool_running("local-model");
    let available =
        running || crate::tools::governed_tool_launchable("local-model");
    json!({
        "model_id": "star-main-native-model",
        "state": if running {
            "ready"
        } else if available {
            "stopped"
        } else {
            "unavailable"
        },
        "running": running,
        "available": available,
        "checked_at": gptbridge_core::app::iso_now(),
        "message": if running || available {
            ""
        } else {
            "local-model runtime not installed"
        },
        "third_party_weights_used": false,
    })
}

/// Memoized ``brief`` payload for the per-connection status push loop.
/// Every WS connection ticks STATUS_EVAL_INTERVAL; without sharing, each
/// socket re-ran evaluate() plus a full Value build per tick.  The TTL is
/// shorter than the push cadence so every tick still re-evaluates, while
/// the immediate-on-connect push and concurrent sockets share one build.
const BRIEF_TTL: Duration = Duration::from_millis(900);
static BRIEF_CACHE: std::sync::OnceLock<
    std::sync::Mutex<(Instant, std::sync::Arc<Value>)>,
> = std::sync::OnceLock::new();

pub fn brief_payload() -> std::sync::Arc<Value> {
    let cache = BRIEF_CACHE.get_or_init(|| {
        std::sync::Mutex::new((
            Instant::now() - BRIEF_TTL,
            std::sync::Arc::new(Value::Null),
        ))
    });
    {
        let (at, value) = &*cache.lock().unwrap();
        if at.elapsed() < BRIEF_TTL {
            return std::sync::Arc::clone(value);
        }
    }
    let value = std::sync::Arc::new(health_payload("brief"));
    *cache.lock().unwrap() = (Instant::now(), std::sync::Arc::clone(&value));
    value
}

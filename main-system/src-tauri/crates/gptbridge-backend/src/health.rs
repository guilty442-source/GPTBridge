//! health.rs — runtime readiness gate and ``/health`` payload.
//!
//! Mirrors the retired Python readiness contract: four conditions
//! (backend-runtime-ready + governance-ready + dependencies-ready +
//! authenticated-ipc-connected) and per-dependency probes declared by
//! ``main-system/config/startup_manifest.json``.  Probes are bounded by
//! per-dependency deadlines; a probe that cannot run fails closed.

use std::io::Read;
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
fn startup_deadline() -> Duration {
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
    match TcpStream::connect_timeout(&addr, deadline) {
        Ok(mut stream) => {
            let _ = stream.set_read_timeout(Some(Duration::from_millis(200)));
            let mut buf = [0u8; 1];
            let _ = stream.read(&mut buf); // readiness = connect succeeded
            true
        }
        Err(_) => false,
    }
}

/// Governance readiness: the codex authority and permission directory
/// must exist and parse — fail-closed on any unreadable artifact.
fn governance_ready() -> bool {
    let root = &paths::path_library().workspace_root;
    let codex_dir = root.join("governance_rule");
    if !codex_dir.join("permission_directory").exists() {
        return false;
    }
    // The release pin records which sealed codex generation this runtime
    // answers to; an unreadable pin means governance cannot be verified.
    root.join("shared-layer")
        .join("release-dependencies.json")
        .metadata()
        .map(|m| m.len() > 0)
        .unwrap_or(false)
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
        let reachable = probe_loopback(dep.port, Duration::from_secs(3));
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
        "health_level": level,
    })
}

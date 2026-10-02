//! xc-runtime-host CLI — operator surface for the Active Model lease.
//!
//!   xc-runtime-host status --ipc <dir>
//!   xc-runtime-host claim --ipc <dir> --pid <n> --exe <name> --bundle <dir> [resource opts]
//!   xc-runtime-host heartbeat --ipc <dir> --pid <n>
//!   xc-runtime-host release --ipc <dir> --pid <n>
//!   xc-runtime-host takeover --ipc <dir> --pid <n> --exe <name> --bundle <dir> --owner-dead [resource opts]
//!   xc-runtime-host slot --ipc <dir> --pid <n> --runtime <kind> --state <state> [--child-pid <m>] [--exe <e>]
//!
//! Resource opts: --weights-sha <hex> --tokenizer-sha <hex> --device <s>
//!   --threads <n> --kv-cache-mb <n> [--prefix-cache/--no-prefix-cache]
//!   [--lease-timeout-s <n>] [--logs <dir>]
//!
//! Exit codes mirror xc-eval/xc-format: 0 = done, 2 = lawful refusal
//! (second owner, not owner, stale-takeover guard), 1 = tool/IO/schema
//! error. Denied claims are audited — a second owner attempting a claim
//! leaves forensics, it never silently passes.
//!
//! `--owner-dead` is an attestation, not a probe: the caller verifies
//! death through its own platform API first (C# `Process`, Win32).
//! This binary never guesses liveness.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::{SystemTime, UNIX_EPOCH};
use xc_runtime_host::{
    audit, claim, clear, heartbeat, load, release, slot_set, store, takeover,
    ClaimOutcome, ClaimRequest, Json, RuntimeKind, SlotState, TakeoverOutcome,
    HOST_FORMAT,
};

fn now_s() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn err_json(code: &str, msg: &str) -> String {
    let esc = msg.replace('\\', "\\\\").replace('"', "\\\"");
    format!(
        "{{\"ok\":false,\"format\":\"{HOST_FORMAT}\",\"error_code\":\"{code}\",\"error\":\"{esc}\"}}"
    )
}

fn fail(code: &str, msg: String) -> ExitCode {
    println!("{}", err_json(code, &msg));
    ExitCode::from(1)
}

fn refused(outcome: &str, detail: Vec<(String, Json)>) -> ExitCode {
    let mut pairs = vec![
        ("ok".to_string(), Json::Bool(true)),
        ("format".to_string(), Json::Str(HOST_FORMAT.to_string())),
        ("outcome".to_string(), Json::Str(outcome.to_string())),
    ];
    pairs.extend(detail);
    println!("{}", xc_runtime_host::render(&Json::Obj(pairs)));
    ExitCode::from(2)
}

struct Args {
    flags: std::collections::HashSet<String>,
    opts: HashMap<String, String>,
}

fn parse_args(argv: &[String]) -> Result<(String, Args), String> {
    if argv.len() < 2 {
        return Err("usage: xc-runtime-host <status|claim|heartbeat|release|takeover|slot> ...".to_string());
    }
    let cmd = argv[1].clone();
    let mut flags = std::collections::HashSet::new();
    let mut opts = HashMap::new();
    let mut i = 2;
    while i < argv.len() {
        let a = &argv[i];
        if !a.starts_with("--") {
            return Err(format!("unexpected positional '{a}'"));
        }
        let key = a[2..].to_string();
        if i + 1 < argv.len() && !argv[i + 1].starts_with("--") {
            opts.insert(key, argv[i + 1].clone());
            i += 2;
        } else {
            flags.insert(key);
            i += 1;
        }
    }
    Ok((cmd, Args { flags, opts }))
}

fn need(opts: &HashMap<String, String>, key: &str) -> Result<String, String> {
    opts.get(key)
        .cloned()
        .filter(|v| !v.is_empty())
        .ok_or_else(|| format!("missing --{key}"))
}

fn need_u32(opts: &HashMap<String, String>, key: &str) -> Result<u32, String> {
    need(opts, key)?
        .parse::<u32>()
        .map_err(|_| format!("--{key} must be a non-negative integer"))
}

fn opt_u32(opts: &HashMap<String, String>, key: &str, dflt: u32) -> Result<u32, String> {
    match opts.get(key) {
        None => Ok(dflt),
        Some(v) => v
            .parse::<u32>()
            .map_err(|_| format!("--{key} must be a non-negative integer")),
    }
}

fn ipc_dir(opts: &HashMap<String, String>) -> Result<PathBuf, String> {
    need(opts, "ipc").map(PathBuf::from)
}

fn logs_dir(opts: &HashMap<String, String>, ipc: &Path) -> PathBuf {
    if let Some(l) = opts.get("logs") {
        return PathBuf::from(l);
    }
    // <tool>/xingcheng/runtime/ipc -> <tool>/xingcheng/runtime/logs
    ipc.parent()
        .map(|p| p.join("logs"))
        .unwrap_or_else(|| PathBuf::from("logs"))
}

fn do_audit(
    opts: &HashMap<String, String>,
    ipc: &Path,
    event: &str,
    fields: Vec<(String, Json)>,
) {
    let logs = logs_dir(opts, ipc);
    if let Err(e) = audit(&logs, now_s(), event, fields) {
        eprintln!("xc-runtime-host: audit append failed: {e}");
    }
}

fn claim_request(
    opts: &HashMap<String, String>,
    pid: u32,
) -> Result<ClaimRequest, String> {
    Ok(ClaimRequest {
        claimant_pid: pid,
        claimant_exe: need(opts, "exe")?,
        bundle_dir: need(opts, "bundle")?,
        weights_sha256: opts.get("weights-sha").cloned().unwrap_or_default(),
        tokenizer_sha256: opts.get("tokenizer-sha").cloned().unwrap_or_default(),
        device: opts.get("device").cloned().unwrap_or_else(|| "cpu-native".to_string()),
        thread_pool_size: opt_u32(opts, "threads", 0)?,
        kv_cache_mb: opt_u32(opts, "kv-cache-mb", 0)?,
        prefix_cache: !opts.contains_key("no-prefix-cache")
            && opts
                .get("prefix-cache")
                .is_none_or(|v| v != "0" && v.to_lowercase() != "false"),
        lease_timeout_s: opt_u32(opts, "lease-timeout-s", 0).map(|v| v as u64)?,
    })
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().collect();
    let (cmd, args) = match parse_args(&argv) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("usage: xc-runtime-host <status|claim|heartbeat|release|takeover|slot> [--ipc <dir>] ...");
            return fail("HOST_USAGE", e);
        }
    };
    match cmd.as_str() {
        "status" => cmd_status(&args),
        "claim" => cmd_claim(&args),
        "heartbeat" => cmd_heartbeat(&args),
        "release" => cmd_release(&args),
        "takeover" => cmd_takeover(&args),
        "slot" => cmd_slot(&args),
        _ => fail("HOST_USAGE", format!("unknown command '{cmd}'")),
    }
}

fn cmd_status(args: &Args) -> ExitCode {
    let ipc = match ipc_dir(&args.opts) {
        Ok(p) => p,
        Err(e) => return fail("HOST_USAGE", e),
    };
    match load(&ipc) {
        Ok(None) => {
            println!(
                "{{\"ok\":true,\"format\":\"{HOST_FORMAT}\",\"active\":false,\"record\":null}}"
            );
            ExitCode::from(0)
        }
        Ok(Some(rec)) => {
            let age = rec.heartbeat_age_s(now_s());
            println!(
                "{{\"ok\":true,\"format\":\"{HOST_FORMAT}\",\"active\":true,\"heartbeat_age_s\":{age},\"lease_expired\":{},\"record\":{}}}",
                rec.lease_expired(now_s()),
                xc_runtime_host::render(&xc_runtime_host::record_to_json(&rec))
            );
            ExitCode::from(0)
        }
        Err(e) => fail("HOST_RECORD_INVALID", e),
    }
}

fn cmd_claim(args: &Args) -> ExitCode {
    let ipc = match ipc_dir(&args.opts) {
        Ok(p) => p,
        Err(e) => return fail("HOST_USAGE", e),
    };
    let pid = match need_u32(&args.opts, "pid") {
        Ok(p) => p,
        Err(e) => return fail("HOST_USAGE", e),
    };
    let req = match claim_request(&args.opts, pid) {
        Ok(r) => r,
        Err(e) => return fail("HOST_USAGE", e),
    };
    let current = match load(&ipc) {
        Ok(c) => c,
        Err(e) => return fail("HOST_RECORD_INVALID", e),
    };
    let now = now_s();
    match claim(current, &req, now) {
        (Some(rec), ClaimOutcome::Claimed) => {
            if let Err(e) = store(&ipc, &rec) {
                return fail("HOST_STORE_FAILED", e);
            }
            do_audit(
                &args.opts,
                &ipc,
                "claimed",
                vec![
                    ("owner_pid".to_string(), Json::Num(pid as i64)),
                    ("bundle_dir".to_string(), Json::Str(req.bundle_dir.clone())),
                ],
            );
            println!(
                "{{\"ok\":true,\"format\":\"{HOST_FORMAT}\",\"outcome\":\"claimed\",\"owner_pid\":{pid}}}"
            );
            ExitCode::from(0)
        }
        (Some(rec), ClaimOutcome::Refreshed) => {
            if let Err(e) = store(&ipc, &rec) {
                return fail("HOST_STORE_FAILED", e);
            }
            println!(
                "{{\"ok\":true,\"format\":\"{HOST_FORMAT}\",\"outcome\":\"refreshed\",\"owner_pid\":{pid}}}"
            );
            ExitCode::from(0)
        }
        (None, ClaimOutcome::DeniedActiveOwner { owner_pid, heartbeat_age_s }) => {
            do_audit(
                &args.opts,
                &ipc,
                "claim-denied",
                vec![
                    ("claimant_pid".to_string(), Json::Num(pid as i64)),
                    ("owner_pid".to_string(), Json::Num(owner_pid as i64)),
                ],
            );
            refused(
                "denied-active-owner",
                vec![
                    ("owner_pid".to_string(), Json::Num(owner_pid as i64)),
                    ("heartbeat_age_s".to_string(), Json::Num(heartbeat_age_s as i64)),
                ],
            )
        }
        _ => fail("HOST_INTERNAL", "unreachable claim branch".to_string()),
    }
}

fn cmd_heartbeat(args: &Args) -> ExitCode {
    let ipc = match ipc_dir(&args.opts) {
        Ok(p) => p,
        Err(e) => return fail("HOST_USAGE", e),
    };
    let pid = match need_u32(&args.opts, "pid") {
        Ok(p) => p,
        Err(e) => return fail("HOST_USAGE", e),
    };
    let mut rec = match load(&ipc) {
        Ok(Some(r)) => r,
        Ok(None) => {
            return refused("no-active-model", vec![]);
        }
        Err(e) => return fail("HOST_RECORD_INVALID", e),
    };
    if !heartbeat(&mut rec, pid, now_s()) {
        return refused(
            "not-owner",
            vec![("owner_pid".to_string(), Json::Num(rec.owner_pid as i64))],
        );
    }
    if let Err(e) = store(&ipc, &rec) {
        return fail("HOST_STORE_FAILED", e);
    }
    println!(
        "{{\"ok\":true,\"format\":\"{HOST_FORMAT}\",\"outcome\":\"heartbeat\",\"owner_pid\":{pid}}}"
    );
    ExitCode::from(0)
}

fn cmd_release(args: &Args) -> ExitCode {
    let ipc = match ipc_dir(&args.opts) {
        Ok(p) => p,
        Err(e) => return fail("HOST_USAGE", e),
    };
    let pid = match need_u32(&args.opts, "pid") {
        Ok(p) => p,
        Err(e) => return fail("HOST_USAGE", e),
    };
    let rec = match load(&ipc) {
        Ok(Some(r)) => r,
        Ok(None) => {
            println!(
                "{{\"ok\":true,\"format\":\"{HOST_FORMAT}\",\"outcome\":\"released\",\"was_active\":false}}"
            );
            return ExitCode::from(0);
        }
        Err(e) => return fail("HOST_RECORD_INVALID", e),
    };
    if !release(&rec, pid) {
        return refused(
            "not-owner",
            vec![("owner_pid".to_string(), Json::Num(rec.owner_pid as i64))],
        );
    }
    if let Err(e) = clear(&ipc) {
        return fail("HOST_STORE_FAILED", e);
    }
    do_audit(
        &args.opts,
        &ipc,
        "released",
        vec![("owner_pid".to_string(), Json::Num(pid as i64))],
    );
    println!(
        "{{\"ok\":true,\"format\":\"{HOST_FORMAT}\",\"outcome\":\"released\",\"was_active\":true}}"
    );
    ExitCode::from(0)
}

fn cmd_takeover(args: &Args) -> ExitCode {
    let ipc = match ipc_dir(&args.opts) {
        Ok(p) => p,
        Err(e) => return fail("HOST_USAGE", e),
    };
    if !args.flags.contains("owner-dead") {
        return fail(
            "HOST_USAGE",
            "takeover requires --owner-dead attestation (verify death via platform API first)".to_string(),
        );
    }
    let pid = match need_u32(&args.opts, "pid") {
        Ok(p) => p,
        Err(e) => return fail("HOST_USAGE", e),
    };
    let req = match claim_request(&args.opts, pid) {
        Ok(r) => r,
        Err(e) => return fail("HOST_USAGE", e),
    };
    let current = match load(&ipc) {
        Ok(Some(r)) => r,
        Ok(None) => {
            // Nothing to take over: plain claim path.
            let now = now_s();
            let (rec, _) = claim(None, &req, now);
            let rec = rec.expect("fresh claim");
            if let Err(e) = store(&ipc, &rec) {
                return fail("HOST_STORE_FAILED", e);
            }
            println!(
                "{{\"ok\":true,\"format\":\"{HOST_FORMAT}\",\"outcome\":\"claimed\",\"owner_pid\":{pid}}}"
            );
            return ExitCode::from(0);
        }
        Err(e) => return fail("HOST_RECORD_INVALID", e),
    };
    match takeover(&current, &req, true, now_s()) {
        TakeoverOutcome::TookOver(rec) => {
            if let Err(e) = store(&ipc, &rec) {
                return fail("HOST_STORE_FAILED", e);
            }
            do_audit(
                &args.opts,
                &ipc,
                "took-over",
                vec![
                    ("owner_pid".to_string(), Json::Num(pid as i64)),
                    (
                        "previous_owner_pid".to_string(),
                        Json::Num(current.owner_pid as i64),
                    ),
                ],
            );
            println!(
                "{{\"ok\":true,\"format\":\"{HOST_FORMAT}\",\"outcome\":\"took-over\",\"owner_pid\":{pid},\"previous_owner_pid\":{}}}",
                current.owner_pid
            );
            ExitCode::from(0)
        }
        TakeoverOutcome::DeniedOwnerPossiblyLive { .. } => {
            fail("HOST_INTERNAL", "attestation flag set but denial raised".to_string())
        }
        TakeoverOutcome::DeniedFreshHeartbeat { owner_pid, heartbeat_age_s } => refused(
            "denied-fresh-heartbeat",
            vec![
                ("owner_pid".to_string(), Json::Num(owner_pid as i64)),
                ("heartbeat_age_s".to_string(), Json::Num(heartbeat_age_s as i64)),
            ],
        ),
    }
}

fn cmd_slot(args: &Args) -> ExitCode {
    let ipc = match ipc_dir(&args.opts) {
        Ok(p) => p,
        Err(e) => return fail("HOST_USAGE", e),
    };
    let pid = match need_u32(&args.opts, "pid") {
        Ok(p) => p,
        Err(e) => return fail("HOST_USAGE", e),
    };
    let kind = match args.opts.get("runtime").and_then(|s| RuntimeKind::parse(s)) {
        Some(k) => k,
        None => {
            return fail(
                "HOST_USAGE",
                "missing/invalid --runtime (one of the six runtime kinds)".to_string(),
            )
        }
    };
    let state = match args.opts.get("state").and_then(|s| SlotState::parse(s)) {
        Some(s) => s,
        None => {
            return fail(
                "HOST_USAGE",
                "missing/invalid --state (absent|starting|ready|draining|down)".to_string(),
            )
        }
    };
    let child_pid = match args.opts.get("child-pid") {
        None => None,
        Some(v) => match v.parse::<u32>() {
            Ok(n) => Some(n),
            Err(_) => return fail("HOST_USAGE", "--child-pid must be an integer".to_string()),
        },
    };
    let exe = args.opts.get("exe").cloned().unwrap_or_default();
    let mut rec = match load(&ipc) {
        Ok(Some(r)) => r,
        Ok(None) => return refused("no-active-model", vec![]),
        Err(e) => return fail("HOST_RECORD_INVALID", e),
    };
    if !slot_set(&mut rec, pid, kind, state, child_pid, &exe) {
        return refused(
            "not-owner",
            vec![("owner_pid".to_string(), Json::Num(rec.owner_pid as i64))],
        );
    }
    if let Err(e) = store(&ipc, &rec) {
        return fail("HOST_STORE_FAILED", e);
    }
    println!(
        "{{\"ok\":true,\"format\":\"{HOST_FORMAT}\",\"outcome\":\"slot-updated\",\"runtime\":\"{}\",\"state\":\"{}\"}}",
        kind.as_str(),
        state.as_str()
    );
    ExitCode::from(0)
}

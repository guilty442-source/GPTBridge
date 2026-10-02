//! xc-runtime-host — Xingcheng Runtime Host contract (`star-runtime-host/v1`).
//!
//! One machine, one Active Model, one owner. The six runtimes hang off a
//! single host record instead of each tool reloading weights, tokenizer,
//! caches and thread pools on its own:
//!
//! ```text
//! XingchengRuntimeHost          this crate (contract + lease + discovery)
//! ├─ NativeModelRuntime       C++    inference engine (resident serve worker, phase 2)
//! ├─ NativeTrainer            C++    xingcheng_trainer, on-demand, single-flight
//! ├─ NativeKernelRuntime      C++/C  kernels (CUDA + native core), shared pool sizing
//! ├─ NativeDataRuntime        Rust   dataset/snapshot/tokenizer-format IO (phase 2)
//! ├─ NativeArtifactStore      Rust   content-hash bundle custody (phase 2)
//! └─ GovernanceHost           C#     xc-learning: policy, lifecycle, scheduling
//! ```
//!
//! Shared-once resources (weights bytes + sha, tokenizer + sha, cache
//! config, thread-pool size, device) are recorded on the Active Model at
//! claim time; every runtime reuses the record instead of re-resolving.
//!
//! Single-owner enforcement is fail-closed and never guesses liveness:
//! a second claimant while a record names another pid is DENIED, even if
//! that pid looks dead — takeover requires the caller to attest death
//! through its own platform API (C# `Process`, Win32) AND a stale
//! heartbeat past the lease timeout. A corrupt record is an error, never
//! silent absence (absence would admit a second owner).
//!
//! Language-architecture placement (2026-10-02, human-governor directive):
//! supervision/lifecycle/IPC ownership is Rust. OS pid probing stays in
//! the caller lanes. B166 remains the sole language authority.

use std::fs;
use std::path::{Path, PathBuf};

/// Contract format identifier.
pub const HOST_FORMAT: &str = "star-runtime-host/v1";
/// Discovery file name under `<tool>/xingcheng/runtime/ipc/`.
pub const DESCRIPTOR_FILE: &str = "runtime-host.json";
/// Audit ledger name under `<tool>/xingcheng/runtime/logs/`.
pub const AUDIT_FILE: &str = "runtime-host-audit.jsonl";
/// Heartbeat staleness bound (s). Past this, the lease is expired —
/// expiry alone never transfers ownership (takeover still needs the
/// death attestation).
pub const LEASE_TIMEOUT_S: u64 = 120;

// ---------------------------------------------------------------- kinds --

/// The six runtime slots. Exactly one slot per kind lives on the record.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuntimeKind {
    NativeModelRuntime,
    NativeTrainer,
    NativeKernelRuntime,
    NativeDataRuntime,
    NativeArtifactStore,
    GovernanceHost,
}

impl RuntimeKind {
    pub const ALL: [RuntimeKind; 6] = [
        RuntimeKind::NativeModelRuntime,
        RuntimeKind::NativeTrainer,
        RuntimeKind::NativeKernelRuntime,
        RuntimeKind::NativeDataRuntime,
        RuntimeKind::NativeArtifactStore,
        RuntimeKind::GovernanceHost,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            RuntimeKind::NativeModelRuntime => "native-model-runtime",
            RuntimeKind::NativeTrainer => "native-trainer",
            RuntimeKind::NativeKernelRuntime => "native-kernel-runtime",
            RuntimeKind::NativeDataRuntime => "native-data-runtime",
            RuntimeKind::NativeArtifactStore => "native-artifact-store",
            RuntimeKind::GovernanceHost => "governance-host",
        }
    }

    pub fn parse(s: &str) -> Option<RuntimeKind> {
        RuntimeKind::ALL
            .iter()
            .copied()
            .find(|k| k.as_str() == s)
    }
}

/// Slot lifecycle. `Ready` means the runtime holds its shared resources
/// (no reload needed by late joiners).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SlotState {
    Absent,
    Starting,
    Ready,
    Draining,
    Down,
}

impl SlotState {
    pub fn as_str(self) -> &'static str {
        match self {
            SlotState::Absent => "absent",
            SlotState::Starting => "starting",
            SlotState::Ready => "ready",
            SlotState::Draining => "draining",
            SlotState::Down => "down",
        }
    }

    pub fn parse(s: &str) -> Option<SlotState> {
        match s {
            "absent" => Some(SlotState::Absent),
            "starting" => Some(SlotState::Starting),
            "ready" => Some(SlotState::Ready),
            "draining" => Some(SlotState::Draining),
            "down" => Some(SlotState::Down),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeSlot {
    pub kind: RuntimeKind,
    pub state: SlotState,
    /// OS pid of the slot process, if spawned.
    pub pid: Option<u32>,
    /// Executable backing the slot (informational).
    pub exe: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SharedResources {
    pub bundle_dir: String,
    pub weights_sha256: String,
    pub tokenizer_sha256: String,
    pub device: String,
    pub thread_pool_size: u32,
    pub kv_cache_mb: u32,
    pub prefix_cache: bool,
}

/// The single Active Model record. Epoch seconds throughout (no TZ code).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActiveModel {
    pub owner_pid: u32,
    pub owner_exe: String,
    pub claimed_at_s: u64,
    pub heartbeat_at_s: u64,
    pub lease_timeout_s: u64,
    pub shared: SharedResources,
    /// Exactly six slots, one per [`RuntimeKind`], in [`RuntimeKind::ALL`] order.
    pub runtimes: Vec<RuntimeSlot>,
}

impl ActiveModel {
    pub fn heartbeat_age_s(&self, now_s: u64) -> u64 {
        now_s.saturating_sub(self.heartbeat_at_s)
    }

    pub fn lease_expired(&self, now_s: u64) -> bool {
        self.heartbeat_age_s(now_s) > self.lease_timeout_s
    }

    pub fn slot(&self, kind: RuntimeKind) -> Option<&RuntimeSlot> {
        self.runtimes.iter().find(|s| s.kind == kind)
    }
}

// ----------------------------------------------------------------- JSON --

// Minimal JSON model: enough for this schema, nothing more. Numbers are
// i64 (epoch secs, pids, sizes all fit); floats never appear on this
// contract — a float where an integer belongs is a schema error.

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Json {
    Null,
    Bool(bool),
    Num(i64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
}

fn esc_into(out: &mut String, s: &str) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

pub fn render(value: &Json) -> String {
    let mut out = String::new();
    render_into(&mut out, value);
    out
}

fn render_into(out: &mut String, value: &Json) {
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(true) => out.push_str("true"),
        Json::Bool(false) => out.push_str("false"),
        Json::Num(n) => out.push_str(&n.to_string()),
        Json::Str(s) => esc_into(out, s),
        Json::Arr(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                render_into(out, item);
            }
            out.push(']');
        }
        Json::Obj(pairs) => {
            out.push('{');
            for (i, (k, v)) in pairs.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                esc_into(out, k);
                out.push(':');
                render_into(out, v);
            }
            out.push('}');
        }
    }
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

pub fn parse(text: &str) -> Result<Json, String> {
    let mut p = Parser {
        bytes: text.as_bytes(),
        pos: 0,
    };
    p.skip_ws();
    let v = p.value()?;
    p.skip_ws();
    if p.pos != p.bytes.len() {
        return Err(format!("trailing bytes at {}", p.pos));
    }
    Ok(v)
}

impl<'a> Parser<'a> {
    fn skip_ws(&mut self) {
        while self.pos < self.bytes.len()
            && matches!(self.bytes[self.pos], b' ' | b'\t' | b'\n' | b'\r')
        {
            self.pos += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn expect(&mut self, b: u8, what: &str) -> Result<(), String> {
        if self.peek() == Some(b) {
            self.pos += 1;
            Ok(())
        } else {
            Err(format!("expected {what} at {}", self.pos))
        }
    }

    fn value(&mut self) -> Result<Json, String> {
        match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Json::Str(self.string()?)),
            Some(b't') => self.literal("true", Json::Bool(true)),
            Some(b'f') => self.literal("false", Json::Bool(false)),
            Some(b'n') => self.literal("null", Json::Null),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            _ => Err(format!("unexpected value at {}", self.pos)),
        }
    }

    fn literal(&mut self, word: &str, v: Json) -> Result<Json, String> {
        if self.bytes.len() >= self.pos + word.len()
            && &self.bytes[self.pos..self.pos + word.len()] == word.as_bytes()
        {
            self.pos += word.len();
            Ok(v)
        } else {
            Err(format!("bad literal at {}", self.pos))
        }
    }

    fn number(&mut self) -> Result<Json, String> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        while self.peek().is_some_and(|c| c.is_ascii_digit()) {
            self.pos += 1;
        }
        // Contract integers only: a fraction/exponent is a schema error.
        if self.peek().is_some_and(|c| c == b'.' || c == b'e' || c == b'E') {
            return Err(format!("non-integer number at {start}"));
        }
        let s = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|e| e.to_string())?;
        s.parse::<i64>()
            .map(Json::Num)
            .map_err(|_| format!("integer out of range at {start}"))
    }

    fn string(&mut self) -> Result<String, String> {
        self.expect(b'"', "opening quote")?;
        let mut out = String::new();
        loop {
            let c = self.peek().ok_or("unterminated string")?;
            match c {
                b'"' => {
                    self.pos += 1;
                    return Ok(out);
                }
                b'\\' => {
                    self.pos += 1;
                    match self.peek().ok_or("bad escape")? {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            if self.pos + 5 > self.bytes.len() {
                                return Err("bad \\u escape".to_string());
                            }
                            let hex = std::str::from_utf8(
                                &self.bytes[self.pos + 1..self.pos + 5],
                            )
                            .map_err(|e| e.to_string())?;
                            let cp = u32::from_str_radix(hex, 16)
                                .map_err(|_| "bad \\u escape".to_string())?;
                            out.push(
                                char::from_u32(cp).ok_or("bad codepoint")?,
                            );
                            self.pos += 5;
                            continue;
                        }
                        _ => return Err(format!("bad escape at {}", self.pos)),
                    }
                    self.pos += 1;
                }
                _ => {
                    // UTF-8 passthrough: consume the full code point.
                    let rest = &self.bytes[self.pos..];
                    let s = std::str::from_utf8(rest)
                        .map_err(|_| format!("bad utf8 at {}", self.pos))?;
                    let ch = s.chars().next().ok_or("unterminated string")?;
                    // Raw control characters are rejected (must be escaped).
                    if (ch as u32) < 0x20 {
                        return Err(format!(
                            "unescaped control at {}",
                            self.pos
                        ));
                    }
                    out.push(ch);
                    self.pos += ch.len_utf8();
                }
            }
        }
    }

    fn array(&mut self) -> Result<Json, String> {
        self.expect(b'[', "'['")?;
        let mut items = Vec::new();
        loop {
            self.skip_ws();
            if self.peek() == Some(b']') {
                self.pos += 1;
                return Ok(Json::Arr(items));
            }
            items.push(self.value()?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b']') => {
                    self.pos += 1;
                    return Ok(Json::Arr(items));
                }
                _ => return Err(format!("expected , or ] at {}", self.pos)),
            }
        }
    }

    fn object(&mut self) -> Result<Json, String> {
        self.expect(b'{', "'{'")?;
        let mut pairs = Vec::new();
        loop {
            self.skip_ws();
            if self.peek() == Some(b'}') {
                self.pos += 1;
                return Ok(Json::Obj(pairs));
            }
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(format!("expected key at {}", self.pos));
            }
            let key = self.string()?;
            self.skip_ws();
            self.expect(b':', "':'")?;
            self.skip_ws();
            let val = self.value()?;
            pairs.push((key, val));
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(Json::Obj(pairs));
                }
                _ => return Err(format!("expected , or }} at {}", self.pos)),
            }
        }
    }
}

// ------------------------------------------------------------ schema --

fn req_str(obj: &Json, key: &str) -> Result<String, String> {
    match obj.get(key) {
        Some(Json::Str(s)) => Ok(s.clone()),
        _ => Err(format!("field '{key}' must be a string")),
    }
}

fn req_i64(obj: &Json, key: &str) -> Result<i64, String> {
    match obj.get(key) {
        Some(Json::Num(n)) if *n >= 0 => Ok(*n),
        _ => Err(format!("field '{key}' must be a non-negative integer")),
    }
}

fn req_bool(obj: &Json, key: &str) -> Result<bool, String> {
    match obj.get(key) {
        Some(Json::Bool(b)) => Ok(*b),
        _ => Err(format!("field '{key}' must be a boolean")),
    }
}

fn slot_to_json(s: &RuntimeSlot) -> Json {
    Json::Obj(vec![
        ("kind".to_string(), Json::Str(s.kind.as_str().to_string())),
        ("state".to_string(), Json::Str(s.state.as_str().to_string())),
        (
            "pid".to_string(),
            s.pid.map(|p| Json::Num(p as i64)).unwrap_or(Json::Null),
        ),
        ("exe".to_string(), Json::Str(s.exe.clone())),
    ])
}

fn slot_from_json(v: &Json) -> Result<RuntimeSlot, String> {
    let kind = req_str(v, "kind")?;
    let state = req_str(v, "state")?;
    let pid = match v.get("pid") {
        None | Some(Json::Null) => None,
        Some(Json::Num(n)) if *n >= 0 && *n <= u32::MAX as i64 => {
            Some(*n as u32)
        }
        _ => return Err("field 'pid' must be null or a pid integer".to_string()),
    };
    Ok(RuntimeSlot {
        kind: RuntimeKind::parse(&kind)
            .ok_or_else(|| format!("unknown runtime kind '{kind}'"))?,
        state: SlotState::parse(&state)
            .ok_or_else(|| format!("unknown slot state '{state}'"))?,
        pid,
        exe: req_str(v, "exe")?,
    })
}

pub fn record_to_json(rec: &ActiveModel) -> Json {
    let mut pairs = vec![
        ("format".to_string(), Json::Str(HOST_FORMAT.to_string())),
        ("owner_pid".to_string(), Json::Num(rec.owner_pid as i64)),
        ("owner_exe".to_string(), Json::Str(rec.owner_exe.clone())),
        ("claimed_at_s".to_string(), Json::Num(rec.claimed_at_s as i64)),
        ("heartbeat_at_s".to_string(), Json::Num(rec.heartbeat_at_s as i64)),
        (
            "lease_timeout_s".to_string(),
            Json::Num(rec.lease_timeout_s as i64),
        ),
    ];
    let sh = &rec.shared;
    for (k, v) in [
        ("bundle_dir", Json::Str(sh.bundle_dir.clone())),
        ("weights_sha256", Json::Str(sh.weights_sha256.clone())),
        ("tokenizer_sha256", Json::Str(sh.tokenizer_sha256.clone())),
        ("device", Json::Str(sh.device.clone())),
        ("thread_pool_size", Json::Num(sh.thread_pool_size as i64)),
        ("kv_cache_mb", Json::Num(sh.kv_cache_mb as i64)),
        ("prefix_cache", Json::Bool(sh.prefix_cache)),
    ] {
        pairs.push((k.to_string(), v));
    }
    pairs.push((
        "runtimes".to_string(),
        Json::Arr(rec.runtimes.iter().map(slot_to_json).collect()),
    ));
    Json::Obj(pairs)
}

pub fn record_from_json(v: &Json) -> Result<ActiveModel, String> {
    match v.get("format") {
        Some(Json::Str(f)) if f == HOST_FORMAT => {}
        _ => {
            return Err(format!(
                "expected format '{HOST_FORMAT}'"
            ))
        }
    }
    let runtimes = match v.get("runtimes") {
        Some(Json::Arr(items)) => items
            .iter()
            .map(slot_from_json)
            .collect::<Result<Vec<_>, _>>()?,
        _ => return Err("field 'runtimes' must be an array".to_string()),
    };
    // Exactly one slot per kind, in canonical order — anything else is a
    // corrupt record, rejected rather than repaired.
    let kinds: Vec<RuntimeKind> = runtimes.iter().map(|s| s.kind).collect();
    if kinds != RuntimeKind::ALL {
        return Err("runtimes must hold exactly one slot per kind, in order".to_string());
    }
    Ok(ActiveModel {
        owner_pid: req_i64(v, "owner_pid")? as u32,
        owner_exe: req_str(v, "owner_exe")?,
        claimed_at_s: req_i64(v, "claimed_at_s")? as u64,
        heartbeat_at_s: req_i64(v, "heartbeat_at_s")? as u64,
        lease_timeout_s: req_i64(v, "lease_timeout_s")? as u64,
        shared: SharedResources {
            bundle_dir: req_str(v, "bundle_dir")?,
            weights_sha256: req_str(v, "weights_sha256")?,
            tokenizer_sha256: req_str(v, "tokenizer_sha256")?,
            device: req_str(v, "device")?,
            thread_pool_size: req_i64(v, "thread_pool_size")? as u32,
            kv_cache_mb: req_i64(v, "kv_cache_mb")? as u32,
            prefix_cache: req_bool(v, "prefix_cache")?,
        },
        runtimes,
    })
}

// ------------------------------------------------------ state machine --

/// Claim inputs. Shared resources are declared once here and reused by
/// every runtime — nothing re-resolves them behind the host's back.
#[derive(Debug, Clone)]
pub struct ClaimRequest {
    pub claimant_pid: u32,
    pub claimant_exe: String,
    pub bundle_dir: String,
    pub weights_sha256: String,
    pub tokenizer_sha256: String,
    pub device: String,
    pub thread_pool_size: u32,
    pub kv_cache_mb: u32,
    pub prefix_cache: bool,
    pub lease_timeout_s: u64,
}

fn fresh_record(req: &ClaimRequest, now_s: u64) -> ActiveModel {
    ActiveModel {
        owner_pid: req.claimant_pid,
        owner_exe: req.claimant_exe.clone(),
        claimed_at_s: now_s,
        heartbeat_at_s: now_s,
        lease_timeout_s: if req.lease_timeout_s > 0 {
            req.lease_timeout_s
        } else {
            LEASE_TIMEOUT_S
        },
        shared: SharedResources {
            bundle_dir: req.bundle_dir.clone(),
            weights_sha256: req.weights_sha256.clone(),
            tokenizer_sha256: req.tokenizer_sha256.clone(),
            device: req.device.clone(),
            thread_pool_size: req.thread_pool_size,
            kv_cache_mb: req.kv_cache_mb,
            prefix_cache: req.prefix_cache,
        },
        runtimes: RuntimeKind::ALL
            .iter()
            .map(|k| RuntimeSlot {
                kind: *k,
                state: SlotState::Absent,
                pid: None,
                exe: String::new(),
            })
            .collect(),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClaimOutcome {
    Claimed,
    /// Same pid reclaiming: heartbeat refreshed, record otherwise kept.
    Refreshed,
    DeniedActiveOwner { owner_pid: u32, heartbeat_age_s: u64 },
}

/// Claim the Active Model. A record naming another pid is denied even
/// when its heartbeat is stale — staleness alone never transfers
/// ownership (see [`takeover`]).
pub fn claim(
    current: Option<ActiveModel>,
    req: &ClaimRequest,
    now_s: u64,
) -> (Option<ActiveModel>, ClaimOutcome) {
    match current {
        None => (Some(fresh_record(req, now_s)), ClaimOutcome::Claimed),
        Some(mut rec) if rec.owner_pid == req.claimant_pid => {
            rec.heartbeat_at_s = now_s;
            (Some(rec), ClaimOutcome::Refreshed)
        }
        Some(rec) => (
            None,
            ClaimOutcome::DeniedActiveOwner {
                owner_pid: rec.owner_pid,
                heartbeat_age_s: rec.heartbeat_age_s(now_s),
            },
        ),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TakeoverOutcome {
    TookOver(ActiveModel),
    /// The caller could not prove death: no transfer, ever.
    DeniedOwnerPossiblyLive { owner_pid: u32 },
    /// Heartbeat still fresh: nothing to take over.
    DeniedFreshHeartbeat { owner_pid: u32, heartbeat_age_s: u64 },
}

/// Seize a stale record. Both preconditions are required: the caller
/// attests (via its own platform API) that the owner pid is dead, AND
/// the heartbeat is past the lease timeout. Either missing → denied.
pub fn takeover(
    current: &ActiveModel,
    req: &ClaimRequest,
    owner_dead_attested: bool,
    now_s: u64,
) -> TakeoverOutcome {
    if !owner_dead_attested {
        return TakeoverOutcome::DeniedOwnerPossiblyLive {
            owner_pid: current.owner_pid,
        };
    }
    if !current.lease_expired(now_s) {
        return TakeoverOutcome::DeniedFreshHeartbeat {
            owner_pid: current.owner_pid,
            heartbeat_age_s: current.heartbeat_age_s(now_s),
        };
    }
    TakeoverOutcome::TookOver(fresh_record(req, now_s))
}

/// Refresh the owner heartbeat. False unless `pid` is the owner.
pub fn heartbeat(rec: &mut ActiveModel, pid: u32, now_s: u64) -> bool {
    if rec.owner_pid != pid {
        return false;
    }
    rec.heartbeat_at_s = now_s;
    true
}

/// Release ownership. False unless `pid` is the owner.
pub fn release(rec: &ActiveModel, pid: u32) -> bool {
    rec.owner_pid == pid
}

/// Update one runtime slot. Owner-only: a non-owner caller cannot move
/// another owner's slots.
pub fn slot_set(
    rec: &mut ActiveModel,
    pid: u32,
    kind: RuntimeKind,
    state: SlotState,
    child_pid: Option<u32>,
    exe: &str,
) -> bool {
    if rec.owner_pid != pid {
        return false;
    }
    match rec.runtimes.iter_mut().find(|s| s.kind == kind) {
        Some(slot) => {
            slot.state = state;
            slot.pid = child_pid;
            slot.exe = exe.to_string();
            true
        }
        None => false,
    }
}

// ---------------------------------------------------------- discovery --

fn descriptor_path(ipc_dir: &Path) -> PathBuf {
    ipc_dir.join(DESCRIPTOR_FILE)
}

/// Load the record. `Ok(None)` only when no file exists; a corrupt file
/// is `Err` — corruption must never read as absence (absence would admit
/// a second owner).
pub fn load(ipc_dir: &Path) -> Result<Option<ActiveModel>, String> {
    let path = descriptor_path(ipc_dir);
    let raw = match fs::read_to_string(&path) {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(format!("read {}: {e}", path.display())),
    };
    let v = parse(&raw).map_err(|e| format!("parse {}: {e}", path.display()))?;
    record_from_json(&v)
        .map(Some)
        .map_err(|e| format!("schema {}: {e}", path.display()))
}

/// Store the record atomically (tmp file + rename in the same directory).
pub fn store(ipc_dir: &Path, rec: &ActiveModel) -> Result<(), String> {
    fs::create_dir_all(ipc_dir)
        .map_err(|e| format!("mkdir {}: {e}", ipc_dir.display()))?;
    let path = descriptor_path(ipc_dir);
    let tmp = path.with_extension(format!("tmp.{}", std::process::id()));
    fs::write(&tmp, render(&record_to_json(rec)) + "\n")
        .map_err(|e| format!("write {}: {e}", tmp.display()))?;
    match fs::rename(&tmp, &path) {
        Ok(()) => Ok(()),
        Err(_) => {
            // Same-dir replace fallback (Windows rename-over-existing).
            let _ = fs::remove_file(&path);
            fs::rename(&tmp, &path)
                .map_err(|e| format!("rename {}: {e}", tmp.display()))
        }
    }
}

/// Remove the record (owner release path). Missing file is success.
pub fn clear(ipc_dir: &Path) -> Result<(), String> {
    let path = descriptor_path(ipc_dir);
    match fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("remove {}: {e}", path.display())),
    }
}

/// Append one audit line. Best-effort by contract: callers must not fail
/// governed work on a ledger error, but the error is returned so the
/// caller can surface it.
pub fn audit(
    logs_dir: &Path,
    at_s: u64,
    event: &str,
    fields: Vec<(String, Json)>,
) -> Result<(), String> {
    fs::create_dir_all(logs_dir)
        .map_err(|e| format!("mkdir {}: {e}", logs_dir.display()))?;
    let mut pairs = vec![
        ("at_s".to_string(), Json::Num(at_s as i64)),
        ("event".to_string(), Json::Str(event.to_string())),
    ];
    pairs.extend(fields);
    let line = render(&Json::Obj(pairs)) + "\n";
    fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs_dir.join(AUDIT_FILE))
        .and_then(|mut f| {
            use std::io::Write as _;
            f.write_all(line.as_bytes())
        })
        .map_err(|e| format!("append audit: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn req(pid: u32) -> ClaimRequest {
        ClaimRequest {
            claimant_pid: pid,
            claimant_exe: "xc-host-test".to_string(),
            bundle_dir: "xingcheng/runtime/devin/gen-consolidate/bundle".to_string(),
            weights_sha256: "aa".to_string(),
            tokenizer_sha256: "bb".to_string(),
            device: "cpu-native".to_string(),
            thread_pool_size: 4,
            kv_cache_mb: 512,
            prefix_cache: true,
            lease_timeout_s: 120,
        }
    }

    #[test]
    fn claim_fresh() {
        let (rec, out) = claim(None, &req(100), 1000);
        let rec = rec.expect("fresh claim succeeds");
        assert_eq!(out, ClaimOutcome::Claimed);
        assert_eq!(rec.owner_pid, 100);
        assert_eq!(rec.runtimes.len(), 6);
        assert!(rec.slot(RuntimeKind::GovernanceHost).is_some());
    }

    #[test]
    fn claim_second_owner_denied_even_when_stale() {
        let (rec, _) = claim(None, &req(100), 1000);
        let rec = rec.unwrap();
        // Heartbeat 10x past lease — still denied without attested takeover.
        let (rec2, out) = claim(Some(rec), &req(200), 1000 + 1200);
        assert!(rec2.is_none());
        assert_eq!(
            out,
            ClaimOutcome::DeniedActiveOwner {
                owner_pid: 100,
                heartbeat_age_s: 1200
            }
        );
    }

    #[test]
    fn claim_self_refreshes() {
        let (rec, _) = claim(None, &req(100), 1000);
        let (rec2, out) = claim(rec, &req(100), 1500);
        assert_eq!(out, ClaimOutcome::Refreshed);
        assert_eq!(rec2.unwrap().heartbeat_at_s, 1500);
    }

    #[test]
    fn takeover_needs_both_preconditions() {
        let (rec, _) = claim(None, &req(100), 1000);
        let rec = rec.unwrap();
        // Fresh heartbeat: denied even with attestation.
        assert_eq!(
            takeover(&rec, &req(200), true, 1050),
            TakeoverOutcome::DeniedFreshHeartbeat {
                owner_pid: 100,
                heartbeat_age_s: 50
            }
        );
        // Stale heartbeat but no attestation: denied.
        assert_eq!(
            takeover(&rec, &req(200), false, 5000),
            TakeoverOutcome::DeniedOwnerPossiblyLive { owner_pid: 100 }
        );
        // Both: transfer with fresh slots.
        match takeover(&rec, &req(200), true, 5000) {
            TakeoverOutcome::TookOver(n) => {
                assert_eq!(n.owner_pid, 200);
                assert!(n.runtimes.iter().all(|s| s.state == SlotState::Absent));
            }
            o => panic!("expected takeover, got {o:?}"),
        }
    }

    #[test]
    fn heartbeat_and_release_are_owner_only() {
        let (rec, _) = claim(None, &req(100), 1000);
        let mut rec = rec.unwrap();
        assert!(!heartbeat(&mut rec, 200, 1100));
        assert!(heartbeat(&mut rec, 100, 1100));
        assert_eq!(rec.heartbeat_at_s, 1100);
        assert!(!release(&rec, 200));
        assert!(release(&rec, 100));
    }

    #[test]
    fn slot_set_is_owner_only() {
        let (rec, _) = claim(None, &req(100), 1000);
        let mut rec = rec.unwrap();
        assert!(!slot_set(
            &mut rec,
            200,
            RuntimeKind::NativeModelRuntime,
            SlotState::Ready,
            Some(300),
            "engine"
        ));
        assert!(slot_set(
            &mut rec,
            100,
            RuntimeKind::NativeModelRuntime,
            SlotState::Ready,
            Some(300),
            "engine"
        ));
        let s = rec.slot(RuntimeKind::NativeModelRuntime).unwrap();
        assert_eq!((s.state, s.pid), (SlotState::Ready, Some(300)));
    }

    #[test]
    fn json_roundtrip_with_escapes() {
        let (rec, _) = claim(None, &req(100), 1000);
        let mut rec = rec.expect("fresh claim");
        rec.shared.bundle_dir = "C:\\Models\\gen \"a\"\nnewline".to_string();
        rec.owner_exe = "host✓".to_string();
        let text = render(&record_to_json(&rec));
        let back = record_from_json(&parse(&text).expect("parse")).expect("schema");
        assert_eq!(back, rec);
    }

    #[test]
    fn corrupt_record_is_error_not_absence() {
        assert!(parse("{oops").is_err());
        let bad = Json::Obj(vec![("format".to_string(), Json::Str("nope".to_string()))]);
        assert!(record_from_json(&bad).is_err());
        let short = Json::Obj(vec![(
            "format".to_string(),
            Json::Str(HOST_FORMAT.to_string()),
        )]);
        assert!(record_from_json(&short).is_err());
    }

    #[test]
    fn store_load_roundtrip_atomic() {
        let dir = std::env::temp_dir().join(format!(
            "xc-host-test-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dir);
        let (rec, _) = claim(None, &req(4242), 2000);
        let rec = rec.unwrap();
        store(&dir, &rec).expect("store");
        let back = load(&dir).expect("load").expect("present");
        assert_eq!(back, rec);
        clear(&dir).expect("clear");
        assert_eq!(load(&dir).expect("load"), None);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn audit_appends_jsonl() {
        let dir = std::env::temp_dir().join(format!(
            "xc-host-audit-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dir);
        audit(&dir, 3000, "claimed", vec![("owner_pid".to_string(), Json::Num(7))])
            .expect("audit");
        let raw = fs::read_to_string(dir.join(AUDIT_FILE)).expect("read");
        let v = parse(raw.trim()).expect("parse");
        assert_eq!(v.get("event"), Some(&Json::Str("claimed".to_string())));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn slot_kinds_cover_all_six() {
        let names: Vec<&str> = RuntimeKind::ALL.iter().map(|k| k.as_str()).collect();
        assert_eq!(
            names,
            vec![
                "native-model-runtime",
                "native-trainer",
                "native-kernel-runtime",
                "native-data-runtime",
                "native-artifact-store",
                "governance-host",
            ]
        );
    }
}

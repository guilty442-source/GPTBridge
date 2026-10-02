//! xstore — governed data/safety lane CLI (xstore/v1).
//!
//!   xstore ckpt-info   <file>              header + tensor table (JSON)
//!   xstore ckpt-verify <file> [--hash-payloads]
//!                                          structural verify + sha256
//!   xstore ckpt-diff   <base> <cand>       delta-candidate parity report
//!   xstore hash        <file>              sha256 of the file
//!   xstore put  --store <dir> --file <f> [--kind xcn1|blob]
//!   xstore get  --store <dir> --sha256 <hex> --out <path>
//!   xstore verify-store --store <dir>      re-hash all + index chain
//!
//! All output is a single JSON object on stdout; errors go to stderr and
//! exit 2 — fail-closed, matching the governed-subprocess contract used
//! by xc_modeltool/xingcheng_trainer.

#![recursion_limit = "512"]

mod audit;
mod diff;
mod failpool;
mod hash;
mod kernels;
mod snapshot;
mod store;
mod xcn1;

use std::collections::HashMap;
use std::path::Path;

use std::process::ExitCode;

fn fail(code: &str, msg: &str) -> ExitCode {
    eprintln!("{code}: {msg}");
    ExitCode::from(2)
}

/// Map a file read-only. The mapping is the only I/O; parsing then runs
/// on a plain slice so every later access is bounds-checked, not I/O.
fn map_file(path: &str) -> Result<memmap2::Mmap, String> {
    let f = std::fs::File::open(path)
        .map_err(|e| format!("open {path}: {e}"))?;
    // SAFETY: read-only map of a file we do not write; mutation by
    // another process is a documented mmap caveat — the parser only
    // reads through the slice and a torn read yields a ParseError, not
    // UB, because no unsafe pointer arithmetic escapes bounds checks.
    unsafe { memmap2::Mmap::map(&f) }.map_err(|e| format!("mmap {path}: {e}"))
}

fn config_json(c: &xcn1::CkptConfig) -> serde_json::Value {
    serde_json::json!({
        "vocab": c.vocab, "hidden": c.hidden, "inter": c.inter,
        "layers": c.layers, "heads": c.heads, "kv_heads": c.kv_heads,
        "max_pos": c.max_pos, "moe_experts": c.moe_experts,
        "moe_top_k": c.moe_top_k,
        "moe_layer_interval": c.moe_layer_interval,
        "rope_theta": c.rope_theta, "rms_eps": c.rms_eps,
        "moe_aux_w": c.moe_aux_w,
        "moe_expert_inter": c.moe_expert_inter,
        "moe_shared_experts": c.moe_shared_experts,
        "moe_shared_inter": c.moe_shared_inter,
        "full_attention_interval": c.full_attention_interval,
        "attn_output_gate": c.attn_output_gate,
        "qk_norm": c.qk_norm,
        "shared_expert_gate": c.shared_expert_gate,
        "partial_rotary": c.partial_rotary,
        "lin_key_heads": c.lin_key_heads, "lin_key_dim": c.lin_key_dim,
        "lin_value_heads": c.lin_value_heads,
        "lin_value_dim": c.lin_value_dim,
        "lin_conv_kernel": c.lin_conv_kernel,
        "use_vision": c.use_vision,
        "vision_patch_dim": c.vision_patch_dim,
        "vision_max_patches": c.vision_max_patches,
        "global_attn_interval": c.global_attn_interval,
        "sliding_window": c.sliding_window,
        "num_global_kv_heads": c.num_global_kv_heads,
        "k_eq_v_global": c.k_eq_v_global,
        "post_attn_norm": c.post_attn_norm,
        "post_ffw_norm": c.post_ffw_norm,
        "ffn_act": c.ffn_act,
        "local_rope_proportion": c.local_rope_proportion,
        "global_rope_proportion": c.global_rope_proportion,
        "rope_theta_local": c.rope_theta_local,
        "rope_theta_global": c.rope_theta_global,
        "final_logit_softcap": c.final_logit_softcap,
        "moe_router_sigmoid": c.moe_router_sigmoid,
        "kv_lora_rank": c.kv_lora_rank, "q_lora_rank": c.q_lora_rank,
        "qk_nope_head_dim": c.qk_nope_head_dim,
        "qk_rope_head_dim": c.qk_rope_head_dim,
        "moe_auxfree_balance": c.moe_auxfree_balance,
        "moe_lb_bias_rate": c.moe_lb_bias_rate,
        "mtp_num_layers": c.mtp_num_layers,
        "mtp_loss_weight": c.mtp_loss_weight,
        "yarn_factor": c.yarn_factor,
        "yarn_orig_pos": c.yarn_orig_pos,
        "yarn_beta_fast": c.yarn_beta_fast,
        "yarn_beta_slow": c.yarn_beta_slow,
        "yarn_attn_factor": c.yarn_attn_factor,
        "is_gemma4": c.is_gemma4,
        "mtp_depth": c.mtp_depth,
        "mtp_loss_w": c.mtp_loss_w,
    })
}

fn tensor_json(e: &xcn1::TensorEntry, payload_sha: Option<String>) -> serde_json::Value {
    let mut v = serde_json::json!({
        "name": e.name,
        "shape": e.shape,
        "count": e.count,
        "data_offset": e.data_offset,
        "data_len": e.data_len,
    });
    if let Some(h) = payload_sha {
        v["sha256"] = serde_json::json!(h);
    }
    v
}

fn cmd_ckpt_info(path: &str) -> Result<serde_json::Value, String> {
    let map = map_file(path)?;
    let buf: &[u8] = &map;
    let header = xcn1::parse_header(buf)
        .map_err(|e| format!("XCN_PARSE: {e}"))?;
    let mut tensors = Vec::new();
    for e in xcn1::tensors(buf, &header) {
        let e = e.map_err(|e| format!("XCN_PARSE: {e}"))?;
        tensors.push(tensor_json(&e, None));
    }
    Ok(serde_json::json!({
        "format": "xstore-ckpt-info/v1",
        "path": path,
        "version": header.version,
        "config": config_json(&header.config),
        "tensor_count": header.tensor_count,
        "tensors": tensors,
    }))
}

fn cmd_ckpt_verify(path: &str, hash_payloads: bool) -> Result<serde_json::Value, String> {
    let map = map_file(path)?;
    let buf: &[u8] = &map;
    let (header, entries, trailing) =
        xcn1::verify(buf).map_err(|e| format!("XCN_VERIFY: {e}"))?;
    let file_sha = hash::sha256_hex(buf);
    let mut tensors = Vec::with_capacity(entries.len());
    let mut total_params: u64 = 0;
    let mut overflow = false;
    for e in &entries {
        total_params = total_params
            .checked_add(e.count)
            .unwrap_or_else(|| {
                overflow = true;
                u64::MAX
            });
        let ph = if hash_payloads {
            let lo = e.data_offset;
            let hi = lo + e.data_len as usize; // bounds proven by verify
            Some(hash::sha256_hex(&buf[lo..hi]))
        } else {
            None
        };
        tensors.push(tensor_json(e, ph));
    }
    Ok(serde_json::json!({
        "format": "xstore-ckpt-verify/v1",
        "path": path,
        "ok": true,
        "version": header.version,
        "sha256": file_sha,
        "tensor_count": header.tensor_count,
        "param_count": total_params,
        "param_count_overflow": overflow,
        "trailing_bytes": trailing,
        "tensors": tensors,
    }))
}

fn cmd_hash(path: &str) -> Result<serde_json::Value, String> {
    let map = map_file(path)?;
    Ok(serde_json::json!({
        "format": "xstore-hash/v1",
        "path": path,
        "algo": "sha256",
        "sha256": hash::sha256_hex(&map),
    }))
}

fn kv_args(args: &[String]) -> HashMap<String, String> {
    let mut m = HashMap::new();
    let mut i = 0;
    while i < args.len() {
        if let Some(k) = args[i].strip_prefix("--") {
            if i + 1 < args.len() && !args[i + 1].starts_with("--") {
                m.insert(k.to_string(), args[i + 1].clone());
                i += 2;
                continue;
            }
            m.insert(k.to_string(), "1".into());
        }
        i += 1;
    }
    m
}

fn cmd_ckpt_diff(base: &str, cand: &str) -> Result<serde_json::Value, String> {
    let bm = map_file(base)?;
    let cm = map_file(cand)?;
    diff::ckpt_diff(&bm, &cm)
}

fn cmd_put(m: &HashMap<String, String>) -> Result<serde_json::Value, String> {
    let store = m.get("store").ok_or("STORE_ARG_MISSING: --store")?;
    let file = m.get("file").ok_or("STORE_ARG_MISSING: --file")?;
    let kind = m.get("kind").map(|s| s.as_str()).unwrap_or("blob");
    let rec = store::put(
        Path::new(store), Path::new(file), kind)?;
    Ok(serde_json::json!({
        "format": "xstore-put/v1",
        "ok": true,
        "sha256": rec.sha256,
        "kind": rec.kind,
        "size": rec.size,
        "object": rec.object.to_string_lossy(),
        "prev": rec.prev,
    }))
}

fn cmd_get(m: &HashMap<String, String>) -> Result<serde_json::Value, String> {
    let store = m.get("store").ok_or("STORE_ARG_MISSING: --store")?;
    let sha = m.get("sha256").ok_or("STORE_ARG_MISSING: --sha256")?;
    let out = m.get("out").ok_or("STORE_ARG_MISSING: --out")?;
    let n = store::get(Path::new(store), sha, Path::new(out))?;
    Ok(serde_json::json!({
        "format": "xstore-get/v1",
        "ok": true,
        "sha256": sha,
        "bytes": n,
        "out": out,
    }))
}

fn cmd_verify_store(m: &HashMap<String, String>) -> Result<serde_json::Value, String> {
    let store = m.get("store").ok_or("STORE_ARG_MISSING: --store")?;
    store::verify_store(Path::new(store))
}

fn cmd_audit_append(m: &HashMap<String, String>) -> Result<serde_json::Value, String> {
    let log = m.get("log").ok_or("AUDIT_ARG_MISSING: --log")?;
    let data = m.get("data").ok_or("AUDIT_ARG_MISSING: --data")?;
    let v: serde_json::Value = serde_json::from_str(data)
        .map_err(|e| format!("AUDIT_DATA_INVALID: {e}"))?;
    audit::append(Path::new(log), v)
}

fn cmd_audit_verify(m: &HashMap<String, String>) -> Result<serde_json::Value, String> {
    let log = m.get("log").ok_or("AUDIT_ARG_MISSING: --log")?;
    audit::verify(Path::new(log))
}

fn cmd_snapshot(m: &HashMap<String, String>) -> Result<serde_json::Value, String> {
    let store_dir = m.get("store").ok_or("SNAPSHOT_ARG_MISSING: --store")?;
    let src = m.get("src").ok_or("SNAPSHOT_ARG_MISSING: --src")?;
    let name = m.get("name").map(|s| s.as_str()).unwrap_or("snapshot");
    snapshot::snapshot(Path::new(store_dir), Path::new(src), name)
}

fn cmd_snapshot_verify(m: &HashMap<String, String>) -> Result<serde_json::Value, String> {
    let store_dir = m.get("store").ok_or("SNAPSHOT_ARG_MISSING: --store")?;
    let mf = m.get("manifest").ok_or("SNAPSHOT_ARG_MISSING: --manifest")?;
    snapshot::snapshot_verify(Path::new(store_dir), mf)
}

fn cmd_fail_record(m: &HashMap<String, String>) -> Result<serde_json::Value, String> {
    let pool = m.get("pool-dir").ok_or("POOL_ARG_MISSING: --pool-dir")?;
    let need = |k: &str| -> Result<String, String> {
        m.get(k)
            .cloned()
            .ok_or_else(|| format!("POOL_ARG_MISSING: --{k}"))
    };
    failpool::record(
        Path::new(pool),
        failpool::RecordArgs {
            failure_class: need("class")?,
            input: need("input")?,
            generation: need("generation")?,
            expected: need("expected")?,
            actual: need("actual")?,
            evidence: need("evidence")?,
            severity: m.get("severity")
                .cloned()
                .unwrap_or_else(|| "medium".into()),
            reproducible: m
                .get("reproducible")
                .map(|s| s != "0" && s != "false")
                .unwrap_or(true),
            reason: m.get("reason").cloned().unwrap_or_default(),
            model_version: m.get("model-version").cloned(),
            runtime_version: m.get("runtime-version").cloned(),
            provenance: m.get("provenance").cloned().unwrap_or_default(),
        },
    )
}

fn cmd_fail_list(m: &HashMap<String, String>) -> Result<serde_json::Value, String> {
    let pool = m.get("pool-dir").ok_or("POOL_ARG_MISSING: --pool-dir")?;
    let cls = m.get("class").ok_or("POOL_ARG_MISSING: --class")?;
    failpool::list(Path::new(pool), cls)
}

fn cmd_fail_mark(m: &HashMap<String, String>) -> Result<serde_json::Value, String> {
    let pool = m.get("pool-dir").ok_or("POOL_ARG_MISSING: --pool-dir")?;
    let cls = m.get("class").ok_or("POOL_ARG_MISSING: --class")?;
    let fp = m.get("fingerprint").ok_or("POOL_ARG_MISSING: --fingerprint")?;
    let st = m.get("state").ok_or("POOL_ARG_MISSING: --state")?;
    failpool::mark(Path::new(pool), cls, fp, st)
}

fn cmd_fail_status(m: &HashMap<String, String>) -> Result<serde_json::Value, String> {
    let pool = m.get("pool-dir").ok_or("POOL_ARG_MISSING: --pool-dir")?;
    failpool::status(Path::new(pool))
}

fn usage() -> ExitCode {
    eprintln!(
        "usage: xstore <ckpt-info|ckpt-verify|hash> <file> [--hash-payloads]\n\
         \x20      xstore ckpt-diff <base> <cand>\n\
         \x20      xstore put --store <dir> --file <f> [--kind xcn1|blob]\n\
         \x20      xstore get --store <dir> --sha256 <hex> --out <path>\n\
         \x20      xstore verify-store --store <dir>\n\
         \x20      xstore audit-append --log <f> --data <json>\n\
         \x20      xstore audit-verify --log <f>\n\
         \x20      xstore snapshot --store <dir> --src <dir> [--name <id>]\n\
         \x20      xstore snapshot-verify --store <dir> --manifest <sha|path>\n\
         \x20      xstore fail-record --pool-dir <d> --class <c> --input <s>\n\
         \x20      \x20 --generation <g> --expected <s> --actual <s>\n\
         \x20      \x20 --evidence <s> [--severity <l>] [--reason <s>]\n\
         \x20      xstore fail-list  --pool-dir <d> --class <c>\n\
         \x20      xstore fail-mark  --pool-dir <d> --class <c>\n\
         \x20      \x20 --fingerprint <fp> --state <OPEN|TRAINED|RESOLVED|REGRESSED>\n\
         \x20      xstore fail-status --pool-dir <d>\n\
         \x20      xstore kernel-registry [--policy <json>]"
    );
    ExitCode::from(2)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        return usage();
    }
    let cmd = args[0].as_str();
    let hash_payloads = args.iter().any(|a| a == "--hash-payloads");
    let kv = kv_args(&args[1..]);
    // star-kernel-policy/v1: --policy <path> arg or XCT_KERNEL_POLICY
    // env; a referenced but unreadable/denied policy fails closed.
    let pol = match kernels::policy_load(
        &kernels::policy_path(kv.get("policy")),
    ) {
        Ok(p) => p,
        Err(e) => return fail("XSTORE_FAILED", &e),
    };
    if let Err(e) = kernels::policy_gate(&pol, cmd) {
        return fail("XSTORE_FAILED", &e);
    }
    let out = match cmd {
        "ckpt-info" if args.len() >= 2 => cmd_ckpt_info(&args[1]),
        "ckpt-verify" if args.len() >= 2 => {
            cmd_ckpt_verify(&args[1], hash_payloads)
        }
        "hash" if args.len() >= 2 => cmd_hash(&args[1]),
        "ckpt-diff" if args.len() >= 3 => cmd_ckpt_diff(&args[1], &args[2]),
        "put" => cmd_put(&kv_args(&args[1..])),
        "get" => cmd_get(&kv_args(&args[1..])),
        "verify-store" => cmd_verify_store(&kv_args(&args[1..])),
        "audit-append" => cmd_audit_append(&kv_args(&args[1..])),
        "audit-verify" => cmd_audit_verify(&kv_args(&args[1..])),
        "snapshot" => cmd_snapshot(&kv_args(&args[1..])),
        "snapshot-verify" => cmd_snapshot_verify(&kv_args(&args[1..])),
        "fail-record" => cmd_fail_record(&kv),
        "fail-list" => cmd_fail_list(&kv),
        "fail-mark" => cmd_fail_mark(&kv),
        "fail-status" => cmd_fail_status(&kv),
        "kernel-registry" => Ok(kernels::registry_emit(
            kv.get("policy"))),
        _ => return usage(),
    };
    match out {
        Ok(v) => {
            println!("{}", serde_json::to_string(&v).unwrap());
            ExitCode::SUCCESS
        }
        Err(e) => fail("XSTORE_FAILED", &e),
    }
}

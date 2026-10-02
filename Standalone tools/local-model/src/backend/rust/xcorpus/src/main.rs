//! xcorpus — governed corpus pipeline CLI (star-pretrain-corpus/v1 port).
//!
//!   xcorpus corpus --registry <json> --root <dir> --tokenizer <p|dir>
//!                  --out <dir> [--max-len N] [--val-ratio PCT]
//!                  [--max-docs N] [--max-doc-chars N] [--max-tokens N]
//!                  [--jobs N]
//!
//! One JSON object on stdout; errors to stderr with exit 2 — the same
//! governed-subprocess contract as xc_modeltool.

use xcorpus::corpus;
use xcorpus::kernels;

use std::process::ExitCode;

fn parse_args(args: &[String]) -> std::collections::HashMap<String, String> {
    let mut m = std::collections::HashMap::new();
    let mut i = 0;
    while i < args.len() {
        if let Some(k) = args[i].strip_prefix("--") {
            if i + 1 < args.len() && !args[i + 1].starts_with("--") {
                m.insert(k.to_string(), args[i + 1].clone());
                i += 2;
                continue;
            }
            m.insert(k.to_string(), String::new());
        }
        i += 1;
    }
    m
}

fn num(m: &std::collections::HashMap<String, String>, k: &str, d: i64) -> i64 {
    m.get(k).and_then(|s| s.parse::<i64>().ok()).unwrap_or(d)
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.is_empty() {
        eprintln!("usage: xcorpus corpus --registry <json> --root <dir> --tokenizer <p> --out <dir> [opts]");
        return ExitCode::from(2);
    }
    let cmd = argv[0].as_str();
    let a = parse_args(&argv[1..]);
    // star-kernel-policy/v1: --policy <path> or XCT_KERNEL_POLICY;
    // unreadable/denied policy fails closed.
    let pol = match kernels::policy_load(
        &kernels::policy_path(a.get("policy")),
    ) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::from(2);
        }
    };
    if let Err(e) = kernels::policy_gate(&pol, cmd) {
        eprintln!("{e}");
        return ExitCode::from(2);
    }
    match cmd {
        "corpus" => {
            let args = corpus::Args {
                registry: a.get("registry").cloned().unwrap_or_default(),
                root: a.get("root").cloned().unwrap_or_default(),
                tokenizer: a.get("tokenizer").cloned().unwrap_or_default(),
                out: a.get("out").cloned().unwrap_or_default(),
                max_len: num(&a, "max-len", 1024),
                val_pct: num(&a, "val-ratio", 5),
                max_docs: num(&a, "max-docs", 20000),
                max_doc_chars: num(&a, "max-doc-chars", 1_000_000),
                max_tokens: num(&a, "max-tokens", 50_000_000),
                jobs: num(&a, "jobs", 0),
            };
            match corpus::run(&args) {
                Ok(line) => {
                    println!("{line}");
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("{e}");
                    ExitCode::from(2)
                }
            }
        }
        "kernel-registry" => {
            println!(
                "{}",
                serde_json::to_string(&kernels::registry_emit(
                    a.get("policy")))
                .unwrap()
            );
            ExitCode::SUCCESS
        }
        _ => {
            eprintln!("unknown mode: {}", argv[0]);
            ExitCode::from(2)
        }
    }
}

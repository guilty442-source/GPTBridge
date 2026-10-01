//! xcorpus — governed corpus pipeline CLI (star-pretrain-corpus/v1 port).
//!
//!   xcorpus corpus --registry <json> --root <dir> --tokenizer <p|dir>
//!                  --out <dir> [--max-len N] [--val-ratio PCT]
//!                  [--max-docs N] [--max-doc-chars N] [--max-tokens N]
//!                  [--jobs N]
//!
//! One JSON object on stdout; errors to stderr with exit 2 — the same
//! governed-subprocess contract as xc_modeltool.

mod corpus;
mod scan;
mod textutil;
mod tokenizer;

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
    match argv[0].as_str() {
        "corpus" => {
            let a = parse_args(&argv[1..]);
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
        _ => {
            eprintln!("unknown mode: {}", argv[0]);
            ExitCode::from(2)
        }
    }
}

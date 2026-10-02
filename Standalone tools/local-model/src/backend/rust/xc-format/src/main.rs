//! xc-format CLI: structural validation for Xingcheng binary artifacts.
//!
//!   xc-format check --ckpt <file>
//!       stdout: {"ok":bool,"format":"star-xcn-header/v1",...}
//!       exit 0 = valid, 2 = invalid header, 1 = tool/IO error
//!
//! Phase 1 covers XCN checkpoint headers only. The C# governed lane
//! (XcnHeader) remains the runtime reader until this lane is wired end
//! to end; this binary is the format authority used by validation and
//! certification paths.

use std::env;
use std::fs;
use std::process::ExitCode;
use xc_format::{parse_header, HeaderError};

fn esc(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

fn err_json(code: &str, msg: &str) -> String {
    format!(
        "{{\"ok\":false,\"format\":\"star-xcn-header/v1\",\"error_code\":\"{}\",\"error\":\"{}\"}}",
        esc(code),
        esc(msg)
    )
}

fn main() -> ExitCode {
    let argv: Vec<String> = env::args().collect();
    let path = match argv.as_slice() {
        [_, cmd, flag, p] if cmd == "check" && flag == "--ckpt" => p.clone(),
        _ => {
            eprintln!("usage: xc-format check --ckpt <file>");
            return ExitCode::from(1);
        }
    };
    let bytes = match fs::read(&path) {
        Ok(b) => b,
        Err(e) => {
            println!("{}", err_json("XC_FORMAT_UNREADABLE", &e.to_string()));
            return ExitCode::from(1);
        }
    };
    match parse_header(&bytes) {
        Ok(s) => {
            println!(
                "{{\"ok\":true,\"format\":\"star-xcn-header/v1\",\"version\":{},\"header_len\":{},\"vocab_size\":{},\"hidden_size\":{},\"num_hidden_layers\":{},\"moe_num_experts\":{},\"has_gemma4\":{},\"mtp_stack_depth\":{}}}",
                s.version,
                s.header_len,
                s.vocab_size,
                s.hidden_size,
                s.num_hidden_layers,
                s.moe_num_experts,
                s.has_gemma4,
                match s.mtp_stack_depth {
                    Some(d) => d.to_string(),
                    None => "null".to_string(),
                }
            );
            ExitCode::from(0)
        }
        Err(HeaderError::BadMagic) => {
            println!("{}", err_json("XC_FORMAT_BAD_MAGIC", "not an XCN1 checkpoint"));
            ExitCode::from(2)
        }
        Err(HeaderError::BadVersion(v)) => {
            println!(
                "{}",
                err_json("XC_FORMAT_VERSION", &format!("unsupported version {v}"))
            );
            ExitCode::from(2)
        }
        Err(e) => {
            println!("{}", err_json("XC_FORMAT_INVALID", &format!("{e:?}")));
            ExitCode::from(2)
        }
    }
}

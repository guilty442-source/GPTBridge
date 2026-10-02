//! Canonical XNC conformance vectors (`contracts/xnc/vectors/`,
//! `xnc-vectors/v1`). Every lane must parse ok vectors byte-identically
//! and reject malformed ones. This crate currently validates the XCN
//! header surface; XCB vectors bind the data-plane crates (xcorpus
//! `xcb`, xstore) on the lane where they live.

use crate::{parse_header, HeaderError};
use std::path::PathBuf;

fn vectors_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../../contracts/xnc/vectors")
}

fn load(name: &str) -> Vec<u8> {
    let p = vectors_dir().join(name);
    std::fs::read(&p).unwrap_or_else(|e| panic!("vector {name} unreadable: {e}"))
}

#[test]
fn vector_dir_present() {
    assert!(vectors_dir().join("manifest.json").is_file());
}

#[test]
fn xcn1_v1_minimal_parses() {
    let s = parse_header(&load("xcn1-v1-minimal.bin")).expect("v1 vector");
    assert_eq!(s.version, 1);
    assert_eq!(s.header_len, 60);
    assert_eq!(s.vocab_size, 32000);
    assert_eq!(s.hidden_size, 64);
    assert_eq!(s.num_hidden_layers, 2);
    assert_eq!(s.moe_num_experts, 0);
    assert!(!s.has_gemma4);
    assert_eq!(s.mtp_stack_depth, None);
}

#[test]
fn xcn1_v10_minimal_parses() {
    let s = parse_header(&load("xcn1-v10-minimal.bin")).expect("v10 vector");
    assert_eq!(s.version, 10);
    assert_eq!(s.mtp_stack_depth, Some(0));
    assert!(!s.has_gemma4);
}

#[test]
fn xcn1_v9_gemma4_parses() {
    let s = parse_header(&load("xcn1-v9-gemma4.bin")).expect("g4 vector");
    assert_eq!(s.version, 9);
    assert!(s.has_gemma4);
}

#[test]
fn xcn_rejects() {
    assert_eq!(
        parse_header(&load("xcn1-bad-magic.bin")),
        Err(HeaderError::BadMagic)
    );
    assert_eq!(
        parse_header(&load("xcn1-bad-version.bin")),
        Err(HeaderError::BadVersion(99))
    );
    assert_eq!(
        parse_header(&load("xcn1-bad-marker.bin")),
        Err(HeaderError::BadGemma4Marker(7))
    );
    assert!(matches!(
        parse_header(&load("xcn1-truncated.bin")),
        Err(HeaderError::Truncated(_))
    ));
}

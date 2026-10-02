//! hash.rs — streaming SHA-256 over byte slices / mapped files.
//! One canonical hashing surface for the data lane so corpus manifests,
//! checkpoint integrity and audit chains share the same digest.

use sha2::{Digest, Sha256};

/// Lowercase-hex SHA-256 of a contiguous buffer.
pub fn sha256_hex(buf: &[u8]) -> String {
    let h = Sha256::digest(buf);
    to_hex(&h)
}

fn to_hex(b: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(b.len() * 2);
    for &x in b {
        s.push(HEX[(x >> 4) as usize] as char);
        s.push(HEX[(x & 15) as usize] as char);
    }
    s
}

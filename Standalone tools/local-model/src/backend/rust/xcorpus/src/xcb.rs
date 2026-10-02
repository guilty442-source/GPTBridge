//! xcb.rs — XCB1 binary token-batch writer, byte-parity port of
//! `cpp/src/xcb_batch.h` (Xingcheng binary hot-data rule: token
//! batches are never JSON).
//!
//!   "XCB1" | u32 version=1 | u32 meta_len | meta JSON | u64 count
//!   record x count:
//!     u8 kind | u8 flags | u16 rsv
//!     u32 n_ids + i32[n] | u32 n_labels + i32[n]
//!     kind 3 (dpo): u32 n_rej + i32[n] | u32 n_rej_labels + i32[n]
//!     flags&1: u32 patches | u32 dim | f32[n]
//!
//! Streaming: header emitted on open, record_count back-patched via a
//! seek on close — identical layout to the C++ Writer.

use std::io::{Seek, SeekFrom, Write};

pub const MAGIC: &[u8; 4] = b"XCB1";
pub const VERSION: u32 = 1;

pub const KIND_PRETRAIN: u8 = 1;
pub const KIND_SFT: u8 = 2;
pub const KIND_DPO: u8 = 3;
pub const KIND_GRPO: u8 = 4;

pub struct Writer<W: Write + Seek> {
    out: W,
    count_pos: u64,
    count: u64,
    closed: bool,
}

impl<W: Write + Seek> Writer<W> {
    pub fn new(mut out: W, meta_json: &str) -> std::io::Result<Self> {
        out.write_all(MAGIC)?;
        out.write_all(&VERSION.to_le_bytes())?;
        out.write_all(&(meta_json.len() as u32).to_le_bytes())?;
        out.write_all(meta_json.as_bytes())?;
        let count_pos = out.stream_position()?;
        out.write_all(&0u64.to_le_bytes())?; // patched in finish()
        Ok(Writer {
            out,
            count_pos,
            count: 0,
            closed: false,
        })
    }

    /// ids-only record (pretrain lane). Other kinds can be added the
    /// same way when a producer needs them — the corpus emits only
    /// packed pretrain rows.
    pub fn add_ids(&mut self, ids: &[i32]) -> std::io::Result<()> {
        if self.closed {
            return Err(std::io::Error::new(
                std::io::ErrorKind::Other,
                "XCB_WRITE_AFTER_CLOSE",
            ));
        }
        self.out.write_all(&[KIND_PRETRAIN, 0, 0, 0])?;
        self.out.write_all(&(ids.len() as u32).to_le_bytes())?;
        for &t in ids {
            self.out.write_all(&t.to_le_bytes())?;
        }
        self.out.write_all(&0u32.to_le_bytes())?; // n_labels = 0
        self.count += 1;
        Ok(())
    }

    pub fn finish(&mut self) -> std::io::Result<u64> {
        if !self.closed {
            self.closed = true;
            let end = self.out.stream_position()?;
            self.out.seek(SeekFrom::Start(self.count_pos))?;
            self.out.write_all(&self.count.to_le_bytes())?;
            self.out.seek(SeekFrom::Start(end))?;
        }
        Ok(self.count)
    }
}

// ------------------------------------------------------------------ b64 --
// Binary-hot-data rule: the incremental corpus cache keeps per-file
// token ids as base64 little-endian i32 (binary payload in a JSONL
// metadata envelope), never a JSON number array.

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// ids must fit i32 (vocab < 2^31); returns Err on overflow so the
/// caller drops the ids field (cache miss → reparse, fail-safe).
pub fn b64_enc_i32(ids: &[i64]) -> Result<String, &'static str> {
    let mut bytes = Vec::with_capacity(ids.len() * 4);
    for &t in ids {
        if t < i32::MIN as i64 || t > i32::MAX as i64 {
            return Err("XCB_IDS_RANGE");
        }
        bytes.extend_from_slice(&(t as i32).to_le_bytes());
    }
    let mut s = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for c in bytes.chunks(3) {
        let b = [c[0], *c.get(1).unwrap_or(&0), *c.get(2).unwrap_or(&0)];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        s.push(B64[(n >> 18) as usize & 63] as char);
        s.push(B64[(n >> 12) as usize & 63] as char);
        s.push(if c.len() > 1 { B64[(n >> 6) as usize & 63] as char } else { '=' });
        s.push(if c.len() > 2 { B64[n as usize & 63] as char } else { '=' });
    }
    Ok(s)
}

/// Strict decoder: standard alphabet + '=' padding only, output byte
/// count must be a multiple of 4. None → caller treats as cache miss.
pub fn b64_dec_i32(s: &str) -> Option<Vec<i64>> {
    fn val(c: u8) -> Option<u32> {
        match c {
            b'A'..=b'Z' => Some((c - b'A') as u32),
            b'a'..=b'z' => Some((c - b'a' + 26) as u32),
            b'0'..=b'9' => Some((c - b'0' + 52) as u32),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    }
    let raw = s.as_bytes();
    if raw.is_empty() {
        return Some(Vec::new());
    }
    if raw.len() % 4 != 0 {
        return None;
    }
    let mut bytes = Vec::with_capacity(raw.len() / 4 * 3);
    for (i, c) in raw.chunks(4).enumerate() {
        let last = i == raw.len() / 4 - 1;
        let pad = if !last { 0 } else { c.iter().rev().take_while(|&&x| x == b'=').count() };
        if pad > 2 || (!last && c.contains(&b'=')) {
            return None;
        }
        let mut n = 0u32;
        for (j, &x) in c.iter().enumerate() {
            if x == b'=' {
                if j < 4 - pad {
                    return None;
                }
            } else {
                if pad > 0 && j >= 4 - pad {
                    return None; // data char inside padding
                }
                n |= val(x)? << (18 - 6 * j);
            }
        }
        bytes.push((n >> 16) as u8);
        if pad < 2 {
            bytes.push((n >> 8) as u8);
        }
        if pad < 1 {
            bytes.push(n as u8);
        }
    }
    if bytes.len() % 4 != 0 {
        return None;
    }
    Some(
        bytes
            .chunks(4)
            .map(|c| i32::from_le_bytes([c[0], c[1], c[2], c[3]]) as i64)
            .collect(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    /// Golden byte layout, identical to the C++ `xcb::Writer` stream for
    /// meta `{"format":"parity/v1"}` + pretrain ids [5,-2,7,300].
    /// Guards the cross-language byte-parity contract.
    #[test]
    fn pretrain_record_layout_matches_cpp_writer() {
        let mut w =
            Writer::new(Cursor::new(Vec::new()), "{\"format\":\"parity/v1\"}").unwrap();
        w.add_ids(&[5, -2, 7, 300]).unwrap();
        let n = w.finish().unwrap();
        assert_eq!(n, 1);
        let bytes = w.out.into_inner();
        let mut exp: Vec<u8> = b"XCB1".to_vec();
        exp.extend_from_slice(&1u32.to_le_bytes());
        exp.extend_from_slice(&22u32.to_le_bytes());
        exp.extend_from_slice(b"{\"format\":\"parity/v1\"}");
        exp.extend_from_slice(&1u64.to_le_bytes());
        exp.extend_from_slice(&[KIND_PRETRAIN, 0, 0, 0]);
        exp.extend_from_slice(&4u32.to_le_bytes());
        for t in [5i32, -2, 7, 300] {
            exp.extend_from_slice(&t.to_le_bytes());
        }
        exp.extend_from_slice(&0u32.to_le_bytes()); // n_labels = 0
        assert_eq!(bytes, exp);
        // Optional cross-language diff: XCB_PARITY_OUT writes the stream
        // for `fc /b` comparison against a C++-emitted file.
        if let Ok(path) = std::env::var("XCB_PARITY_OUT") {
            std::fs::write(path, &bytes).unwrap();
        }
    }
}

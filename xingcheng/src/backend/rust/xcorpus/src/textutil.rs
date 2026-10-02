//! textutil.rs — text normalization + hashing surface, a byte-faithful
//! port of the helpers `xcm_corpus.h` consumes (`nfc`, `py_strip`,
//! `sha256_text`, `norm_text_sha`, `json_escape`).

use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;

/// SHA-256 hex over raw bytes.
pub fn sha256_bytes(b: &[u8]) -> String {
    let h = Sha256::digest(b);
    let mut s = String::with_capacity(64);
    for x in h {
        s.push_str(&format!("{x:02x}"));
    }
    s
}

pub fn sha256_text(s: &str) -> String {
    sha256_bytes(s.as_bytes())
}

pub fn sha256_file(path: &std::path::Path) -> Result<String, String> {
    let b = std::fs::read(path).map_err(|e| format!("read {path:?}: {e}"))?;
    Ok(sha256_bytes(&b))
}

/// NFC. The C++ lane delegates to Windows NormalizeString on a
/// lossy-decoded UTF-16 view; `from_utf8_lossy` + `nfc()` is the same
/// normalization on the same replacement semantics (invalid bytes
/// become U+FFFD before composition).
pub fn nfc(s: &str) -> String {
    s.nfc().collect()
}

/// The exact whitespace set `unicode_space` uses in xc_modeltool.cpp.
fn unicode_space(cp: u32) -> bool {
    matches!(cp,
        0x09 | 0x0A | 0x0B | 0x0C | 0x0D | 0x20 | 0x85 | 0xA0 | 0x1680
        | 0x2000..=0x200A | 0x2028 | 0x2029 | 0x202F | 0x205F | 0x3000
        | 0x1C..=0x1F)
}

/// Decode the codepoint starting at `i` the same way the C++ helper
/// does: raw continuation-byte accumulation without validity checks,
/// so malformed input yields the same (garbage) codepoint instead of a
/// decode error.
fn next_cp(s: &[u8], i: usize) -> (u32, usize) {
    let c = s[i];
    let (mut cp, len) = if c < 0x80 {
        (c as u32, 1)
    } else if (c & 0xE0) == 0xC0 {
        ((c & 0x1F) as u32, 2)
    } else if (c & 0xF0) == 0xE0 {
        ((c & 0x0F) as u32, 3)
    } else {
        ((c & 0x07) as u32, 4)
    };
    for k in 1..len {
        if i + k < s.len() {
            cp = (cp << 6) | ((s[i + k] & 0x3F) as u32);
        }
    }
    (cp, len)
}

/// Python `str.strip()` over the C++ unicode_space table, on raw bytes
/// (the input may be arbitrary, not necessarily valid UTF-8).
pub fn py_strip(s: &[u8]) -> &[u8] {
    let (mut b, mut e) = (0usize, s.len());
    while b < e {
        let (cp, l) = next_cp(s, b);
        if !unicode_space(cp) {
            break;
        }
        b += l;
    }
    while e > b {
        let mut i = e - 1;
        while i > b && (s[i] & 0xC0) == 0x80 {
            i -= 1;
        }
        let (cp, l) = next_cp(s, i);
        if !unicode_space(cp) || i + l != e {
            break;
        }
        e = i;
    }
    &s[b..e]
}

/// sha256(py_strip(nfc(text))) — the overlap-gate document hash.
pub fn norm_text_sha(text: &str) -> String {
    sha256_bytes(py_strip(nfc(text).as_bytes()))
}

/// JSON string escaping matching gptbridge::jsonlite::json_escape:
/// quote/backslash/control escapes, other bytes pass through verbatim.
pub fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0C}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

//! Deterministic hashed embedding — Rust port of the governed
//! `xingcheng-hashed-embedding-v1` scheme (`native_runtime._native_embed`).
//!
//! CJK unigram+bigram and latin word-token features are hashed with
//! BLAKE2b-64; each feature adds ±1.0 to `digest[..4] (LE) % dim`, then the
//! vector is L2-normalised.  Text upsert/search lets callers send `text`
//! instead of a serialised `vector`, removing the Python-side embed →
//! list → JSON → parse copy chain (PERF-07).
//!
//! BLAKE2b is implemented locally (RFC 7693) so no new dependency enters
//! the build; golden tests pin byte-parity against Python `hashlib`.

// ---------- BLAKE2b-64 (RFC 7693, keyless, digest_size = 8) ----------

const IV: [u64; 8] = [
    0x6a09e667f3bcc908,
    0xbb67ae8584caa73b,
    0x3c6ef372fe94f82b,
    0xa54ff53a5f1d36f1,
    0x510e527fade682d1,
    0x9b05688c2b3e6c1f,
    0x1f83d9abfb41bd6b,
    0x5be0cd19137e2179,
];

const SIGMA: [[usize; 16]; 12] = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
    [11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4],
    [7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8],
    [9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13],
    [2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9],
    [12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11],
    [13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10],
    [6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5],
    [10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0],
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
];

#[inline]
fn g(v: &mut [u64; 16], a: usize, b: usize, c: usize, d: usize, x: u64, y: u64) {
    v[a] = v[a].wrapping_add(v[b]).wrapping_add(x);
    v[d] = (v[d] ^ v[a]).rotate_right(32);
    v[c] = v[c].wrapping_add(v[d]);
    v[b] = (v[b] ^ v[c]).rotate_right(24);
    v[a] = v[a].wrapping_add(v[b]).wrapping_add(y);
    v[d] = (v[d] ^ v[a]).rotate_right(16);
    v[c] = v[c].wrapping_add(v[d]);
    v[b] = (v[b] ^ v[c]).rotate_right(63);
}

fn compress(h: &mut [u64; 8], block: &[u8; 128], count: u128, last: bool) {
    let mut m = [0u64; 16];
    for (i, chunk) in block.chunks_exact(8).enumerate() {
        m[i] = u64::from_le_bytes(chunk.try_into().unwrap());
    }
    let mut v = [0u64; 16];
    v[..8].copy_from_slice(h);
    v[8..].copy_from_slice(&IV);
    v[12] ^= count as u64;
    v[13] ^= (count >> 64) as u64;
    if last {
        v[14] ^= u64::MAX; // f[0] last-block flag XORs into IV[6], not assign
    }
    for round in &SIGMA {
        g(&mut v, 0, 4, 8, 12, m[round[0]], m[round[1]]);
        g(&mut v, 1, 5, 9, 13, m[round[2]], m[round[3]]);
        g(&mut v, 2, 6, 10, 14, m[round[4]], m[round[5]]);
        g(&mut v, 3, 7, 11, 15, m[round[6]], m[round[7]]);
        g(&mut v, 0, 5, 10, 15, m[round[8]], m[round[9]]);
        g(&mut v, 1, 6, 11, 12, m[round[10]], m[round[11]]);
        g(&mut v, 2, 7, 8, 13, m[round[12]], m[round[13]]);
        g(&mut v, 3, 4, 9, 14, m[round[14]], m[round[15]]);
    }
    for i in 0..8 {
        h[i] ^= v[i] ^ v[i + 8];
    }
}

/// BLAKE2b with out_len 8 — equivalent to `hashlib.blake2b(d, digest_size=8)`.
fn blake2b_8(data: &[u8]) -> [u8; 8] {
    // param block: digest_length=8, key_length=0, fanout=1, depth=1
    let mut h = IV;
    h[0] ^= 0x0101_0008;

    let mut offset = 0usize;
    let mut count = 0u128;
    while data.len() - offset > 128 {
        let mut block = [0u8; 128];
        block.copy_from_slice(&data[offset..offset + 128]);
        count += 128;
        compress(&mut h, &block, count, false);
        offset += 128;
    }
    let mut last_block = [0u8; 128];
    let tail = &data[offset..];
    last_block[..tail.len()].copy_from_slice(tail);
    count += tail.len() as u128;
    compress(&mut h, &last_block, count, true);

    let mut out = [0u8; 8];
    out.copy_from_slice(&h[0].to_le_bytes());
    out
}

// ---------- feature extraction (mirrors _native_embed) ----------

#[inline]
fn is_cjk(c: char) -> bool {
    // [㐀-鿿豈-﫿]
    matches!(c, '\u{3400}'..='\u{9fff}' | '\u{f900}'..='\u{faff}')
}

#[inline]
fn is_latin_token_char(c: char) -> bool {
    // [a-z0-9_./:-]
    matches!(c, 'a'..='z' | '0'..='9' | '_' | '.' | '/' | ':' | '-')
}

/// Python `str.casefold()` equivalent for the governed corpus: Unicode
/// lowercase plus the small set of common full folds. Chars outside this
/// table fold the same way under `to_lowercase` on every realistic input.
fn fold_char(c: char, out: &mut String) {
    match c {
        'ß' | 'ẞ' => out.push_str("ss"),
        'ﬀ' => out.push_str("ff"),
        'ﬁ' => out.push_str("fi"),
        'ﬂ' => out.push_str("fl"),
        'ﬃ' => out.push_str("ffi"),
        'ﬄ' => out.push_str("ffl"),
        'ﬅ' | 'ﬆ' => out.push_str("st"),
        'ς' => out.push('σ'),
        'ŉ' => out.push_str("\u{02bc}n"),
        _ => out.extend(c.to_lowercase()),
    }
}

fn normalize(text: &str) -> String {
    // Python: str(text).strip().casefold() then \s+ -> " "
    let mut folded = String::with_capacity(text.len());
    for c in text.trim().chars() {
        fold_char(c, &mut folded);
    }
    let mut out = String::with_capacity(folded.len());
    let mut pending_space = false;
    for c in folded.chars() {
        if c.is_whitespace() {
            pending_space = !out.is_empty();
        } else {
            if pending_space {
                out.push(' ');
                pending_space = false;
            }
            out.push(c);
        }
    }
    out
}

/// Deterministic hash embedding — byte-exact port of
/// `xingcheng-hashed-embedding-v1`.
pub(crate) fn embed(text: &str, dimension: usize) -> Vec<f32> {
    let mut vector = vec![0.0f64; dimension];
    let normalized = normalize(text);
    if normalized.is_empty() {
        return vec![0.0f32; dimension];
    }

    let cjk: Vec<char> = normalized.chars().filter(|c| is_cjk(*c)).collect();

    let mut hash_into = |feature: &str, vector: &mut [f64]| {
        let digest = blake2b_8(feature.as_bytes());
        let bucket =
            (u32::from_le_bytes(digest[..4].try_into().unwrap()) as usize) % dimension;
        if digest[4] & 1 == 1 {
            vector[bucket] += 1.0;
        } else {
            vector[bucket] -= 1.0;
        }
    };

    let mut feature = String::with_capacity(8);
    for ch in &cjk {
        feature.clear();
        feature.push_str("c:");
        feature.push(*ch);
        hash_into(&feature, &mut vector);
    }
    for pair in cjk.windows(2) {
        feature.clear();
        feature.push_str("b:");
        feature.push(pair[0]);
        feature.push(pair[1]);
        hash_into(&feature, &mut vector);
    }
    let chars: Vec<char> = normalized.chars().collect();
    let mut i = 0usize;
    while i < chars.len() {
        if is_latin_token_char(chars[i]) {
            let start = i;
            while i < chars.len() && is_latin_token_char(chars[i]) {
                i += 1;
            }
            feature.clear();
            feature.push_str("w:");
            feature.extend(&chars[start..i]);
            hash_into(&feature, &mut vector);
        } else {
            i += 1;
        }
    }

    let norm = vector.iter().map(|v| v * v).sum::<f64>().sqrt();
    if norm <= 0.0 {
        return vec![0.0f32; dimension];
    }
    vector.iter().map(|v| (v / norm) as f32).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex8(bytes: [u8; 8]) -> String {
        bytes.iter().map(|b| format!("{:02x}", b)).collect()
    }

    #[test]
    fn blake2b8_matches_hashlib_goldens() {
        // Generated with hashlib.blake2b(x, digest_size=8).hexdigest()
        assert_eq!(hex8(blake2b_8(b"")), "e4a6a0577479b2b4");
        assert_eq!(hex8(blake2b_8(b"abc")), "d8bb14d833d59559");
        assert_eq!(hex8(blake2b_8("c:中".as_bytes())), "a93713f462f6adfc");
        assert_eq!(hex8(blake2b_8(b"w:gptbridge")), "6809bde6562e52c1");
    }

    #[test]
    fn embed_matches_python_golden() {
        // Golden generated by native_runtime._native_embed(dim=16).
        let cases: [(&str, [f32; 16]); 4] = [
            (
                "本地向量資料庫",
                [
                    -0.2773501, -0.2773501, 0.2773501, 0.0, 0.0, 0.0, 0.0,
                    -0.5547002, 0.5547002, 0.0, 0.0, 0.0, 0.0, -0.2773501,
                    0.2773501, 0.0,
                ],
            ),
            (
                "vector engine",
                [
                    0.0, -0.70710678, 0.0, 0.0, 0.0, 0.0, 0.0, -0.70710678,
                    0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                ],
            ),
            (
                "Mixed 混合 Test-42",
                [
                    -0.4472136, 0.0, 0.4472136, 0.0, 0.0, 0.4472136, 0.0, 0.0,
                    -0.4472136, 0.0, 0.0, 0.0, 0.0, 0.0, -0.4472136, 0.0,
                ],
            ),
            (
                "向量檢索 test",
                [
                    -0.5, 0.0, 0.0, 0.0, 0.5, 0.0, 0.0, -0.5, 0.0, 0.0, 0.0,
                    0.0, 0.0, -0.5, 0.0, 0.0,
                ],
            ),
        ];
        for (text, expected) in cases {
            let got = embed(text, 16);
            for (g, e) in got.iter().zip(expected.iter()) {
                assert!((g - e).abs() < 1e-6, "embed parity failed for {:?}", text);
            }
        }
    }

    #[test]
    fn embed_is_normalized_and_deterministic() {
        let a = embed("向量檢索 test", 1024);
        let b = embed("向量檢索 test", 1024);
        assert_eq!(a, b);
        let norm: f32 = a.iter().map(|v| v * v).sum::<f32>().sqrt();
        assert!((norm - 1.0).abs() < 1e-5);
        assert_eq!(embed("", 1024), vec![0.0f32; 1024]);
    }
}

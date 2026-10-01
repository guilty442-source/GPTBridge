//! scan.rs — deny-list, language classifier, MinHash signature/bands —
//! a 1:1 port of the pure helpers in xcm_corpus.h. Same constants, same
//! order, same arithmetic (bit-identical signatures).

/// C108 confidential deny-fragments, matched against lowercase
/// '/'-separated repo-relative paths (same set and order as the C++).
const DENY: [&str; 19] = [
    "governance_rule/codex",
    "governance_codex",
    "permission_directory",
    "permission-directory",
    "/audit/",
    "git_audit_chain",
    "codex_amendment_audit",
    "codex_read_audit",
    "/runtime/",
    ".git/",
    ".worktrees",
    "node_modules",
    ".backups",
    "/obj/",
    "/bin/",
    "/.vs/",
    "/.vscode/",
    "/.devin/",
    "/.kilo/",
    "/.smallcode/",
    "/.tools/",
];

pub fn corpus_denied(rel: &str) -> bool {
    // '/'-padded haystack so anchored fragments like "/runtime/" still
    // deny a top-level "runtime" dir.
    let mut hay = String::with_capacity(rel.len() + 2);
    hay.push('/');
    hay.push_str(rel);
    if rel.is_empty() || !rel.ends_with('/') {
        hay.push('/');
    }
    DENY.iter().any(|d| hay.contains(d))
}

const TEXT_EXT: [&str; 26] = [
    ".md", ".txt", ".json", ".py", ".cs", ".cpp", ".h", ".hpp", ".c",
    ".cc", ".rs", ".ts", ".tsx", ".js", ".mjs", ".go", ".sql", ".toml",
    ".yaml", ".yml", ".xml", ".html", ".css", ".ps1", ".bat", ".sh",
];

pub fn corpus_text_ext(ext: &str) -> bool {
    TEXT_EXT.contains(&ext)
}

const CODE_EXT: [&str; 17] = [
    ".cs", ".cpp", ".h", ".hpp", ".c", ".cc", ".rs", ".ts", ".tsx",
    ".js", ".mjs", ".py", ".go", ".sql", ".ps1", ".bat", ".sh",
];

/// Deterministic per-document language tag: CJK density → zh-tw, code
/// extension → code, digit density → math, else en/other. Operates on
/// the first 64 KiB like the C++ sampler.
pub fn corpus_lang(text: &[u8], ext: &str) -> &'static str {
    let span = text.len().min(64 * 1024);
    let (mut cjk, mut alpha, mut digit, mut total) = (0i64, 0i64, 0i64, 0i64);
    let mut i = 0usize;
    while i < span {
        let c = text[i];
        let (mut cp, len) = if (c & 0xE0) == 0xC0 {
            ((c & 0x1F) as u32, 2)
        } else if (c & 0xF0) == 0xE0 {
            ((c & 0x0F) as u32, 3)
        } else if (c & 0xF8) == 0xF0 {
            ((c & 0x07) as u32, 4)
        } else {
            (c as u32, 1)
        };
        for k in 1..len {
            if i + k < span {
                cp = (cp << 6) | ((text[i + k] & 0x3F) as u32);
            }
        }
        i += len;
        total += 1;
        if (0x4E00..=0x9FFF).contains(&cp)
            || (0x3400..=0x4DBF).contains(&cp)
            || (0xF900..=0xFAFF).contains(&cp)
        {
            cjk += 1;
        } else if (97..=122).contains(&cp) || (65..=90).contains(&cp) {
            alpha += 1;
        } else if (48..=57).contains(&cp) {
            digit += 1;
        }
    }
    if total > 0 && cjk * 20 > total {
        return "zh-tw";
    }
    if CODE_EXT.contains(&ext) {
        return "code";
    }
    if total > 0 && digit * 4 > alpha {
        return "math";
    }
    if alpha > 0 {
        "en"
    } else {
        "other"
    }
}

pub fn corpus_fnv(s: &[u8]) -> u64 {
    let mut h = 1469598103934665603u64;
    for &c in s {
        h ^= c as u64;
        h = h.wrapping_mul(1099511628211);
    }
    h
}

pub fn corpus_mix(mut x: u64) -> u64 {
    x ^= x >> 30;
    x = x.wrapping_mul(0xBF58476D1CE4E5B9);
    x ^= x >> 27;
    x = x.wrapping_mul(0x94D049BB133111EB);
    x ^ (x >> 31)
}

/// 64-lane MinHash over whitespace word 5-grams (≤4000 grams/doc).
/// Bit-identical to corpus_sig in xcm_corpus.h.
pub fn corpus_sig(text: &[u8]) -> [u64; 64] {
    let mut sig = [!0u64; 64];
    let mut grams: Vec<u64> = Vec::with_capacity(64);
    let mut i = 0usize;
    while i < text.len() {
        let mut g = 1469598103934665603u64;
        let mut words = 0;
        while i < text.len() && words < 5 {
            while i < text.len() && text[i] <= b' ' {
                i += 1;
            }
            if i >= text.len() {
                break;
            }
            let b0 = i;
            while i < text.len() && text[i] > b' ' {
                i += 1;
            }
            for k in b0..i {
                g ^= text[k] as u64;
                g = g.wrapping_mul(1099511628211);
            }
            g ^= 0xFF;
            g = g.wrapping_mul(1099511628211);
            words += 1;
        }
        if words == 5 {
            grams.push(g);
        }
        if grams.len() > 4000 {
            break;
        }
    }
    if grams.is_empty() {
        grams.push(corpus_fnv(text));
    }
    for &g in &grams {
        for s in 0..64u64 {
            let v = corpus_mix(g ^ (0x9E3779B97F4A7C15u64.wrapping_mul(s + 1)));
            if v < sig[s as usize] {
                sig[s as usize] = v;
            }
        }
    }
    sig
}

/// 16 band keys from the 64-lane signature (band width 4).
pub fn band_keys(sig: &[u64; 64]) -> [u64; 16] {
    let mut out = [0u64; 16];
    for b in 0..16usize {
        out[b] = corpus_mix(
            sig[b * 4]
                ^ sig[b * 4 + 1]
                ^ (sig[b * 4 + 2] << 1)
                ^ (sig[b * 4 + 3] >> 1)
                ^ (b as u64),
        );
    }
    out
}

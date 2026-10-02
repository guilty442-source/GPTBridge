//! tokenizer.rs — byte-level BPE encode, a faithful port of
//! `ByteLevelBPETokenizer` (engine_tokenizer.h): GPT-2 printable-byte
//! mapping, merge-by-lowest-rank, fixed special-token set. bos is the
//! literal id 1 and eos the literal id 2, matching the C++ encode().

use std::collections::{HashMap, HashSet};

const SPECIALS: [&str; 9] = [
    "<|pad|>", "<|bos|>", "<|eos|>", "<|unk|>", "<|system|>",
    "<|user|>", "<|assistant|>", "<|tool|>", "<|eot|>",
];

pub struct ByteLevelBpeTokenizer {
    vocab: HashMap<String, i64>,
    id_to_token: Vec<String>,
    merge_rank: HashMap<String, i64>,
    special_tokens: Vec<(String, i64)>, // sorted longest-first
    special_ids: HashSet<i64>,
    byte_to_token: Vec<String>,
    token_to_byte: HashMap<String, u8>,
}

fn push_cp_utf8(out: &mut String, cp: u32) {
    match char::from_u32(cp) {
        Some(c) => out.push(c),
        None => out.push('\u{FFFD}'),
    }
}

impl ByteLevelBpeTokenizer {
    pub fn load(path: &std::path::Path) -> Result<Self, String> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| format!("TOKENIZER_READ: {path:?}: {e}"))?;
        let root: serde_json::Value = serde_json::from_str(&text)
            .map_err(|e| format!("TOKENIZER_JSON: {e}"))?;
        let model = root
            .get("model")
            .ok_or("TOKENIZER_MODEL_MISSING")?;
        let vocab_obj = model
            .get("vocab")
            .and_then(|v| v.as_object())
            .ok_or("TOKENIZER_VOCAB_INVALID")?;
        let mut vocab = HashMap::with_capacity(vocab_obj.len());
        for (tok, idv) in vocab_obj {
            let id = idv
                .as_i64()
                .filter(|&i| i >= 0)
                .ok_or("TOKENIZER_VOCAB_ID_INVALID")?;
            vocab.insert(tok.clone(), id);
        }

        let mut merge_rank = HashMap::new();
        if let Some(merges) = model.get("merges") {
            let arr = merges
                .as_array()
                .ok_or("TOKENIZER_MERGES_INVALID")?;
            for (rank, item) in arr.iter().enumerate() {
                let pair = if let Some(s) = item.as_str() {
                    let split = s.find(' ')
                        .filter(|&i| i > 0 && i + 1 < s.len())
                        .ok_or("TOKENIZER_MERGE_FORMAT_INVALID")?;
                    (s[..split].to_string(), s[split + 1..].to_string())
                } else if let Some(a) = item.as_array() {
                    if a.len() != 2 {
                        return Err("TOKENIZER_MERGE_INVALID".into());
                    }
                    match (a[0].as_str(), a[1].as_str()) {
                        (Some(l), Some(r)) => (l.to_string(), r.to_string()),
                        _ => return Err("TOKENIZER_MERGE_INVALID".into()),
                    }
                } else {
                    return Err("TOKENIZER_MERGE_INVALID".into());
                };
                merge_rank.insert(
                    format!("{}\x1f{}", pair.0, pair.1),
                    rank as i64,
                );
            }
        }

        // GPT-2 bytes-to-unicode: printable bytes map to themselves;
        // the rest get codepoints 256+.
        let mut printable = [false; 256];
        for b in 33..=126u16 {
            printable[b as usize] = true;
        }
        for b in 161..=172u16 {
            printable[b as usize] = true;
        }
        for b in 174..=255u16 {
            printable[b as usize] = true;
        }
        let mut byte_to_token = vec![String::new(); 256];
        let mut extra = 0u32;
        for b in 0..256usize {
            let cp = if printable[b] {
                b as u32
            } else {
                let c = 256 + extra;
                extra += 1;
                c
            };
            push_cp_utf8(&mut byte_to_token[b], cp);
        }

        let mut special_tokens: Vec<(String, i64)> = SPECIALS
            .iter()
            .filter_map(|t| vocab.get(*t).map(|&id| (t.to_string(), id)))
            .collect();
        special_tokens.sort_by(|a, b| b.0.len().cmp(&a.0.len()));
        let special_ids: HashSet<i64> =
            special_tokens.iter().map(|(_, id)| *id).collect();

        let max_id = vocab.values().copied().max().unwrap_or(-1);
        let mut id_to_token = vec![String::new(); (max_id + 1) as usize];
        for (tok, &id) in &vocab {
            id_to_token[id as usize] = tok.clone();
        }
        let token_to_byte: HashMap<String, u8> = byte_to_token
            .iter()
            .enumerate()
            .map(|(b, t)| (t.clone(), b as u8))
            .collect();

        Ok(Self {
            vocab,
            id_to_token,
            merge_rank,
            special_tokens,
            special_ids,
            byte_to_token,
            token_to_byte,
        })
    }

    pub fn vocab_size(&self) -> i64 {
        self.id_to_token.len() as i64
    }

    fn special_at(&self, text: &[u8], pos: usize) -> Option<&(String, i64)> {
        self.special_tokens
            .iter()
            .find(|(t, _)| text[pos..].starts_with(t.as_bytes()))
    }

    /// encode(text, add_bos, add_eos) — the corpus lane never passes a
    /// max_length, so the truncation path is omitted.
    pub fn encode(&self, text: &[u8], add_bos: bool, add_eos: bool) -> Vec<i64> {
        let mut ids = Vec::new();
        if add_bos {
            ids.push(1);
        }
        let mut pos = 0usize;
        while pos < text.len() {
            if let Some((t, id)) = self.special_at(text, pos) {
                ids.push(*id);
                pos += t.len();
                continue;
            }
            let mut end = pos;
            while end < text.len() && self.special_at(text, end).is_none() {
                end += 1;
            }
            ids.extend(self.encode_segment(&text[pos..end]));
            pos = end;
        }
        if add_eos {
            ids.push(2);
        }
        ids
    }

    /// Greedy merge-by-rank, identical order to the C++ loop: scan all
    /// adjacent pairs, merge the single lowest-rank pair, repeat.
    fn encode_segment(&self, text: &[u8]) -> Vec<i64> {
        let mut pieces: Vec<String> =
            text.iter().map(|&b| self.byte_to_token[b as usize].clone()).collect();
        while pieces.len() > 1 {
            let mut best_rank = i64::MAX;
            let mut best = usize::MAX;
            for i in 0..pieces.len() - 1 {
                let key = format!("{}\x1f{}", pieces[i], pieces[i + 1]);
                if let Some(&r) = self.merge_rank.get(&key) {
                    if r < best_rank {
                        best_rank = r;
                        best = i;
                    }
                }
            }
            if best == usize::MAX {
                break;
            }
            let merged = format!("{}{}", pieces[best], pieces[best + 1]);
            pieces[best] = merged;
            pieces.remove(best + 1);
        }
        pieces
            .iter()
            .map(|p| *self.vocab.get(p).unwrap_or(&3)) // unk
            .collect()
    }

    /// encode with the engine's max_length contract: truncate to
    /// max_length and, when add_eos was requested, pin the last id to
    /// eos — identical to the C++ tail.
    pub fn encode_limited(
        &self,
        text: &[u8],
        add_bos: bool,
        add_eos: bool,
        max_length: i64,
    ) -> Vec<i64> {
        let mut ids = self.encode(text, add_bos, add_eos);
        if max_length > 0 && ids.len() as i64 > max_length {
            ids.truncate(max_length as usize);
            if add_eos {
                if let Some(last) = ids.last_mut() {
                    *last = 2;
                }
            }
        }
        ids
    }

    /// decode(ids, skip_special) — byte-exact port of the C++ decode:
    /// id→token, UTF-8 units mapped back through token_to_byte, unknown
    /// ids emit ' ' when !skip_special, then sanitize_utf8.
    pub fn decode(&self, ids: &[i64], skip_special: bool) -> Vec<u8> {
        let mut bytes = Vec::new();
        for &id in ids {
            if id < 0 || id as usize >= self.id_to_token.len() {
                if !skip_special {
                    bytes.push(b' ');
                }
                continue;
            }
            if skip_special && self.special_ids.contains(&id) {
                continue;
            }
            let token = self.id_to_token[id as usize].as_bytes();
            let mut i = 0usize;
            while i < token.len() {
                let lead = token[i];
                let width = if lead & 0xE0 == 0xC0 {
                    2
                } else if lead & 0xF0 == 0xE0 {
                    3
                } else if lead & 0xF8 == 0xF0 {
                    4
                } else {
                    1
                };
                if i + width > token.len() {
                    bytes.push(b' ');
                    break;
                }
                let unit = &token[i..i + width];
                match self.token_to_byte.get(
                    std::str::from_utf8(unit).unwrap_or(""),
                ) {
                    Some(&b) => bytes.push(b),
                    None => bytes.extend_from_slice(unit),
                }
                i += width;
            }
        }
        sanitize_utf8(&bytes)
    }
}

// Byte-exact port of engine_json.h sanitize_utf8: ASCII passes, valid
// multi-byte sequences pass, anything else emits U+FFFD and consumes
// the whole truncated tail (errors='replace' semantics).
fn sanitize_utf8(input: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(input.len());
    let mut i = 0usize;
    while i < input.len() {
        let c = input[i];
        if c < 0x80 {
            out.push(c);
            i += 1;
            continue;
        }
        let width = if c & 0xE0 == 0xC0 {
            2
        } else if c & 0xF0 == 0xE0 {
            3
        } else if c & 0xF8 == 0xF0 {
            4
        } else {
            0
        };
        let mut valid = width > 0 && i + width <= input.len();
        if valid {
            for k in 1..width {
                if input[i + k] & 0xC0 != 0x80 {
                    valid = false;
                    break;
                }
            }
        }
        if valid {
            out.extend_from_slice(&input[i..i + width]);
            i += width;
        } else {
            out.extend_from_slice(&[0xEF, 0xBF, 0xBD]);
            if width > 0 {
                i += 1;
                while i < input.len() && input[i] & 0xC0 == 0x80 {
                    i += 1;
                }
            } else {
                i += 1;
            }
        }
    }
    out
}

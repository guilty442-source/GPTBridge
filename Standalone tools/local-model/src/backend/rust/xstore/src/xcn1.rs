//! xcn1.rs — bounds-checked parser for the XCN1..XCN10 checkpoint format
//! written by `xct_ckpt.h` (ckpt_save / ckpt_read_config).
//!
//! This is the untrusted-binary boundary: every length field is
//! attacker-controlled, so all reads are bounds-checked and every
//! multiplicative extent is overflow-checked. A malformed file yields a
//! typed `ParseError`, never a panic and never an allocation sized by
//! hostile input.

use std::fmt;

// -------------------------------------------------------------- limits --
// Hard caps are an order of magnitude above any sane value so corrupt
// length fields fail closed instead of allocating.
pub const MAX_VERSION: u32 = 10;
pub const MAX_TENSORS: u32 = 1 << 22; // ~4.2M entries
pub const MAX_NAME_LEN: u32 = 4096;
pub const MAX_DIMS: u32 = 32;
pub const MAX_G4_LAYERS: u32 = 4096; // layer_types count
pub const MAX_G4_STRLEN: u32 = 4096; // per layer-type / hidden_act bytes

// -------------------------------------------------------------- errors --
#[derive(Debug, Clone, PartialEq)]
pub enum ParseError {
    /// Fewer than `need` bytes remain at `offset`.
    Truncated { offset: usize, need: usize, remain: usize },
    BadMagic,
    /// Version outside 1..=MAX_VERSION.
    BadVersion(u32),
    /// gemma4 marker neither 0 nor 1.
    BadG4Marker(u32),
    /// A length/count field exceeds its hard cap.
    FieldTooLarge { field: &'static str, value: u64, cap: u64 },
    /// name/shape/count*4 extends past the end of file.
    PayloadOutOfBounds { offset: usize, len: u64, file_len: u64 },
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Truncated { offset, need, remain } => write!(
                f, "truncated at {offset}: need {need} bytes, {remain} remain"),
            Self::BadMagic => write!(f, "bad magic (expected XCN1)"),
            Self::BadVersion(v) => write!(f, "unsupported version {v}"),
            Self::BadG4Marker(m) => write!(f, "gemma4 marker {m} (want 0|1)"),
            Self::FieldTooLarge { field, value, cap } => write!(
                f, "field {field}={value} exceeds cap {cap}"),
            Self::PayloadOutOfBounds { offset, len, file_len } => write!(
                f, "payload at {offset} len {len} exceeds file len {file_len}"),
        }
    }
}

impl std::error::Error for ParseError {}

type PResult<T> = Result<T, ParseError>;

// -------------------------------------------------------------- reader --
/// Cursor over a byte slice. Little-endian, matching the little-endian
/// u32/u64/f32 writes in xct_ckpt.h (the writer emits host order on LE
/// hosts — the format is de-facto little-endian).
pub struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    pub fn new(buf: &'a [u8]) -> Self {
        Self { buf, pos: 0 }
    }

    pub fn pos(&self) -> usize {
        self.pos
    }

    pub fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    pub fn take(&mut self, n: usize) -> PResult<&'a [u8]> {
        if self.remaining() < n {
            return Err(ParseError::Truncated {
                offset: self.pos,
                need: n,
                remain: self.remaining(),
            });
        }
        let s = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(s)
    }

    pub fn u32(&mut self) -> PResult<u32> {
        let b = self.take(4)?;
        Ok(u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
    }

    pub fn u64(&mut self) -> PResult<u64> {
        let b = self.take(8)?;
        Ok(u64::from_le_bytes([
            b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7],
        ]))
    }

    pub fn f32(&mut self) -> PResult<f32> {
        let b = self.take(4)?;
        Ok(f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
    }

    /// Bounded length-prefixed field: reads the u32 length, checks it
    /// against `cap`, then returns the slice.
    fn len_field(&mut self, field: &'static str, cap: u32) -> PResult<&'a [u8]> {
        let n = self.u32()?;
        if n > cap {
            return Err(ParseError::FieldTooLarge {
                field,
                value: n as u64,
                cap: cap as u64,
            });
        }
        self.take(n as usize)
    }

    /// Skip `n` payload bytes with bounds check (untrusted extent).
    fn skip(&mut self, n: u64) -> PResult<()> {
        if n > self.remaining() as u64 {
            return Err(ParseError::PayloadOutOfBounds {
                offset: self.pos,
                len: n,
                file_len: self.buf.len() as u64,
            });
        }
        self.pos += n as usize;
        Ok(())
    }
}

// -------------------------------------------------------------- config --
/// The versioned config block, mirroring `ckpt_read_config`. Fields use
/// the C++ names; versioned blocks absent in older files stay at the
/// default (0/false/empty) exactly like the C++ reader leaves them.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct CkptConfig {
    pub vocab: u32,
    pub hidden: u32,
    pub inter: u32,
    pub layers: u32,
    pub heads: u32,
    pub kv_heads: u32,
    pub max_pos: u32,
    pub moe_experts: u32,
    pub moe_top_k: u32,
    pub moe_layer_interval: u32,
    pub rope_theta: f32,
    pub rms_eps: f32,
    pub moe_aux_w: f32,
    // v2
    pub moe_expert_inter: u32,
    pub moe_shared_experts: u32,
    pub moe_shared_inter: u32,
    // v3
    pub full_attention_interval: u32,
    pub attn_output_gate: bool,
    pub qk_norm: bool,
    pub shared_expert_gate: bool,
    pub partial_rotary: f32,
    pub lin_key_heads: u32,
    pub lin_key_dim: u32,
    pub lin_value_heads: u32,
    pub lin_value_dim: u32,
    pub lin_conv_kernel: u32,
    // v4
    pub use_vision: bool,
    pub vision_patch_dim: u32,
    pub vision_max_patches: u32,
    // v5
    pub global_attn_interval: u32,
    pub sliding_window: u32,
    pub num_global_kv_heads: u32,
    pub k_eq_v_global: bool,
    pub post_attn_norm: bool,
    pub post_ffw_norm: bool,
    pub ffn_act: u32,
    pub local_rope_proportion: f32,
    pub global_rope_proportion: f32,
    pub rope_theta_local: f32,
    pub rope_theta_global: f32,
    pub final_logit_softcap: f32,
    // v6
    pub moe_router_sigmoid: bool,
    // v7
    pub kv_lora_rank: u32,
    pub q_lora_rank: u32,
    pub qk_nope_head_dim: u32,
    pub qk_rope_head_dim: u32,
    pub moe_auxfree_balance: bool,
    pub moe_lb_bias_rate: f32,
    pub mtp_num_layers: u32,
    pub mtp_loss_weight: f32,
    // v8
    pub yarn_factor: f32,
    pub yarn_orig_pos: u32,
    pub yarn_beta_fast: f32,
    pub yarn_beta_slow: f32,
    pub yarn_attn_factor: f32,
    // v9 gemma4 (present only when the marker reads 1)
    pub is_gemma4: bool,
    pub g4_head_dim: u32,
    pub g4_global_head_dim: u32,
    pub g4_sliding_window: u32,
    pub g4_num_kv_shared_layers: u32,
    pub g4_ple_hidden: u32,
    pub g4_ple_vocab: u32,
    pub g4_use_double_wide_mlp: bool,
    pub g4_tie_embed: bool,
    pub g4_rope_theta_full: f32,
    pub g4_rope_partial_full: f32,
    pub g4_final_logit_softcapping: f32,
    pub g4_attention_scale: f32,
    pub g4_layer_types: Vec<String>,
    pub g4_hidden_act: String,
    // v10
    pub mtp_depth: u32,
    pub mtp_loss_w: f32,
}

fn read_g4(r: &mut Reader<'_>, c: &mut CkptConfig) -> PResult<()> {
    c.is_gemma4 = true;
    c.g4_head_dim = r.u32()?;
    c.g4_global_head_dim = r.u32()?;
    c.g4_sliding_window = r.u32()?;
    c.g4_num_kv_shared_layers = r.u32()?;
    c.g4_ple_hidden = r.u32()?;
    c.g4_ple_vocab = r.u32()?;
    let flags = r.u32()?;
    c.g4_use_double_wide_mlp = flags & 1 != 0;
    c.g4_tie_embed = flags & 2 != 0;
    c.g4_rope_theta_full = r.f32()?;
    c.g4_rope_partial_full = r.f32()?;
    c.g4_final_logit_softcapping = r.f32()?;
    c.g4_attention_scale = r.f32()?;
    let nt = r.u32()?;
    if nt > MAX_G4_LAYERS {
        return Err(ParseError::FieldTooLarge {
            field: "g4.layer_types",
            value: nt as u64,
            cap: MAX_G4_LAYERS as u64,
        });
    }
    for _ in 0..nt {
        let s = r.len_field("g4.layer_type", MAX_G4_STRLEN)?;
        c.g4_layer_types.push(String::from_utf8_lossy(s).into_owned());
    }
    let act = r.len_field("g4.hidden_act", MAX_G4_STRLEN)?;
    c.g4_hidden_act = String::from_utf8_lossy(act).into_owned();
    Ok(())
}

fn read_config(r: &mut Reader<'_>, ver: u32, c: &mut CkptConfig) -> PResult<()> {
    c.vocab = r.u32()?;
    c.hidden = r.u32()?;
    c.inter = r.u32()?;
    c.layers = r.u32()?;
    c.heads = r.u32()?;
    c.kv_heads = r.u32()?;
    c.max_pos = r.u32()?;
    c.moe_experts = r.u32()?;
    c.moe_top_k = r.u32()?;
    c.moe_layer_interval = r.u32()?;
    c.rope_theta = r.f32()?;
    c.rms_eps = r.f32()?;
    c.moe_aux_w = r.f32()?;
    if ver >= 2 {
        c.moe_expert_inter = r.u32()?;
        c.moe_shared_experts = r.u32()?;
        c.moe_shared_inter = r.u32()?;
    }
    if ver >= 3 {
        c.full_attention_interval = r.u32()?;
        let fl = r.u32()?;
        c.attn_output_gate = fl & 1 != 0;
        c.qk_norm = fl & 2 != 0;
        c.shared_expert_gate = fl & 4 != 0;
        c.partial_rotary = r.f32()?;
        c.lin_key_heads = r.u32()?;
        c.lin_key_dim = r.u32()?;
        c.lin_value_heads = r.u32()?;
        c.lin_value_dim = r.u32()?;
        c.lin_conv_kernel = r.u32()?;
    }
    if ver >= 4 {
        c.use_vision = r.u32()? != 0;
        c.vision_patch_dim = r.u32()?;
        c.vision_max_patches = r.u32()?;
    }
    if ver >= 5 {
        c.global_attn_interval = r.u32()?;
        c.sliding_window = r.u32()?;
        c.num_global_kv_heads = r.u32()?;
        let fl = r.u32()?;
        c.k_eq_v_global = fl & 1 != 0;
        c.post_attn_norm = fl & 2 != 0;
        c.post_ffw_norm = fl & 4 != 0;
        c.ffn_act = if fl & 8 != 0 { 1 } else { 0 };
        c.local_rope_proportion = r.f32()?;
        c.global_rope_proportion = r.f32()?;
        c.rope_theta_local = r.f32()?;
        c.rope_theta_global = r.f32()?;
        c.final_logit_softcap = r.f32()?;
    }
    if ver >= 6 {
        c.moe_router_sigmoid = r.u32()? != 0;
    }
    if ver >= 7 {
        c.kv_lora_rank = r.u32()?;
        c.q_lora_rank = r.u32()?;
        c.qk_nope_head_dim = r.u32()?;
        c.qk_rope_head_dim = r.u32()?;
        c.moe_auxfree_balance = r.u32()? != 0;
        c.moe_lb_bias_rate = r.f32()?;
        c.mtp_num_layers = r.u32()?;
        c.mtp_loss_weight = r.f32()?;
    }
    if ver >= 8 {
        c.yarn_factor = r.f32()?;
        c.yarn_orig_pos = r.u32()?;
        c.yarn_beta_fast = r.f32()?;
        c.yarn_beta_slow = r.f32()?;
        c.yarn_attn_factor = r.f32()?;
    }
    if ver >= 9 {
        let g4m = r.u32()?;
        match g4m {
            0 => {}
            1 => read_g4(r, c)?,
            m => return Err(ParseError::BadG4Marker(m)),
        }
    }
    if ver >= 10 {
        c.mtp_depth = r.u32()?;
        c.mtp_loss_w = r.f32()?;
    }
    Ok(())
}

// ------------------------------------------------------------ tensors --
/// One tensor-table entry. `data_offset`/`data_len` locate the f32
/// payload inside the file; the payload itself is never materialized by
/// the parser.
#[derive(Debug, Clone)]
pub struct TensorEntry {
    pub name: String,
    pub shape: Vec<u64>,
    pub count: u64,
    pub data_offset: usize,
    /// count * 4 — already overflow-checked.
    pub data_len: u64,
}

/// Parsed header: magic, version, config, and the declared tensor count.
/// `tensor_table_offset` is where the first entry begins.
#[derive(Debug, PartialEq)]
pub struct CkptHeader {
    pub version: u32,
    pub config: CkptConfig,
    pub tensor_count: u32,
    pub tensor_table_offset: usize,
}

/// Parse magic + version + config + tensor count. Succeeds only when
/// the whole header is well-formed.
pub fn parse_header(buf: &[u8]) -> PResult<CkptHeader> {
    let mut r = Reader::new(buf);
    if r.take(4)? != b"XCN1" {
        return Err(ParseError::BadMagic);
    }
    let ver = r.u32()?;
    if ver < 1 || ver > MAX_VERSION {
        return Err(ParseError::BadVersion(ver));
    }
    let mut config = CkptConfig::default();
    read_config(&mut r, ver, &mut config)?;
    let tensor_count = r.u32()?;
    if tensor_count > MAX_TENSORS {
        return Err(ParseError::FieldTooLarge {
            field: "tensor_count",
            value: tensor_count as u64,
            cap: MAX_TENSORS as u64,
        });
    }
    Ok(CkptHeader {
        version: ver,
        config,
        tensor_count,
        tensor_table_offset: r.pos(),
    })
}

/// Iterator over the tensor table. Each `next` bounds-checks the name,
/// shape, count and payload extent; payloads are skipped, not copied.
pub struct TensorIter<'a> {
    r: Reader<'a>,
    remaining: u32,
}

pub fn tensors<'a>(buf: &'a [u8], header: &CkptHeader) -> TensorIter<'a> {
    let mut r = Reader::new(buf);
    // Safe: parse_header proved the offset is inside buf.
    r.pos = header.tensor_table_offset;
    TensorIter {
        r,
        remaining: header.tensor_count,
    }
}

impl<'a> TensorIter<'a> {
    fn entry(&mut self) -> PResult<TensorEntry> {
        let name = self.r.len_field("tensor.name", MAX_NAME_LEN)?;
        let name = String::from_utf8_lossy(name).into_owned();
        let nd = self.r.u32()?;
        if nd > MAX_DIMS {
            return Err(ParseError::FieldTooLarge {
                field: "tensor.ndims",
                value: nd as u64,
                cap: MAX_DIMS as u64,
            });
        }
        let mut shape = Vec::with_capacity(nd as usize);
        for _ in 0..nd {
            shape.push(self.r.u64()?);
        }
        let count = self.r.u64()?;
        let data_len = count.checked_mul(4).ok_or(ParseError::FieldTooLarge {
            field: "tensor.count*4",
            value: count,
            cap: u64::MAX / 4,
        })?;
        let data_offset = self.r.pos();
        self.r.skip(data_len)?;
        Ok(TensorEntry {
            name,
            shape,
            count,
            data_offset,
            data_len,
        })
    }
}

impl<'a> Iterator for TensorIter<'a> {
    type Item = PResult<TensorEntry>;
    fn next(&mut self) -> Option<Self::Item> {
        if self.remaining == 0 {
            return None;
        }
        self.remaining -= 1;
        Some(self.entry())
    }
}

/// Full structural verification: parses header and walks every tensor
/// entry. Returns `(entries, trailing_bytes)`; `trailing_bytes > 0`
/// means bytes exist after the last payload (the C++ reader ignores
/// them — reported, not rejected).
pub fn verify(buf: &[u8]) -> PResult<(CkptHeader, Vec<TensorEntry>, usize)> {
    let header = parse_header(buf)?;
    let mut entries = Vec::with_capacity(header.tensor_count.min(1 << 16) as usize);
    let mut it = tensors(buf, &header);
    let mut end = header.tensor_table_offset;
    while let Some(e) = it.next() {
        let e = e?;
        end = e.data_offset + e.data_len as usize;
        entries.push(e);
    }
    Ok((header, entries, buf.len() - end))
}

// -------------------------------------------------------------- tests --
#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// Minimal XCN10 writer mirroring ckpt_save/ckpt_write_config for a
    /// non-vision, non-gemma4 config (zeros where the axis is absent).
    struct W(Vec<u8>);
    impl W {
        fn u(&mut self, x: u32) { self.0.extend_from_slice(&x.to_le_bytes()); }
        fn u64w(&mut self, x: u64) { self.0.extend_from_slice(&x.to_le_bytes()); }
        fn f(&mut self, x: f32) { self.0.extend_from_slice(&x.to_le_bytes()); }
        fn raw(&mut self, s: &[u8]) { self.0.extend_from_slice(s); }
    }

    pub(crate) fn build_ckpt(tensors: &[(&str, &[u64], u64)]) -> Vec<u8> {
        let mut b = W(Vec::new());
        b.raw(b"XCN1");
        b.u(10);
        // base block
        b.u(32000); b.u(256); b.u(688); b.u(4); b.u(8); b.u(2); b.u(2048);
        b.u(0); b.u(0); b.u(0);
        b.f(1_000_000.0); b.f(1e-6); b.f(0.0);
        // v2
        b.u(0); b.u(0); b.u(0);
        // v3
        b.u(0); b.u(0); b.f(0.0); b.u(0); b.u(0); b.u(0); b.u(0); b.u(0);
        // v4
        b.u(0); b.u(0); b.u(0);
        // v5
        b.u(0); b.u(0); b.u(0); b.u(0);
        b.f(0.0); b.f(0.0); b.f(0.0); b.f(0.0); b.f(0.0);
        // v6
        b.u(0);
        // v7
        b.u(0); b.u(0); b.u(0); b.u(0); b.u(0); b.f(0.0); b.u(0); b.f(0.0);
        // v8
        b.f(0.0); b.u(0); b.f(0.0); b.f(0.0); b.f(0.0);
        // v9: marker 0 (not gemma4)
        b.u(0);
        // v10
        b.u(2); b.f(0.3);
        // tensor table
        b.u(tensors.len() as u32);
        for &(name, shape, count) in tensors {
            b.u(name.len() as u32);
            b.raw(name.as_bytes());
            b.u(shape.len() as u32);
            for &d in shape {
                b.u64w(d);
            }
            b.u64w(count);
            b.0.extend(std::iter::repeat(0u8).take(count as usize * 4));
        }
        b.0
    }

    #[test]
    fn parses_valid_xcn10() {
        let buf = build_ckpt(&[
            ("embed.weight", &[32000, 256], 8),
            ("lm_head.weight", &[256, 32000], 4),
        ]);
        let (h, entries, trailing) = verify(&buf).unwrap();
        assert_eq!(h.version, 10);
        assert_eq!(h.config.vocab, 32000);
        assert_eq!(h.config.hidden, 256);
        assert_eq!(h.config.mtp_depth, 2);
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].name, "embed.weight");
        assert_eq!(entries[0].shape, vec![32000, 256]);
        assert_eq!(entries[0].count, 8);
        assert_eq!(entries[1].data_len, 16);
        assert_eq!(trailing, 0);
        assert_eq!(buf.len(), entries[1].data_offset + 16);
    }

    #[test]
    fn rejects_bad_magic() {
        let mut buf = build_ckpt(&[]);
        buf[0] = b'Z';
        assert_eq!(parse_header(&buf), Err(ParseError::BadMagic));
    }

    #[test]
    fn rejects_bad_version() {
        let mut buf = build_ckpt(&[]);
        buf[4..8].copy_from_slice(&11u32.to_le_bytes());
        assert_eq!(parse_header(&buf), Err(ParseError::BadVersion(11)));
    }

    #[test]
    fn rejects_truncated_header() {
        let buf = build_ckpt(&[]);
        for cut in [3usize, 6, 40, 100, buf.len() - 1] {
            assert!(matches!(
                verify(&buf[..cut]),
                Err(ParseError::Truncated { .. }) | Err(ParseError::PayloadOutOfBounds { .. })
            ), "cut={cut}");
        }
    }

    #[test]
    fn rejects_giant_name_len() {
        let mut buf = build_ckpt(&[("a", &[1], 1)]);
        // Locate tensor table: walk to the name-len field of entry 0.
        // header = 8 + config(120 fixed + g4 marker + v10) — recompute:
        // simplest is parse_header's tensor_table_offset.
        let h = parse_header(&buf).unwrap();
        let nlen_at = h.tensor_table_offset;
        buf[nlen_at..nlen_at + 4]
            .copy_from_slice(&(u32::MAX).to_le_bytes());
        let mut it = tensors(&buf, &h);
        assert!(matches!(
            it.next().unwrap(),
            Err(ParseError::FieldTooLarge { field: "tensor.name", .. })
        ));
    }

    #[test]
    fn rejects_giant_count_overflow() {
        // count near u64::MAX — count*4 must overflow-check, not wrap.
        let mut buf = build_ckpt(&[("a", &[1], 1)]);
        let h = parse_header(&buf).unwrap();
        // entry layout: u32 nlen | name | u32 nd | nd u64s | u64 count
        let mut p = h.tensor_table_offset;
        let nlen = u32::from_le_bytes(buf[p..p + 4].try_into().unwrap()) as usize;
        p += 4 + nlen;
        let nd = u32::from_le_bytes(buf[p..p + 4].try_into().unwrap()) as usize;
        p += 4 + nd * 8;
        buf[p..p + 8].copy_from_slice(&(u64::MAX).to_le_bytes());
        let mut it = tensors(&buf, &h);
        assert!(matches!(
            it.next().unwrap(),
            Err(ParseError::FieldTooLarge { field: "tensor.count*4", .. })
        ));
    }

    #[test]
    fn rejects_payload_past_eof() {
        // count says 100 floats but the file ends early.
        let mut buf = build_ckpt(&[("a", &[10], 100)]);
        buf.truncate(buf.len() - 200); // cut inside payload
        let h = parse_header(&buf).unwrap();
        let mut it = tensors(&buf, &h);
        assert!(matches!(
            it.next().unwrap(),
            Err(ParseError::PayloadOutOfBounds { .. })
        ));
    }

    #[test]
    fn reports_trailing_bytes() {
        let mut buf = build_ckpt(&[("a", &[1], 1)]);
        buf.extend_from_slice(&[0u8; 7]);
        let (_, _, trailing) = verify(&buf).unwrap();
        assert_eq!(trailing, 7);
    }

    #[test]
    fn gemma4_marker_must_be_0_or_1() {
        let mut buf = build_ckpt(&[]);
        let h = parse_header(&buf).unwrap();
        // tensor_table_offset is past the count field; before it lie
        // [marker][mtp_depth][mtp_loss_w][tensor_count] — marker at -16.
        let marker_at = h.tensor_table_offset - 16;
        buf[marker_at..marker_at + 4].copy_from_slice(&7u32.to_le_bytes());
        assert_eq!(parse_header(&buf), Err(ParseError::BadG4Marker(7)));
    }

    #[test]
    fn iter_respects_declared_count() {
        let buf = build_ckpt(&[("a", &[1], 1), ("b", &[1], 1)]);
        let h = parse_header(&buf).unwrap();
        assert_eq!(tensors(&buf, &h).count(), 2);
    }
}

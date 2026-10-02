//! xc-format — Rust owner of Xingcheng binary format validation.
//!
//! Phase 1 covers the XCN1..XCN10 checkpoint header: strict structural
//! validation with exact field order mirrored from `xct_ckpt.h` (write
//! order) and `XcnHeader.cs` (governed reader). Any drift fails closed.
//! The parser consumes exactly the header; trailing bytes are the weight
//! tensors and are out of scope for header validation.
//!
//! Language-architecture placement (2026-10-02, human-governor directive):
//! storage / data / file-format validation is Rust-owned. The C#
//! `XcnHeader` reader stays as the thin governed entry until this lane is
//! wired end to end; no new format logic lands there. B166 remains the
//! sole language authority.

/// Highest checkpoint version this validator understands.
pub const MAX_VERSION: u32 = 10;

/// Gemma4 marker value meaning "g4 fields follow" (XCN9+).
pub const GEMMA4_MARKER: u32 = 1;

/// Structural failure of an XCN header. Every variant is fail-closed:
/// a header that raises any of these is rejected, never guessed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HeaderError {
    /// Fewer than the 8 magic+version bytes.
    TooShort,
    /// First four bytes are not `XCN1`.
    BadMagic,
    /// Version outside 1..=MAX_VERSION.
    BadVersion(u32),
    /// XCN9+ marker is neither 0 (non-gemma4) nor 1 (g4 fields follow).
    BadGemma4Marker(u32),
    /// A block ends before its declared fields complete.
    Truncated(&'static str),
    /// A length-prefixed string is not valid UTF-8.
    BadString(&'static str),
}

/// Parsed header summary. Counts are the raw stored values; semantic
/// validation (e.g. publishable-scale bands) belongs to the caller.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HeaderSummary {
    /// Checkpoint version (1..=MAX_VERSION).
    pub version: u32,
    /// Bytes consumed by the header (weights start here).
    pub header_len: usize,
    pub vocab_size: u32,
    pub hidden_size: u32,
    pub num_hidden_layers: u32,
    pub moe_num_experts: u32,
    /// True when the XCN9 gemma4 block is present (marker == 1).
    pub has_gemma4: bool,
    /// XCN10 MTP-stack depth; None below v10.
    pub mtp_stack_depth: Option<u32>,
}

struct Cursor<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Cursor { bytes, pos: 0 }
    }

    fn take(&mut self, n: usize, what: &'static str) -> Result<&'a [u8], HeaderError> {
        let end = self.pos.saturating_add(n);
        if end > self.bytes.len() || end < self.pos {
            return Err(HeaderError::Truncated(what));
        }
        let slice = &self.bytes[self.pos..end];
        self.pos = end;
        Ok(slice)
    }

    fn u32(&mut self, what: &'static str) -> Result<u32, HeaderError> {
        let b = self.take(4, what)?;
        Ok(u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
    }

    fn f32(&mut self, what: &'static str) -> Result<(), HeaderError> {
        self.take(4, what)?;
        Ok(())
    }

    fn counted_str(&mut self, what: &'static str) -> Result<(), HeaderError> {
        let len = self.u32(what)? as usize;
        let b = self.take(len, what)?;
        core::str::from_utf8(b).map_err(|_| HeaderError::BadString(what))?;
        Ok(())
    }
}

/// Validate and summarize an XCN checkpoint header.
pub fn parse_header(bytes: &[u8]) -> Result<HeaderSummary, HeaderError> {
    if bytes.len() < 8 {
        return Err(HeaderError::TooShort);
    }
    let mut c = Cursor::new(bytes);
    let magic = c.take(4, "magic")?;
    if magic != b"XCN1" {
        return Err(HeaderError::BadMagic);
    }
    let ver = c.u32("version")?;
    if ver < 1 || ver > MAX_VERSION {
        return Err(HeaderError::BadVersion(ver));
    }

    // v1 base block: 10x u32 geometry + 3x f32 hyper-parameters.
    let vocab_size = c.u32("vocab_size")?;
    let hidden_size = c.u32("hidden_size")?;
    c.u32("intermediate_size")?;
    let num_hidden_layers = c.u32("num_hidden_layers")?;
    c.u32("num_attention_heads")?;
    c.u32("num_key_value_heads")?;
    c.u32("max_position_embeddings")?;
    let moe_num_experts = c.u32("moe_num_experts")?;
    c.u32("moe_top_k")?;
    c.u32("moe_layer_interval")?;
    c.f32("rope_theta")?;
    c.f32("rms_norm_eps")?;
    c.f32("moe_aux_loss_weight")?;

    if ver >= 2 {
        c.u32("moe_expert_intermediate_size")?;
        c.u32("moe_num_shared_experts")?;
        c.u32("moe_shared_intermediate_size")?;
    }
    if ver >= 3 {
        c.u32("full_attention_interval")?;
        c.u32("attn_flags")?;
        c.f32("partial_rotary_factor")?;
        c.u32("linear_num_key_heads")?;
        c.u32("linear_key_head_dim")?;
        c.u32("linear_num_value_heads")?;
        c.u32("linear_value_head_dim")?;
        c.u32("linear_conv_kernel_dim")?;
    }
    if ver >= 4 {
        c.u32("use_vision")?;
        c.u32("vision_patch_dim")?;
        c.u32("vision_max_patches")?;
    }
    if ver >= 5 {
        c.u32("global_attention_interval")?;
        c.u32("sliding_window_size")?;
        c.u32("num_global_kv_heads")?;
        c.u32("gemma_flags")?;
        c.f32("local_rope_proportion")?;
        c.f32("global_rope_proportion")?;
        c.f32("local_base_frequency")?;
        c.f32("global_base_frequency")?;
        c.f32("final_logit_softcap")?;
    }
    if ver >= 6 {
        c.u32("moe_router_sigmoid")?;
    }
    if ver >= 7 {
        c.u32("kv_lora_rank")?;
        c.u32("q_lora_rank")?;
        c.u32("qk_nope_head_dim")?;
        c.u32("qk_rope_head_dim")?;
        c.u32("moe_auxfree_balance")?;
        c.f32("moe_lb_bias_rate")?;
        c.u32("num_nextn_predict_layers")?;
        c.f32("mtp_loss_weight")?;
    }
    if ver >= 8 {
        c.f32("yarn_factor")?;
        c.u32("yarn_original_max_position_embeddings")?;
        c.f32("yarn_beta_fast")?;
        c.f32("yarn_beta_slow")?;
        c.f32("yarn_attention_factor")?;
    }
    let mut has_gemma4 = false;
    if ver >= 9 {
        // The marker u32 is always present at ver >= 9, even for
        // non-gemma4 (canonical fused) checkpoints.
        let marker = c.u32("gemma4_marker")?;
        if marker == GEMMA4_MARKER {
            has_gemma4 = true;
            c.u32("head_dim")?;
            c.u32("global_head_dim")?;
            c.u32("sliding_window")?;
            c.u32("num_kv_shared_layers")?;
            c.u32("hidden_size_per_layer_input")?;
            c.u32("vocab_size_per_layer_input")?;
            c.u32("gemma4_flags")?;
            c.f32("rope_theta_full")?;
            c.f32("rope_partial_rotary_factor")?;
            c.f32("final_logit_softcapping")?;
            c.f32("attention_scale")?;
            let nt = c.u32("layer_types_count")?;
            for _ in 0..nt {
                c.counted_str("layer_type")?;
            }
            c.counted_str("hidden_activation")?;
        } else if marker != 0 {
            return Err(HeaderError::BadGemma4Marker(marker));
        }
    }
    let mut mtp_stack_depth = None;
    if ver >= 10 {
        mtp_stack_depth = Some(c.u32("mtp_stack_depth")?);
        c.f32("mtp_stack_loss_weight")?;
    }

    Ok(HeaderSummary {
        version: ver,
        header_len: c.pos,
        vocab_size,
        hidden_size,
        num_hidden_layers,
        moe_num_experts,
        has_gemma4,
        mtp_stack_depth,
    })
}

#[cfg(test)]
mod vector_tests;

#[cfg(test)]
mod tests {
    use super::*;

    fn w32(out: &mut Vec<u8>, v: u32) {
        out.extend_from_slice(&v.to_le_bytes());
    }
    fn wf32(out: &mut Vec<u8>, v: f32) {
        out.extend_from_slice(&v.to_le_bytes());
    }
    fn wstr(out: &mut Vec<u8>, s: &str) {
        w32(out, s.len() as u32);
        out.extend_from_slice(s.as_bytes());
    }

    /// Minimal valid header for `ver`, with marker 0 at v9+ and no g4.
    fn header(ver: u32) -> Vec<u8> {
        let mut b = Vec::new();
        b.extend_from_slice(b"XCN1");
        w32(&mut b, ver);
        for _ in 0..10 {
            w32(&mut b, 8);
        }
        for _ in 0..3 {
            wf32(&mut b, 0.5);
        }
        if ver >= 2 {
            for _ in 0..3 {
                w32(&mut b, 2);
            }
        }
        if ver >= 3 {
            w32(&mut b, 4);
            w32(&mut b, 7);
            wf32(&mut b, 0.25);
            for _ in 0..5 {
                w32(&mut b, 3);
            }
        }
        if ver >= 4 {
            for _ in 0..3 {
                w32(&mut b, 0);
            }
        }
        if ver >= 5 {
            for _ in 0..4 {
                w32(&mut b, 1);
            }
            for _ in 0..5 {
                wf32(&mut b, 1.0);
            }
        }
        if ver >= 6 {
            w32(&mut b, 0);
        }
        if ver >= 7 {
            for _ in 0..5 {
                w32(&mut b, 0);
            }
            wf32(&mut b, 0.0);
            w32(&mut b, 0);
            wf32(&mut b, 0.0);
        }
        if ver >= 8 {
            wf32(&mut b, 1.0);
            w32(&mut b, 2048);
            wf32(&mut b, 1.0);
            wf32(&mut b, 2.0);
            wf32(&mut b, 1.0);
        }
        if ver >= 9 {
            w32(&mut b, 0);
        }
        if ver >= 10 {
            w32(&mut b, 3);
            wf32(&mut b, 0.1);
        }
        b
    }

    #[test]
    fn v1_roundtrip() {
        let s = parse_header(&header(1)).expect("v1 must parse");
        assert_eq!(s.version, 1);
        assert_eq!(s.header_len, 8 + 40 + 12);
        assert!(!s.has_gemma4);
        assert_eq!(s.mtp_stack_depth, None);
    }

    #[test]
    fn v10_roundtrip() {
        let s = parse_header(&header(10)).expect("v10 must parse");
        assert_eq!(s.version, 10);
        assert_eq!(s.mtp_stack_depth, Some(3));
    }

    #[test]
    fn trailing_weights_ignored() {
        let mut b = header(3);
        b.extend_from_slice(&[0u8; 1024]);
        let s = parse_header(&b).expect("trailing bytes are weights");
        assert_eq!(s.version, 3);
        assert!(s.header_len < b.len());
    }

    #[test]
    fn bad_magic() {
        let mut b = header(1);
        b[0] = b'Y';
        assert_eq!(parse_header(&b), Err(HeaderError::BadMagic));
    }

    #[test]
    fn bad_version() {
        let mut b = header(1);
        b[4..8].copy_from_slice(&99u32.to_le_bytes());
        assert_eq!(parse_header(&b), Err(HeaderError::BadVersion(99)));
        let mut z = header(1);
        z[4..8].copy_from_slice(&0u32.to_le_bytes());
        assert_eq!(parse_header(&z), Err(HeaderError::BadVersion(0)));
    }

    #[test]
    fn truncated_block() {
        let b = header(5);
        let cut = &b[..b.len() - 3];
        assert!(matches!(
            parse_header(cut),
            Err(HeaderError::Truncated(_))
        ));
    }

    #[test]
    fn too_short() {
        assert_eq!(parse_header(&[b'X', b'C']), Err(HeaderError::TooShort));
    }

    #[test]
    fn bad_gemma4_marker() {
        let mut b = header(9);
        let at = b.len() - 4;
        b[at..].copy_from_slice(&7u32.to_le_bytes());
        assert_eq!(parse_header(&b), Err(HeaderError::BadGemma4Marker(7)));
    }

    #[test]
    fn gemma4_block_parses() {
        let mut b = header(9);
        // Replace the marker-0 tail with a minimal g4 block.
        b.truncate(b.len() - 4);
        b.extend_from_slice(&1u32.to_le_bytes());
        for _ in 0..6 {
            b.extend_from_slice(&4u32.to_le_bytes());
        }
        b.extend_from_slice(&3u32.to_le_bytes());
        for _ in 0..4 {
            b.extend_from_slice(&1f32.to_le_bytes());
        }
        b.extend_from_slice(&2u32.to_le_bytes());
        let mut tmp = Vec::new();
        wstr(&mut tmp, "sliding_attention");
        wstr(&mut tmp, "full_attention");
        b.extend_from_slice(&tmp);
        wstr(&mut b, "gelu_tanh");
        let s = parse_header(&b).expect("g4 block must parse");
        assert!(s.has_gemma4);
        assert_eq!(s.header_len, b.len());
    }
}

//! diff.rs — delta-candidate verification: compare a candidate
//! checkpoint against its base before promotion.
//!
//! Structural parity is fail-closed: different versions, config fields
//! that diverge, name/shape/count mismatches or an unequal tensor count
//! make the pair incompatible — the report still returns but
//! `compatible` is false so the caller can deny promotion.
//!
//! Payload comparison is byte-exact first; differing tensors get
//! lane-level stats (differing element count, max |a-b|, NaN counts)
//! computed over the f32 view — no payload ever materializes out of
//! the mapped region.

use crate::xcn1;
use serde_json::json;

#[derive(Default)]
struct DiffStats {
    tensors: u64,
    tensors_identical: u64,
    tensors_changed: u64,
    lanes_changed: u64,
    nan_a: u64,
    nan_b: u64,
    max_abs_diff: f64,
}

fn payload_diff(a: &[u8], b: &[u8], st: &mut DiffStats) -> serde_json::Value {
    // Caller guarantees equal lengths (count parity already checked).
    debug_assert_eq!(a.len() % 4, 0);
    let mut lanes = 0u64;
    let mut maxd = 0.0f64;
    let (mut na, mut nb) = (0u64, 0u64);
    for i in (0..a.len()).step_by(4) {
        let ba = &a[i..i + 4];
        let bb = &b[i..i + 4];
        if ba == bb {
            continue;
        }
        lanes += 1;
        let fa = f32::from_le_bytes([ba[0], ba[1], ba[2], ba[3]]);
        let fb = f32::from_le_bytes([bb[0], bb[1], bb[2], bb[3]]);
        if fa.is_nan() {
            na += 1;
        }
        if fb.is_nan() {
            nb += 1;
        }
        if fa.is_finite() && fb.is_finite() {
            let d = (fa as f64 - fb as f64).abs();
            if d > maxd {
                maxd = d;
            }
        }
    }
    st.lanes_changed += lanes;
    st.nan_a += na;
    st.nan_b += nb;
    if maxd > st.max_abs_diff {
        st.max_abs_diff = maxd;
    }
    json!({
        "identical": false,
        "lanes_changed": lanes,
        "max_abs_diff": maxd,
        "nan_base": na,
        "nan_candidate": nb,
    })
}

/// Field-by-field config parity — every CkptConfig member compared so a
/// silent architecture drift between base and candidate is visible.
fn config_diffs(a: &xcn1::CkptConfig, b: &xcn1::CkptConfig) -> Vec<String> {
    let mut out = Vec::new();
    macro_rules! cmp {
        ($f:ident) => {
            if a.$f != b.$f {
                out.push(format!("{}.{}: {:?} -> {:?}", "config",
                                 stringify!($f), a.$f, b.$f));
            }
        };
    }
    cmp!(vocab); cmp!(hidden); cmp!(inter); cmp!(layers); cmp!(heads);
    cmp!(kv_heads); cmp!(max_pos); cmp!(moe_experts); cmp!(moe_top_k);
    cmp!(moe_layer_interval);
    cmp!(rope_theta); cmp!(rms_eps); cmp!(moe_aux_w);
    cmp!(moe_expert_inter); cmp!(moe_shared_experts);
    cmp!(moe_shared_inter); cmp!(full_attention_interval);
    cmp!(attn_output_gate); cmp!(qk_norm); cmp!(shared_expert_gate);
    cmp!(partial_rotary); cmp!(lin_key_heads); cmp!(lin_key_dim);
    cmp!(lin_value_heads); cmp!(lin_value_dim); cmp!(lin_conv_kernel);
    cmp!(use_vision); cmp!(vision_patch_dim); cmp!(vision_max_patches);
    cmp!(global_attn_interval); cmp!(sliding_window);
    cmp!(num_global_kv_heads); cmp!(k_eq_v_global); cmp!(post_attn_norm);
    cmp!(post_ffw_norm); cmp!(ffn_act); cmp!(local_rope_proportion);
    cmp!(global_rope_proportion); cmp!(rope_theta_local);
    cmp!(rope_theta_global); cmp!(final_logit_softcap);
    cmp!(moe_router_sigmoid); cmp!(kv_lora_rank); cmp!(q_lora_rank);
    cmp!(qk_nope_head_dim); cmp!(qk_rope_head_dim);
    cmp!(moe_auxfree_balance); cmp!(moe_lb_bias_rate);
    cmp!(mtp_num_layers); cmp!(mtp_loss_weight);
    cmp!(yarn_factor); cmp!(yarn_orig_pos); cmp!(yarn_beta_fast);
    cmp!(yarn_beta_slow); cmp!(yarn_attn_factor);
    cmp!(is_gemma4); cmp!(mtp_depth); cmp!(mtp_loss_w);
    if a.g4_layer_types != b.g4_layer_types {
        out.push("config.g4_layer_types differ".into());
    }
    if a.g4_hidden_act != b.g4_hidden_act {
        out.push("config.g4_hidden_act differ".into());
    }
    out
}

/// Diff two mapped checkpoint files. The returned object carries
/// `compatible` (structural parity) and per-tensor stats.
pub fn ckpt_diff(base: &[u8], cand: &[u8]) -> Result<serde_json::Value, String> {
    let (bh, be, _bt) =
        xcn1::verify(base).map_err(|e| format!("base: {e}"))?;
    let (ch, ce, _ct) =
        xcn1::verify(cand).map_err(|e| format!("candidate: {e}"))?;

    let cfg_diffs = config_diffs(&bh.config, &ch.config);
    let mut compatible = bh.version == ch.version && cfg_diffs.is_empty();
    let mut notes: Vec<String> = Vec::new();
    if bh.version != ch.version {
        notes.push(format!(
            "version differs: {} -> {}", bh.version, ch.version));
    }
    if !cfg_diffs.is_empty() {
        notes.push(format!("{} config field(s) differ", cfg_diffs.len()));
    }
    if bh.tensor_count != ch.tensor_count {
        compatible = false;
        notes.push(format!(
            "tensor count differs: {} -> {}",
            bh.tensor_count, ch.tensor_count));
    }

    let mut st = DiffStats {
        tensors: be.len().min(ce.len()) as u64,
        ..Default::default()
    };
    let mut tensor_reports = Vec::new();
    for (i, (ea, eb)) in be.iter().zip(ce.iter()).enumerate() {
        if ea.name != eb.name || ea.shape != eb.shape || ea.count != eb.count {
            compatible = false;
            tensor_reports.push(json!({
                "index": i,
                "identical": false,
                "structural_mismatch": true,
                "base": {"name": ea.name, "shape": ea.shape,
                         "count": ea.count},
                "candidate": {"name": eb.name, "shape": eb.shape,
                              "count": eb.count},
            }));
            continue;
        }
        let pa = &base[ea.data_offset..ea.data_offset + ea.data_len as usize];
        let pb = &cand[eb.data_offset..eb.data_offset + eb.data_len as usize];
        if pa == pb {
            st.tensors_identical += 1;
            continue; // fully identical tensors are omitted from the list
        }
        st.tensors_changed += 1;
        let mut rep = payload_diff(pa, pb, &mut st);
        rep["index"] = json!(i);
        rep["name"] = json!(ea.name);
        tensor_reports.push(rep);
    }

    Ok(json!({
        "format": "xstore-ckpt-diff/v1",
        "compatible": compatible,
        "version": bh.version,
        "config_diffs": cfg_diffs,
        "notes": notes,
        "stats": {
            "tensors_compared": st.tensors,
            "tensors_identical": st.tensors_identical,
            "tensors_changed": st.tensors_changed,
            "lanes_changed": st.lanes_changed,
            "nan_in_base": st.nan_a,
            "nan_in_candidate": st.nan_b,
            "max_abs_diff": st.max_abs_diff,
        },
        "changed_tensors": tensor_reports,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::xcn1::tests::build_ckpt;

    fn buf_with_payload(tensors: &[(&str, &[u64], u64)], fill: u8) -> Vec<u8> {
        let mut b = build_ckpt(tensors);
        // Flip every payload byte to `fill` — the builder writes zeros,
        // so locate the last count*4 byte block per tensor. Simplest:
        // rebuild payloads by XOR — payload bytes are the only zero
        // runs of length >= 4 at the tail region; instead mutate via
        // verified offsets.
        let (_h, es, _t) = xcn1::verify(&b).unwrap();
        for e in &es {
            for i in 0..e.data_len as usize {
                b[e.data_offset + i] = fill;
            }
        }
        b
    }

    #[test]
    fn self_diff_is_compatible_and_identical() {
        let a = build_ckpt(&[("w", &[2, 2], 4)]);
        let r = ckpt_diff(&a, &a).unwrap();
        assert_eq!(r["compatible"], true);
        assert_eq!(r["stats"]["tensors_identical"], 1);
        assert_eq!(r["stats"]["tensors_changed"], 0);
    }

    #[test]
    fn detects_single_lane_delta() {
        let a = buf_with_payload(&[("w", &[2], 2)], 0);
        let mut b = a.clone();
        // Flip one payload lane byte.
        let (_h, es, _t) = xcn1::verify(&a).unwrap();
        b[es[0].data_offset] = 0x01;
        let r = ckpt_diff(&a, &b).unwrap();
        assert_eq!(r["compatible"], true);
        assert_eq!(r["stats"]["lanes_changed"], 1);
        assert_eq!(r["stats"]["tensors_changed"], 1);
        assert_eq!(r["changed_tensors"][0]["name"], "w");
    }

    #[test]
    fn structural_mismatch_is_incompatible() {
        let a = build_ckpt(&[("w", &[2], 2)]);
        let b = build_ckpt(&[("w", &[2], 2), ("extra", &[1], 1)]);
        let r = ckpt_diff(&a, &b).unwrap();
        assert_eq!(r["compatible"], false);
    }

    #[test]
    fn corrupt_candidate_is_rejected() {
        let a = build_ckpt(&[("w", &[2], 2)]);
        let r = ckpt_diff(&a, b"not-a-ckpt");
        assert!(r.is_err());
    }
}

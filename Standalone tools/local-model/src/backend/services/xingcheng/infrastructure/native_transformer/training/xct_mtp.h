// xct_mtp.h — B94 fragment of xingcheng_trainer.cpp (v29 Qwen3.8-Max MTP).
// Included once by xingcheng_trainer.cpp inside namespace xct.
#pragma once

// ---------------------------------------------------- MTP module (v29) --
//
// Qwen3-Next/Max multi-token prediction: each depth-d module fuses the
// previous hidden stream with the next token's embedding —
//   u[t] = W_proj · [ rms(h_{d-1}[t]) ; rms(embed[ids[t+d+1]]) ]   (2H → H)
//   h_d  = decoder_block(u)   (causal GQA + RoPE + SwiGLU, c.inter)
//   logits_d[t] = lm_head · rms(h_d[t])         predicts ids[t+d+2]
// h_0 = trunk hidden (post final norm), text rows only — vision prefix
// rows never enter the MTP stack. lm_head and embed are shared with the
// trunk. Rows per depth: R_d = PT-2-d. The aux CE densifies supervision
// and the head doubles as the speculative-decoding draft substrate.

static void mtp_fwd(const Params& p, const ModelConfig& c,
                    const std::vector<int>& ids, Fwd& o) {
    o.mtp.clear();
    if (c.mtp_depth <= 0) return;
    const int PT = (int)ids.size();
    const int P = o.vision_patches;
    const int H = c.hidden, hd = H / c.heads;
    const int Hq = c.heads * hd;
    const int kvh = std::max(1, c.kv_heads), Hkv = kvh * hd;
    const int group = c.heads / kvh;
    const int rd = c.rotary_dim();
    const float scale = 1.0f / std::sqrt((float)hd);
    for (int d = 0; d < c.mtp_depth; ++d) {
        MtpCache M;
        const int R = PT - 2 - d;
        if (R <= 0) { o.mtp.push_back(std::move(M)); break; }
        M.rows = R;
        const std::string b = "mtp." + std::to_string(d) + ".";
        // prev stream: d=0 reads trunk hidden at row P+t (text positions);
        // d>0 reads module d-1's block output at row t directly.
        const std::vector<float>& hv =
            (d == 0) ? o.hidden : o.mtp[(size_t)d - 1].x2;
        const int hoff = (d == 0) ? P : 0;
        M.eh_in.resize((size_t)R * H);
        M.ee_in.resize((size_t)R * H);
        for (int t = 0; t < R; ++t) {
            const float* hr = hv.data() + (size_t)(hoff + t) * H;
            std::copy(hr, hr + H, M.eh_in.data() + (size_t)t * H);
            const float* er = p.w.at("embed").d.data() +
                              (size_t)ids[t + d + 1] * H;
            std::copy(er, er + H, M.ee_in.data() + (size_t)t * H);
        }
        M.eh_rms.resize(R); M.ee_rms.resize(R);
        std::vector<float> ehn((size_t)R * H), een((size_t)R * H);
        rmsnorm_fwd(M.eh_in.data(), p.w.at(b + "eh").d.data(),
                    ehn.data(), M.eh_rms.data(), R, H, c.rms_eps);
        rmsnorm_fwd(M.ee_in.data(), p.w.at(b + "et").d.data(),
                    een.data(), M.ee_rms.data(), R, H, c.rms_eps);
        M.cat.assign((size_t)R * 2 * H, 0.0f);
        for (int t = 0; t < R; ++t) {
            std::copy(ehn.data() + (size_t)t * H,
                      ehn.data() + (size_t)t * H + H,
                      M.cat.data() + (size_t)t * 2 * H);
            std::copy(een.data() + (size_t)t * H,
                      een.data() + (size_t)t * H + H,
                      M.cat.data() + (size_t)t * 2 * H + H);
        }
        M.u.resize((size_t)R * H);
        linear_fwd(M.cat.data(), p.w.at(b + "proj"), M.u.data(),
                   R, 2 * H, H);
        // ---- causal decoder block ----
        M.n1.resize((size_t)R * H); M.rms1.resize(R);
        rmsnorm_fwd(M.u.data(), p.w.at(b + "norm1").d.data(),
                    M.n1.data(), M.rms1.data(), R, H, c.rms_eps);
        M.q.resize((size_t)R * Hq);
        M.k.resize((size_t)R * Hkv); M.v.resize((size_t)R * Hkv);
        linear_fwd(M.n1.data(), p.w.at(b + "wq"), M.q.data(), R, H, Hq);
        linear_fwd(M.n1.data(), p.w.at(b + "wk"), M.k.data(), R, H, Hkv);
        linear_fwd(M.n1.data(), p.w.at(b + "wv"), M.v.data(), R, H, Hkv);
        if (rd < hd) {
            rope_hf_partial(M.q.data(), R, c.heads, hd, rd,
                            c.rope_theta, false);
            rope_hf_partial(M.k.data(), R, kvh, hd, rd,
                            c.rope_theta, false);
        } else {
            rope(M.q.data(), R, c.heads, hd, c.rope_theta, false);
            rope(M.k.data(), R, kvh, hd, c.rope_theta, false);
        }
        M.probs.assign((size_t)c.heads * R * R, 0.0f);
        M.attn_out.assign((size_t)R * Hq, 0.0f);
        parallel_for(c.heads, [&](int64_t hb, int64_t he) {
            for (int64_t h = hb; h < he; ++h) {
                int kh2 = (int)h / group;
                for (int t = 0; t < R; ++t) {
                    float* pr = M.probs.data() + ((size_t)h * R + t) * R;
                    float mx = -1e30f;
                    const float* qr =
                        M.q.data() + ((size_t)t * c.heads + h) * hd;
                    for (int s = 0; s <= t; ++s) {
                        const float* kr =
                            M.k.data() + ((size_t)s * kvh + kh2) * hd;
                        pr[s] = tpu_dot(qr, kr, hd) * scale;
                        mx = std::max(mx, pr[s]);
                    }
                    float sum = 0.0f;
                    for (int s = 0; s <= t; ++s) {
                        pr[s] = std::exp(pr[s] - mx); sum += pr[s];
                    }
                    float inv = 1.0f / sum;
                    float* ao =
                        M.attn_out.data() + ((size_t)t * c.heads + h) * hd;
                    for (int s = 0; s <= t; ++s) {
                        pr[s] *= inv;
                        const float* vr =
                            M.v.data() + ((size_t)s * kvh + kh2) * hd;
                        tpu_axpy(ao, pr[s], vr, hd);
                    }
                }
            }
        });
        std::vector<float> aproj((size_t)R * H);
        linear_fwd(M.attn_out.data(), p.w.at(b + "wo"), aproj.data(),
                   R, Hq, H);
        M.x1.resize((size_t)R * H);
        tpu_elementwise((int64_t)M.x1.size(), [&](int64_t i) {
            M.x1[(size_t)i] = M.u[(size_t)i] + aproj[(size_t)i];
        });
        M.n2.resize((size_t)R * H); M.rms2.resize(R);
        rmsnorm_fwd(M.x1.data(), p.w.at(b + "norm2").d.data(),
                    M.n2.data(), M.rms2.data(), R, H, c.rms_eps);
        M.fa.resize((size_t)R * c.inter);
        M.fb.resize((size_t)R * c.inter); M.fh.resize((size_t)R * c.inter);
        linear_fwd(M.n2.data(), p.w.at(b + "w1"), M.fa.data(), R, H, c.inter);
        linear_fwd(M.n2.data(), p.w.at(b + "w3"), M.fb.data(), R, H, c.inter);
        tpu_elementwise((int64_t)M.fh.size(), [&](int64_t i) {
            M.fh[(size_t)i] = gate_act_f(M.fa[(size_t)i], c.ffn_act) *
                              M.fb[(size_t)i];
        });
        std::vector<float> fproj((size_t)R * H);
        linear_fwd(M.fh.data(), p.w.at(b + "w2"), fproj.data(),
                   R, c.inter, H);
        M.x2.resize((size_t)R * H);
        tpu_elementwise((int64_t)M.x2.size(), [&](int64_t i) {
            M.x2[(size_t)i] = M.x1[(size_t)i] + fproj[(size_t)i];
        });
        // ---- shared head ----
        M.hn.resize((size_t)R * H); M.hrms.resize(R);
        rmsnorm_fwd(M.x2.data(), p.w.at(b + "norm_o").d.data(),
                    M.hn.data(), M.hrms.data(), R, H, c.rms_eps);
        M.logits.resize((size_t)R * c.vocab);
        linear_fwd(M.hn.data(), p.w.at("lm_head"), M.logits.data(),
                   R, H, c.vocab);
        o.mtp.push_back(std::move(M));
    }
}

// Backward: dm[d] = dloss/dlogits of module d (already scaled). Folds the
// trunk-hidden contribution into dh_main rows P+t and accumulates the
// shared embed/lm_head + module parameter grads.
static void mtp_bwd(Params& p, const ModelConfig& c,
                    const std::vector<int>& ids, Fwd& o,
                    const std::vector<std::vector<float>>& dm,
                    float* dh_main) {
    const int H = c.hidden, hd = H / c.heads;
    const int Hq = c.heads * hd;
    const int kvh = std::max(1, c.kv_heads), Hkv = kvh * hd;
    const int group = c.heads / kvh;
    const int rd = c.rotary_dim();
    const float scale = 1.0f / std::sqrt((float)hd);
    const int P = o.vision_patches;
    std::vector<float> carry;   // d x2 of module d (for its eh input)
    for (int d = (int)dm.size() - 1; d >= 0; --d) {
        MtpCache& M = o.mtp[(size_t)d];
        const int R = M.rows;
        if (R <= 0 || d >= (int)o.mtp.size()) continue;
        const std::string b = "mtp." + std::to_string(d) + ".";
        // head: logits = lm_head · norm_o(x2)
        std::vector<float> dhn((size_t)R * H, 0.0f);
        linear_bwd(dm[(size_t)d].data(), M.hn.data(), p.w.at("lm_head"),
                   dhn.data(), p.g["lm_head"].d.data(), R, H, c.vocab);
        std::vector<float> dx2((size_t)R * H, 0.0f);
        rmsnorm_bwd(dhn.data(), M.x2.data(),
                    p.w.at(b + "norm_o").d.data(), M.hrms.data(),
                    dx2.data(), p.g[b + "norm_o"].d.data(), R, H);
        // deeper module consumed x2 rows [0, R_{d+1}) as its eh input
        if (!carry.empty())
            for (size_t i = 0; i < carry.size(); ++i) dx2[i] += carry[i];
        carry.clear();
        // ffn: x2 = x1 + w2·(act(fa)⊙fb)
        std::vector<float> dx1 = dx2;                    // residual
        std::vector<float> dfh((size_t)R * c.inter, 0.0f);
        linear_bwd(dx2.data(), M.fh.data(), p.w.at(b + "w2"),
                   dfh.data(), p.g[b + "w2"].d.data(), R, c.inter, H);
        std::vector<float> dfa((size_t)R * c.inter, 0.0f),
                           dfb((size_t)R * c.inter, 0.0f);
        tpu_elementwise((int64_t)M.fh.size(), [&](int64_t i) {
            float a = M.fa[(size_t)i], bb = M.fb[(size_t)i],
                  dd = dfh[(size_t)i];
            dfa[(size_t)i] += dd * bb * gate_act_df(a, c.ffn_act);
            dfb[(size_t)i] += dd * gate_act_f(a, c.ffn_act);
        });
        std::vector<float> dn2((size_t)R * H, 0.0f);
        linear_bwd(dfa.data(), M.n2.data(), p.w.at(b + "w1"),
                   dn2.data(), p.g[b + "w1"].d.data(), R, H, c.inter);
        linear_bwd(dfb.data(), M.n2.data(), p.w.at(b + "w3"),
                   dn2.data(), p.g[b + "w3"].d.data(), R, H, c.inter);
        std::vector<float> dx1n((size_t)R * H, 0.0f);
        rmsnorm_bwd(dn2.data(), M.x1.data(), p.w.at(b + "norm2").d.data(),
                    M.rms2.data(), dx1n.data(), p.g[b + "norm2"].d.data(),
                    R, H);
        tpu_elementwise((int64_t)dx1.size(), [&](int64_t i) {
            dx1[(size_t)i] += dx1n[(size_t)i];
        });
        // attention: x1 = u + wo·attn_out
        std::vector<float> du = dx1;                     // residual to u
        std::vector<float> dao((size_t)R * Hq, 0.0f);
        linear_bwd(dx1.data(), M.attn_out.data(), p.w.at(b + "wo"),
                   dao.data(), p.g[b + "wo"].d.data(), R, Hq, H);
        std::vector<float> dq((size_t)R * Hq, 0.0f),
                           dk((size_t)R * Hkv, 0.0f),
                           dvv((size_t)R * Hkv, 0.0f);
        parallel_for(c.heads, [&](int64_t hb, int64_t he) {
            for (int64_t h = hb; h < he; ++h) {
                int kh2 = (int)h / group;
                std::vector<float> dscore;
                for (int t = 0; t < R; ++t) {
                    const float* pr =
                        M.probs.data() + ((size_t)h * R + t) * R;
                    const float* dao_r =
                        dao.data() + ((size_t)t * c.heads + h) * hd;
                    dscore.assign((size_t)t + 1, 0.0f);
                    for (int s = 0; s <= t; ++s) {
                        const float* vr =
                            M.v.data() + ((size_t)s * kvh + kh2) * hd;
                        dscore[(size_t)s] = tpu_dot(dao_r, vr, hd);
                    }
                    float dsum = 0.0f;
                    for (int s = 0; s <= t; ++s)
                        dsum += dscore[(size_t)s] * pr[s];
                    for (int s = 0; s <= t; ++s)
                        dscore[(size_t)s] =
                            pr[s] * (dscore[(size_t)s] - dsum) * scale;
                    const float* qr =
                        M.q.data() + ((size_t)t * c.heads + h) * hd;
                    float* dqr =
                        dq.data() + ((size_t)t * c.heads + h) * hd;
                    for (int s = 0; s <= t; ++s) {
                        const float* kr =
                            M.k.data() + ((size_t)s * kvh + kh2) * hd;
                        float* dkr =
                            dk.data() + ((size_t)s * kvh + kh2) * hd;
                        tpu_axpy(dqr, dscore[(size_t)s], kr, hd);
                        tpu_axpy(dkr, dscore[(size_t)s], qr, hd);
                        float* dvr =
                            dvv.data() + ((size_t)s * kvh + kh2) * hd;
                        tpu_axpy(dvr, pr[s], dao_r, hd);
                    }
                }
            }
        });
        if (rd < hd) {
            rope_hf_partial(dq.data(), R, c.heads, hd, rd,
                            c.rope_theta, true);
            rope_hf_partial(dk.data(), R, kvh, hd, rd,
                            c.rope_theta, true);
        } else {
            rope(dq.data(), R, c.heads, hd, c.rope_theta, true);
            rope(dk.data(), R, kvh, hd, c.rope_theta, true);
        }
        std::vector<float> dn1((size_t)R * H, 0.0f);
        linear_bwd(dq.data(), M.n1.data(), p.w.at(b + "wq"),
                   dn1.data(), p.g[b + "wq"].d.data(), R, H, Hq);
        linear_bwd(dk.data(), M.n1.data(), p.w.at(b + "wk"),
                   dn1.data(), p.g[b + "wk"].d.data(), R, H, Hkv);
        linear_bwd(dvv.data(), M.n1.data(), p.w.at(b + "wv"),
                   dn1.data(), p.g[b + "wv"].d.data(), R, H, Hkv);
        rmsnorm_bwd(dn1.data(), M.u.data(), p.w.at(b + "norm1").d.data(),
                    M.rms1.data(), du.data(), p.g[b + "norm1"].d.data(),
                    R, H);
        // fusion proj: u = Wp·[ehn|een] → split back into the normed halves
        std::vector<float> dcat((size_t)R * 2 * H, 0.0f);
        linear_bwd(du.data(), M.cat.data(), p.w.at(b + "proj"),
                   dcat.data(), p.g[b + "proj"].d.data(), R, 2 * H, H);
        std::vector<float> deh((size_t)R * H), dee((size_t)R * H);
        for (int t = 0; t < R; ++t) {
            std::copy(dcat.data() + (size_t)t * 2 * H,
                      dcat.data() + (size_t)t * 2 * H + H,
                      deh.data() + (size_t)t * H);
            std::copy(dcat.data() + (size_t)t * 2 * H + H,
                      dcat.data() + (size_t)t * 2 * H + 2 * H,
                      dee.data() + (size_t)t * H);
        }
        std::vector<float> dhprev((size_t)R * H, 0.0f),
                           demb((size_t)R * H, 0.0f);
        rmsnorm_bwd(deh.data(), M.eh_in.data(), p.w.at(b + "eh").d.data(),
                    M.eh_rms.data(), dhprev.data(),
                    p.g[b + "eh"].d.data(), R, H);
        rmsnorm_bwd(dee.data(), M.ee_in.data(), p.w.at(b + "et").d.data(),
                    M.ee_rms.data(), demb.data(),
                    p.g[b + "et"].d.data(), R, H);
        // embed table rows ids[t+d+1]
        float* ge = p.g["embed"].d.data();
        for (int t = 0; t < R; ++t) {
            float* gr = ge + (size_t)ids[t + d + 1] * H;
            const float* dr = demb.data() + (size_t)t * H;
            for (int i = 0; i < H; ++i) gr[i] += dr[i];
        }
        if (d == 0) {
            for (int t = 0; t < R; ++t) {
                float* hr = dh_main + (size_t)(P + t) * H;
                const float* dr = dhprev.data() + (size_t)t * H;
                for (int i = 0; i < H; ++i) hr[i] += dr[i];
            }
        } else {
            carry = std::move(dhprev);
        }
    }
}

// MTP aux loss for the supervised legs: per-depth CE on ids[t+d+2],
// weighted by mtp_loss_w; fills aligned dlogits for mtp_bwd (empty
// vectors mark skipped/empty modules).
static float mtp_aux_loss(const ModelConfig& c, const std::vector<int>& ids,
                          const Fwd& fw,
                          std::vector<std::vector<float>>& dmtp) {
    dmtp.assign(fw.mtp.size(), {});
    if (c.mtp_depth <= 0) return 0.0f;
    float lsum = 0.0f;
    for (int d = 0; d < (int)fw.mtp.size(); ++d) {
        const MtpCache& M = fw.mtp[(size_t)d];
        if (M.rows <= 0) continue;
        std::vector<int> ml((size_t)M.rows);
        for (int t = 0; t < M.rows; ++t) ml[(size_t)t] = ids[t + d + 2];
        lsum += c.mtp_loss_w *
                ce_loss(M.logits, ml, M.rows, c.vocab, dmtp[(size_t)d]);
        for (auto& x : dmtp[(size_t)d]) x *= c.mtp_loss_w;
    }
    return lsum;
}

// ------------------------------------------------------------- mtpcheck --
//
// Executable contract for the v29 Qwen3.8-Max MTP head + router z-loss:
//   1. shape/finiteness: depth d emits R=PT-2-d finite logit rows; the
//      stack of depth 2 chains module-1 hidden into module 0... (d indexes
//      deeper modules consuming shallower outputs);
//   2. causality: corrupting text token j leaves MTP rows [0, j-1)
//      bitwise identical (trunk hidden is causal, the t+1 embed input and
//      the block's causal attention seal the past) while row j-1 moves;
//   3. fusion liveness: MTP logits differ from the same positions of the
//      main logits (the extra embed input + block do real work);
//   4. backward: zeroed main-CE dlogits + MTP dlogits produce finite,
//      non-zero grads on mtp.* params, embed rows and the trunk — verified
//      by finite differences on a sample of elements;
//   5. router z-loss: with moe_z_loss_weight>0 the forward accumulates
//      w·mean(lse²) and the gate weight receives the 2·lse·p/T gradient.
static int mtpcheck() {
    int failures = 0;
    auto fail = [&](const char* what) {
        ++failures;
        std::printf("  FAIL %s\n", what);
    };
    ModelConfig c;
    c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 2;
    c.heads = 2; c.kv_heads = 1; c.max_pos = 64;
    c.full_attention_interval = 2;
    c.attn_output_gate = true; c.qk_norm = true; c.partial_rotary = 0.5f;
    c.lin_key_heads = 1; c.lin_key_dim = 32;
    c.lin_value_heads = 2; c.lin_value_dim = 32; c.lin_conv_kernel = 4;
    c.moe_experts = 4; c.moe_top_k = 2; c.moe_layer_interval = 1;
    c.moe_expert_inter = 24; c.moe_shared_experts = 1;
    c.moe_shared_inter = 24; c.shared_expert_gate = true;
    c.moe_zloss_w = 0.5f;
    c.mtp_depth = 2; c.mtp_loss_w = 0.3f;
    Params p;
    init_params(p, c, 41);
    std::vector<int> ids = {3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41};
    const int PT = (int)ids.size();
    Fwd f0;
    fwd(p, c, ids, f0);
    // ---- 1. shape + finiteness + chaining -------------------------------
    if (f0.mtp.size() != 2) fail("depth-2 stack not built");
    for (int d = 0; d < 2 && d < (int)f0.mtp.size(); ++d) {
        const MtpCache& M = f0.mtp[(size_t)d];
        if (M.rows != PT - 2 - d) fail("row count");
        if ((int)M.logits.size() != M.rows * c.vocab) fail("logit shape");
        for (float x : M.logits) if (!std::isfinite(x)) fail("nonfinite");
    }
    // z-loss accumulated once per MoE layer
    if (!(f0.moe_zloss > 0.0f) || !std::isfinite(f0.moe_zloss))
        fail("z-loss absent");
    // ---- 2. causality ----------------------------------------------------
    for (int j : {PT - 1, PT / 2}) {
        std::vector<int> ids2 = ids;
        ids2[j] = (ids2[j] + 13) % c.vocab;
        if (ids2[j] == ids[j]) ids2[j] = (ids2[j] + 1) % c.vocab;
        Fwd f1;
        fwd(p, c, ids2, f1);
        const MtpCache& M0 = f0.mtp[0], &M1 = f1.mtp[0];
        int sealed = std::min(j - 1, M0.rows);
        if (std::memcmp(M0.logits.data(), M1.logits.data(),
                        (size_t)sealed * c.vocab * sizeof(float)) != 0)
            fail("causal prefix broken");
        if (j - 1 < M0.rows &&
            std::memcmp(M0.logits.data() + (size_t)(j - 1) * c.vocab,
                        M1.logits.data() + (size_t)(j - 1) * c.vocab,
                        (size_t)c.vocab * sizeof(float)) == 0)
            fail("corrupted input row did not move");
    }
    // ---- 3. fusion liveness ----------------------------------------------
    {
        const MtpCache& M0 = f0.mtp[0];
        bool diff = false;
        for (int t = 0; t < M0.rows; ++t) {
            const float* a = M0.logits.data() + (size_t)t * c.vocab;
            const float* bb = f0.logits.data() + (size_t)t * c.vocab;
            double md = 0.0;
            for (int i = 0; i < c.vocab; ++i)
                md = std::max(md, (double)std::fabs(a[i] - bb[i]));
            if (md > 1e-5) diff = true;
        }
        if (!diff) fail("mtp logits identical to trunk logits");
    }
    // ---- 4/5. backward + finite difference -------------------------------
    // Loss under test: mtp CE (depth-0) + trunk CE + aux + z-loss, matching
    // the job's composition.
    auto loss_of = [&](Params& pp, const std::vector<int>& idv) {
        Fwd f;
        fwd(pp, c, idv, f);
        std::vector<int> lab = idv;   // next-token shift (shift_labels eq.)
        std::rotate(lab.begin(), lab.begin() + 1, lab.end());
        lab.back() = -100;
        std::vector<float> dl;
        double l = ce_loss(f.logits, lab, PT, c.vocab, dl) + f.moe_aux +
                   f.moe_zloss;
        const MtpCache& M = f.mtp[0];
        std::vector<int> ml((size_t)M.rows);
        for (int t = 0; t < M.rows; ++t) ml[(size_t)t] = idv[t + 2];
        std::vector<float> dml;
        l += c.mtp_loss_w * ce_loss(M.logits, ml, M.rows, c.vocab, dml);
        return l;
    };
    {
        for (auto& n : p.order) std::fill(p.g[n].d.begin(), p.g[n].d.end(), 0.0f);
        Fwd f;
        fwd(p, c, ids, f);
        std::vector<int> lab = ids;   // next-token shift (shift_labels eq.)
        std::rotate(lab.begin(), lab.begin() + 1, lab.end());
        lab.back() = -100;
        std::vector<float> dl;
        (void)ce_loss(f.logits, lab, PT, c.vocab, dl);
        std::vector<std::vector<float>> dmtp(f.mtp.size());
        for (int d = 0; d < (int)f.mtp.size(); ++d) {
            const MtpCache& M = f.mtp[(size_t)d];
            if (M.rows <= 0) continue;
            std::vector<int> ml((size_t)M.rows);
            for (int t = 0; t < M.rows; ++t) ml[(size_t)t] = ids[t + d + 2];
            ce_loss(M.logits, ml, M.rows, c.vocab, dmtp[(size_t)d]);
            for (auto& x : dmtp[(size_t)d]) x *= c.mtp_loss_w;
        }
        bwd(p, c, ids, f, dl, 1.0f, nullptr, &dmtp);
        auto gr_norm = [&](const char* n) {
            double s = 0.0;
            for (float x : p.g[n].d) s += (double)x * x;
            return std::sqrt(s);
        };
        if (!(gr_norm("mtp.0.proj") > 0.0)) fail("mtp proj grad");
        if (!(gr_norm("mtp.1.wq") > 0.0)) fail("mtp depth-1 chain grad");
        if (!(gr_norm("layers.0.gate") > 0.0)) fail("gate grad missing");
        for (float x : p.g["mtp.0.proj"].d)
            if (!std::isfinite(x)) fail("mtp proj grad nonfinite");
        // embed grad must hit a token consumed via ee (ids[1..PT-2])
        double esum = 0.0;
        for (int i = 0; i < c.hidden; ++i)
            esum += std::fabs(
                p.g["embed"].d[(size_t)ids[1] * c.hidden + i]);
        if (!(esum > 0.0)) fail("embed grad via ee missing");
        // finite differences on a few elements
        const char* probes[] = {"mtp.0.proj", "mtp.0.norm_o",
                                "embed", "layers.0.gate"};
        const int ncheck = 4;
        const float eps = 1e-3f;
        for (const char* n : probes) {
            Tensor& w = p.w[n];
            int stride = (int)w.d.size() / (ncheck + 1);
            for (int k = 1; k <= ncheck; ++k) {
                size_t idx = (size_t)k * stride;
                float orig = w.d[idx];
                w.d[idx] = orig + eps;
                double lp = loss_of(p, ids);
                w.d[idx] = orig - eps;
                double lm = loss_of(p, ids);
                w.d[idx] = orig;
                double num = (lp - lm) / (2.0 * eps);
                double ana = p.g[n].d[idx];
                double tol = 1e-3 * std::max(1.0, std::fabs(ana));
                if (std::fabs(num - ana) > tol) {
                    ++failures;
                    std::printf(
                        "  FAIL fdiff %s[%zu] ana=%.6f num=%.6f\n",
                        n, idx, ana, num);
                }
            }
        }
    }
    bool ok = failures == 0;
    std::printf("mtpcheck: Qwen3.8-Max MTP+zloss failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

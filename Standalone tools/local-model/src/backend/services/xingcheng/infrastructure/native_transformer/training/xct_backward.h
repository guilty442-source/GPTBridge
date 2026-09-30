// xct_backward.h — B94 fragment of xingcheng_trainer.cpp (backward + losses).
// Included once by xingcheng_trainer.cpp inside namespace xct.
#pragma once

// -------------------------------------------------------------- backward --

// defined in xct_mtp.h (included after this header)
static void mtp_bwd(Params& p, const ModelConfig& c,
                    const std::vector<int>& ids, Fwd& o,
                    const std::vector<std::vector<float>>& dmtp,
                    float* dh_main);

static void bwd(Params& p, const ModelConfig& c, const std::vector<int>& ids,
                Fwd& o, const std::vector<float>& dlogits, float aux_scale,
                const std::vector<float>* vision = nullptr,
                const std::vector<std::vector<float>>* dmtp = nullptr) {
    if (c.is_gemma4()) {
        (void)aux_scale;
        (void)vision;
        bwd_g4(p, c, ids, o, dlogits);
        return;
    }
    const int PT = (int)ids.size();
    // Vision prefix rows (P) were prepended by fwd; caches/logits carry T.
    const int P = o.vision_patches;
    if (P < 0 || (P == 0) != (vision == nullptr))
        throw "vision: fwd/bwd prefix mismatch";
    if (P > 0 && ((int)o.vision_in.size() != P * c.vision_patch_dim ||
                  (int)vision->size() != P * c.vision_patch_dim))
        throw "vision: fwd/bwd prefix mismatch";
    const int T = P + PT;
    const int H = c.hidden, hd = H / c.heads;
    const int Hq = c.heads * hd;
    // Gemma final logit softcap: y = cap*tanh(z/cap) — chain the incoming
    // dlogits through dz = dy * (1 - (y/cap)^2) (recoverable from the
    // capped logits themselves).
    std::vector<float> dlog;
    const float* dlp = dlogits.data();
    if (c.final_logit_softcap > 0.0f) {
        dlog = dlogits;
        const float inv = 1.0f / c.final_logit_softcap;
        tpu_elementwise((int64_t)dlog.size(), [&](int64_t i) {
            float yc = o.logits[(size_t)i] * inv;
            dlog[(size_t)i] *= 1.0f - yc * yc;
        });
        dlp = dlog.data();
    }
    std::vector<float> dh((size_t)T * H, 0.0f);
    linear_bwd(dlp, o.hidden.data(), p.w.at("lm_head"),
               dh.data(), p.g["lm_head"].d.data(), T, H, c.vocab);
    // v29 MTP stack: folds its dh contribution onto the trunk hidden rows
    // (post-final-norm input) before the norm_f backward, and accumulates
    // the shared embed/lm_head + mtp.* parameter grads.
    if (dmtp != nullptr && !dmtp->empty() && c.mtp_depth > 0)
        mtp_bwd(p, c, ids, o, *dmtp, dh.data());
    std::vector<float> dx_fin((size_t)T * H, 0.0f);
    rmsnorm_bwd(dh.data(), o.x_fin.data(), p.w.at("norm_f").d.data(),
                o.rmsf.data(), dx_fin.data(), p.g["norm_f"].d.data(), T, H);
    std::vector<float> dx = dx_fin;
    for (int l = c.layers - 1; l >= 0; --l) {
        LayerCache& L = o.layers[l];
        bool moe = c.moe_experts > 0 && (l % c.moe_layer_interval == 0);
        // residual split: dx flows to ffn path (through dproj) and to x_res.
        std::vector<float> dx_res = dx;                   // residual branch
        std::vector<float> dproj;
        if (c.post_ffw_norm) {
            // Gemma sandwich: x_out = x_res + rmsnorm(ffn_proj) — route dx
            // through the post norm backward to reach the raw FFN output.
            dproj.assign((size_t)T * H, 0.0f);
            rmsnorm_bwd(dx.data(), L.ffn_proj.data(),
                        p.w.at(ln(l, "norm_ffw_out")).d.data(),
                        L.post_ffn_rms.data(), dproj.data(),
                        p.g[ln(l, "norm_ffw_out")].d.data(), T, H);
        } else {
            dproj = dx;                                 // [T,H]
        }
        std::vector<float> dn2((size_t)T * H, 0.0f);
        if (!moe) {
            std::vector<float> dfh((size_t)T * c.inter, 0.0f);
            linear_bwd(dproj.data(), L.fh.data(), p.w.at(ln(l, "w2")),
                       dfh.data(), p.g[ln(l, "w2")].d.data(), T, c.inter, H);
            std::vector<float> dfa((size_t)T * c.inter, 0.0f), dfb((size_t)T * c.inter, 0.0f);
            tpu_elementwise((int64_t)L.fh.size(), [&](int64_t i) {
                float a = L.fa[(size_t)i], b = L.fb[(size_t)i], d = dfh[(size_t)i];
                dfa[(size_t)i] += d * b * gate_act_df(a, c.ffn_act);
                dfb[(size_t)i] += d * gate_act_f(a, c.ffn_act);
            });
            linear_bwd(dfa.data(), L.n2.data(), p.w.at(ln(l, "w1")),
                       dn2.data(), p.g[ln(l, "w1")].d.data(), T, H, c.inter);
            linear_bwd(dfb.data(), L.n2.data(), p.w.at(ln(l, "w3")),
                       dn2.data(), p.g[ln(l, "w3")].d.data(), T, H, c.inter);
        } else {
            const int E = c.moe_experts, K = c.moe_top_k;
            const int EI = c.expert_inter();
            const int SI = c.shared_inter();
            std::vector<float> dgl((size_t)T * E, 0.0f);
            // Load-balance aux gradient (Switch Transformer): the layer
            // contributes aux_scale·moe_aux_w·E·Σ_i f_i·P_i where
            // P_i = mean_t gp[t,i] and f_i (assignment share) is a
            // piecewise-constant routing statistic — so
            // ∂L/∂gp[t,i] += aux_scale·moe_aux_w·E·f_i/T, added to dgl
            // before the softmax backward below.
            std::vector<float> moe_f((size_t)E, 0.0f);
            if (aux_scale != 0.0f && c.moe_aux_w != 0.0f) {
                for (size_t a = 0; a < L.moe_idx.size(); ++a)
                    moe_f[(size_t)L.moe_idx[a]] += 1.0f;
                for (int e = 0; e < E; ++e) moe_f[e] /= (float)(T * K);
            }
            const float lb_step = aux_scale * c.moe_aux_w * (float)E /
                                  (float)std::max(1, T);
            for (int t = 0; t < T; ++t) {
                const float* xr = L.n2.data() + (size_t)t * H;
                float* dxr = dn2.data() + (size_t)t * H;
                // recompute wsum for router-weight grads
                const float* gp = L.gate_probs.data() + (size_t)t * E;
                float wsum = 0.0f;
                for (int s = 0; s < K; ++s) wsum += gp[L.moe_idx[(size_t)t * K + s]];
                for (int s = 0; s < K; ++s) {
                    int e = L.moe_idx[(size_t)t * K + s];
                    float wgt = L.moe_w[(size_t)t * K + s];
                    std::string b = ln(l, "experts.") + std::to_string(e) + ".";
                    const std::vector<float>& fh = L.mfh[(size_t)t * K + s];
                    const std::vector<float>& fa = L.mfa[(size_t)t * K + s];
                    const std::vector<float>& fb = L.mfb[(size_t)t * K + s];
                    const float* dpr = dproj.data() + (size_t)t * H;
                    std::vector<float> deo(H);
                    for (int i = 0; i < H; ++i) deo[i] = dpr[i] * wgt;
                    std::vector<float> dfh((size_t)EI, 0.0f);
                    linear_bwd(deo.data(), fh.data(), p.w.at(b + "w2"),
                               dfh.data(), p.g[b + "w2"].d.data(), 1, EI, H);
                    std::vector<float> dfa((size_t)EI, 0.0f), dfb((size_t)EI, 0.0f);
                    for (int i = 0; i < EI; ++i) {
                        float a = fa[i], bb = fb[i], d = dfh[i];
                        dfa[i] += d * bb * gate_act_df(a, c.ffn_act);
                        dfb[i] += d * gate_act_f(a, c.ffn_act);
                    }
                    linear_bwd(dfa.data(), xr, p.w.at(b + "w1"),
                               dxr, p.g[b + "w1"].d.data(), 1, H, EI);
                    linear_bwd(dfb.data(), xr, p.w.at(b + "w3"),
                               dxr, p.g[b + "w3"].d.data(), 1, H, EI);
                    // router weight grad: d(wgt * eo)/d gp[e]
                    float dot = 0.0f;
                    for (int i = 0; i < H; ++i) {
                        // eo = W2 @ fh recomputed cheaply via fwd cache
                    }
                    // dout/d(gp[e]) = eo/wsum - sum_s(wgt_s*eo_s)*gp[e]/wsum^2 + aux
                    // compute eo once:
                    std::vector<float> eo(H);
                    linear_fwd(fh.data(), p.w.at(b + "w2"), eo.data(), 1, EI, H);
                    for (int i = 0; i < H; ++i) dot += dpr[i] * eo[i];
                    float dlogit = dot / wsum;            // contribution via this slot
                    dgl[(size_t)t * E + e] += dlogit;
                }
                for (int e = 0; e < E; ++e)
                    dgl[(size_t)t * E + e] += lb_step * moe_f[(size_t)e];
                // Router backward (aux + weighted path share the logit
                // grads approximated by direct slot contribution):
                // softmax mode uses the full Jacobian p⊙(din−⟨p,din⟩);
                // v28 sigmoid mode is diagonal — scores are per-expert
                // independent, dσ/dz = s(1−s).
                float* gpl = L.gate_probs.data() + (size_t)t * E;
                const float* dglr = dgl.data() + (size_t)t * E;
                std::vector<float> din(E);
                if (c.moe_router_sigmoid) {
                    for (int e = 0; e < E; ++e)
                        din[e] = gpl[e] * (1.0f - gpl[e]) * dglr[e];
                } else {
                    float dotp = 0.0f;
                    for (int e = 0; e < E; ++e) dotp += dglr[e] * gpl[e];
                    for (int e = 0; e < E; ++e) din[e] = gpl[e] * (dglr[e] - dotp);
                }
                // Router z-loss (B133): z = aux_scale·w·mean_t lse_t² —
                // dz/dlogit_e = 2·w·lse_t·softmax_e/T lands post-Jacobian on
                // the raw gate logits.
                if (aux_scale != 0.0f && c.moe_zloss_w != 0.0f) {
                    const float* glr = L.gate_logits.data() + (size_t)t * E;
                    float mx = *std::max_element(glr, glr + E), zs = 0.0f;
                    for (int e = 0; e < E; ++e) zs += std::exp(glr[e] - mx);
                    float lse = mx + std::log(zs);
                    float cz = aux_scale * c.moe_zloss_w * 2.0f * lse /
                               (float)std::max(1, T);
                    for (int e = 0; e < E; ++e) {
                        float pm = c.moe_router_sigmoid
                                       ? std::exp(glr[e] - mx) / zs
                                       : gpl[e];
                        din[e] += cz * pm;
                    }
                }
                linear_bwd(din.data(), xr, p.w.at(ln(l, "gate")),
                           dxr, p.g[ln(l, "gate")].d.data(), 1, H, E);
                (void)aux_scale;
            }
            // Shared experts (v26): always-on SwiGLU backward — the shared
            // output adds into proj with weight 1.0, so dproj flows through
            // each shared FFN identically to the dense FFN backward.
            // v27: when shared_gate is active the effective shared output is
            // sigmoid(Wx) ⊙ shared — dproj must be scaled by the gate and the
            // gate itself gets a logit gradient.
            const bool sgated = !L.shared_gate_sig.empty();
            std::vector<float> dsg((size_t)T, 0.0f);
            for (int se = 0; se < c.moe_shared_experts; ++se) {
                std::string b = ln(l, "shared.") + std::to_string(se) + ".";
                const auto& sfa = L.sfa[(size_t)se];
                const auto& sfb = L.sfb[(size_t)se];
                const auto& sfh = L.sfh[(size_t)se];
                std::vector<float> dsh((size_t)T * SI, 0.0f);
                std::vector<float> dsg_in;
                const float* dproj_use = dproj.data();
                if (sgated) {
                    dsg_in.assign((size_t)T * H, 0.0f);
                    parallel_for(T, [&](int64_t b, int64_t e) {
                        for (int64_t t = b; t < e; ++t) {
                            float g = L.shared_gate_sig[(size_t)t];
                            tpu_scale_copy(dsg_in.data() + (size_t)t * H,
                                           dproj.data() + (size_t)t * H,
                                           g, H);
                        }
                    });
                    dproj_use = dsg_in.data();
                }
                linear_bwd(dproj_use, sfh.data(), p.w.at(b + "w2"),
                           dsh.data(), p.g[b + "w2"].d.data(), T, SI, H);
                std::vector<float> dsa((size_t)T * SI, 0.0f);
                std::vector<float> dsb((size_t)T * SI, 0.0f);
                tpu_elementwise((int64_t)sfh.size(), [&](int64_t i) {
                    float a = sfa[(size_t)i], bb = sfb[(size_t)i], d = dsh[(size_t)i];
                    dsa[(size_t)i] += d * bb * gate_act_df(a, c.ffn_act);
                    dsb[(size_t)i] += d * gate_act_f(a, c.ffn_act);
                });
                linear_bwd(dsa.data(), L.n2.data(), p.w.at(b + "w1"),
                           dn2.data(), p.g[b + "w1"].d.data(), T, H, SI);
                linear_bwd(dsb.data(), L.n2.data(), p.w.at(b + "w3"),
                           dn2.data(), p.g[b + "w3"].d.data(), T, H, SI);
                if (sgated) {
                    // gate grad: dsg[t] = Σ_i dproj[t,i] · so[t,i]; need so —
                    // recompute so = w2 @ sfh (cheap relative to the FFN bwd).
                    std::vector<float> so((size_t)T * H);
                    linear_fwd(sfh.data(), p.w.at(b + "w2"), so.data(),
                               T, SI, H);
                    for (int t = 0; t < T; ++t)
                        for (int i = 0; i < H; ++i)
                            dsg[(size_t)t] +=
                                dproj[(size_t)t * H + i] * so[(size_t)t * H + i];
                }
            }
            if (sgated) {
                std::vector<float> dsg_logit((size_t)T);
                for (int t = 0; t < T; ++t) {
                    float s = L.shared_gate_sig[(size_t)t];
                    dsg_logit[(size_t)t] = dsg[(size_t)t] * s * (1.0f - s);
                }
                linear_bwd(dsg_logit.data(), L.n2.data(),
                           p.w.at(ln(l, "shared_gate")), dn2.data(),
                           p.g[ln(l, "shared_gate")].d.data(), T, H, 1);
            }
        }
        // norm2 backward: dn2 -> dx_res (accumulate into residual branch)
        std::vector<float> dxres2((size_t)T * H, 0.0f);
        rmsnorm_bwd(dn2.data(), L.x_res.data(), p.w.at(ln(l, "norm2")).d.data(),
                    L.rms2.data(), dxres2.data(), p.g[ln(l, "norm2")].d.data(), T, H);
        std::vector<float> dpre((size_t)T * H);
        tpu_elementwise((int64_t)dpre.size(), [&](int64_t i) {
            dpre[(size_t)i] = dx_res[(size_t)i] + dxres2[(size_t)i];
        });
        // attention block: dpre splits into attn path + layer-input residual
        std::vector<float> dproj_attn;
        if (!L.post_attn.empty()) {
            // Gemma sandwich: x_res = x + rmsnorm(attn_proj) — route dpre
            // through the post norm backward to reach the raw proj output.
            dproj_attn.assign((size_t)T * H, 0.0f);
            rmsnorm_bwd(dpre.data(), L.attn_proj.data(),
                        p.w.at(ln(l, "norm_attn_out")).d.data(),
                        L.post_attn_rms.data(), dproj_attn.data(),
                        p.g[ln(l, "norm_attn_out")].d.data(), T, H);
        } else {
            dproj_attn = dpre;                          // through output proj
        }
        std::vector<float> dx_attn_in = dpre;             // residual to x_in
        std::vector<float> dn1((size_t)T * H, 0.0f);
        if (c.is_linear(l)) {
            // -------- gated deltanet backward --------
            const int kd = c.lin_key_dim, vd = c.lin_value_dim;
            const int kh = c.lin_key_heads, vh = c.lin_value_heads;
            const int ratio = vh / kh;
            const int key_dim = kh * kd, val_dim = vh * vd;
            const int conv_dim = key_dim * 2 + val_dim;
            const int group_sz = 2 * kd + vd * ratio;
            const size_t ssz = (size_t)kd * vd;
            const std::string lb = ln(l, "lin.");
            // out_proj
            std::vector<float> don((size_t)T * val_dim, 0.0f);
            linear_bwd(dproj_attn.data(), L.lin_on.data(),
                       p.w.at(lb + "out_proj"), don.data(),
                       p.g[lb + "out_proj"].d.data(), T, val_dim, H);
            // silu(z) gate then gated-RMSNorm backward
            std::vector<float> donorm((size_t)T * val_dim),
                               dz((size_t)T * val_dim);
            tpu_elementwise((int64_t)don.size(), [&](int64_t i) {
                float z = L.lin_z[(size_t)i];
                float sg = sigmoid_f(z);
                donorm[(size_t)i] = don[(size_t)i] * z * sg;
                dz[(size_t)i] = don[(size_t)i] * L.lin_onorm[(size_t)i] * sg * (1.0f + z * (1.0f - sg));
            });
            std::vector<float> do_((size_t)T * val_dim, 0.0f);
            for (int t = 0; t < T; ++t)
                for (int h = 0; h < vh; ++h) {
                    const size_t off = ((size_t)t * vh + h) * (size_t)vd;
                    rmsnorm_bwd(donorm.data() + off, L.lin_o.data() + off,
                                p.w.at(lb + "norm").d.data(),
                                L.lin_orms.data() + (size_t)t * vh + h,
                                do_.data() + off,
                                p.g[lb + "norm"].d.data(), 1, vd);
                }
            // (kept serial: rmsnorm_bwd folds a shared dw row into
            // p.g[lb+"norm"] — partitioning it would reorder the fold.)
            // recurrent scan backward (reverse-time). TPU lanes: heads are
            // disjoint lanes — dS carry, dqn/dkn/dv/da_raw/db_raw and the
            // A_log/dt_bias slots are all indexed by h.
            std::vector<float> dqn((size_t)T * vh * kd, 0.0f),
                               dkn((size_t)T * vh * kd, 0.0f),
                               dv((size_t)T * val_dim, 0.0f),
                               da_raw((size_t)T * vh, 0.0f),
                               db_raw((size_t)T * vh, 0.0f);
            float* dA_log = p.g[lb + "A_log"].d.data();
            float* ddt_bias = p.g[lb + "dt_bias"].d.data();
            const float* A_log = p.w.at(lb + "A_log").d.data();
            parallel_for(vh, [&](int64_t hb, int64_t he) {
            for (int64_t h = hb; h < he; ++h) {
                std::vector<float> dS(ssz, 0.0f);  // carry: dL/dS_t
                std::vector<float> Sd(ssz), u(vd), kvm(vd), du(vd), dkv(vd);
                for (int t = T - 1; t >= 0; --t) {
                    const float* S_t = L.lin_S.data() + ((size_t)(t + 1) * vh + h) * ssz;
                    const float* S_prev = L.lin_S.data() + ((size_t)t * vh + h) * ssz;
                    float dec = L.lin_decay[(size_t)t * vh + h];
                    const float* kr = L.lin_kn.data() + ((size_t)t * vh + h) * kd;
                    const float* vr = L.lin_v.data() + ((size_t)t * vh + h) * vd;
                    const float* qr = L.lin_qn.data() + ((size_t)t * vh + h) * kd;
                    const float* dor = do_.data() + ((size_t)t * vh + h) * vd;
                    float bt = sigmoid_f(L.lin_b_raw[(size_t)t * vh + h]);
                    // S̃ = dec · S_{t-1};  u = β(v − S̃ᵀk) recomputed
                    tpu_scale_copy(Sd.data(), S_prev, dec, (int64_t)ssz);
                    std::fill(kvm.begin(), kvm.end(), 0.0f);
                    for (int d = 0; d < kd; ++d)
                        tpu_axpy(kvm.data(), kr[d], Sd.data() + (size_t)d * vd, vd);
                    for (int i = 0; i < vd; ++i) u[i] = (vr[i] - kvm[i]) * bt;
                    // o_t = S_tᵀq_t → D += q ⊗ do_t ; dq_t = S_t·do_t
                    float* dqr = dqn.data() + ((size_t)t * vh + h) * kd;
                    for (int d = 0; d < kd; ++d) {
                        dqr[d] += tpu_dot(S_t + (size_t)d * vd, dor, vd);
                        tpu_axpy(dS.data() + (size_t)d * vd, qr[d], dor, vd);
                    }
                    // S_t = S̃ + k⊗u → du = Dᵀk ; dk += D·u ; dS̃ = D + k⊗dkv
                    std::fill(du.begin(), du.end(), 0.0f);
                    for (int d = 0; d < kd; ++d)
                        tpu_axpy(du.data(), kr[d], dS.data() + (size_t)d * vd, vd);
                    float* dkr = dkn.data() + ((size_t)t * vh + h) * kd;
                    for (int d = 0; d < kd; ++d)
                        dkr[d] += tpu_dot(dS.data() + (size_t)d * vd, u.data(), vd);
                    // u = β(v − kv_mem)
                    float dbt = 0.0f;
                    for (int i = 0; i < vd; ++i) {
                        float vmkv = vr[i] - kvm[i];
                        dv[(size_t)((size_t)t * vh + h) * vd + i] += bt * du[i];
                        dbt += vmkv * du[i];
                    }
                    db_raw[(size_t)t * vh + h] += dbt * bt * (1.0f - bt);
                    // kv_mem = S̃ᵀk → dS̃ += k⊗dkv ; dk += S̃·dkv
                    for (int i = 0; i < vd; ++i) dkv[i] = -bt * du[i];
                    for (int d = 0; d < kd; ++d) {
                        tpu_axpy(dS.data() + (size_t)d * vd, kr[d], dkv.data(), vd);
                        dkr[d] += tpu_dot(Sd.data() + (size_t)d * vd, dkv.data(), vd);
                    }
                    // S̃ = dec·S_{t-1}: dS_{t-1} = dec·dS̃ ; dg = Σ dS̃⊙S̃
                    float dg = tpu_dot(dS.data(), Sd.data(), (int64_t)ssz);
                    tpu_scale(dS.data(), dec, (int64_t)ssz);
                    // g = −e^{A_log}·softplus(a_raw): g_t = ln(dec)
                    float g_t = std::log(dec);
                    float eA = std::exp(A_log[h]);
                    float ar = L.lin_a_raw[(size_t)t * vh + h];
                    dA_log[h] += dg * g_t;
                    float dar = dg * (-eA) * sigmoid_f(ar);
                    da_raw[(size_t)t * vh + h] += dar;
                    ddt_bias[h] += dar;
                }
            }
            });
            // fold v-head grads back to k-heads (repeat_interleave inverse)
            const float qscale = 1.0f / std::sqrt((float)kd);
            std::vector<float> dqk_raw((size_t)T * kh * kd, 0.0f),
                               dkk_raw((size_t)T * kh * kd, 0.0f),
                               dconv_v((size_t)T * val_dim);
            // fold v-head grads back to k-heads — lane per t keeps the
            // r-order fold identical; (t,g) slots stay single-lane.
            parallel_for(T, [&](int64_t b, int64_t e) {
            for (int64_t t = b; t < e; ++t)
                for (int h = 0; h < vh; ++h) {
                    int g = h / ratio;
                    for (int d = 0; d < kd; ++d) {
                        dqk_raw[(size_t)(t * kh + g) * kd + d] +=
                            dqn[((size_t)t * vh + h) * kd + d];
                        dkk_raw[(size_t)(t * kh + g) * kd + d] +=
                            dkn[((size_t)t * vh + h) * kd + d];
                    }
                }
            });
            // undo q scale then l2norm backward on q and k
            tpu_scale(dqk_raw.data(), qscale, (int64_t)dqk_raw.size());
            std::vector<float> dq_pre((size_t)T * kh * kd),
                               dk_pre((size_t)T * kh * kd);
            {
                // l2norm_bwd expects per-row normed input + norms — our
                // caches are v-head-expanded; take the first replica per
                // group (normed values are identical across replicas).
                std::vector<float> qn_kh((size_t)T * kh * kd),
                                   kn_kh((size_t)T * kh * kd),
                                   qr_kh((size_t)T * kh), kr_kh((size_t)T * kh);
                for (int t = 0; t < T; ++t)
                    for (int g = 0; g < kh; ++g) {
                        int h = g * ratio;
                        std::copy(L.lin_qn.begin() + ((size_t)t * vh + h) * kd,
                                  L.lin_qn.begin() + ((size_t)t * vh + h) * kd + kd,
                                  qn_kh.begin() + ((size_t)t * kh + g) * kd);
                        std::copy(L.lin_kn.begin() + ((size_t)t * vh + h) * kd,
                                  L.lin_kn.begin() + ((size_t)t * vh + h) * kd + kd,
                                  kn_kh.begin() + ((size_t)t * kh + g) * kd);
                        qr_kh[(size_t)t * kh + g] =
                            L.lin_qrms[(size_t)t * vh + h];
                        kr_kh[(size_t)t * kh + g] =
                            L.lin_krms[(size_t)t * vh + h];
                    }
                // qn is l2normed then scaled — undo scale for l2norm_bwd
                for (auto& x : qn_kh) x /= qscale;
                l2norm_bwd(dqk_raw.data(), qn_kh.data(), qr_kh.data(),
                           dq_pre.data(), T * kh, kd);
                l2norm_bwd(dkk_raw.data(), kn_kh.data(), kr_kh.data(),
                           dk_pre.data(), T * kh, kd);
            }
            dconv_v = std::move(dv);
            // conv output grad: [T, q_flat|k_flat|v_flat]
            std::vector<float> dconv_out((size_t)T * conv_dim, 0.0f);
            for (int t = 0; t < T; ++t) {
                float* dr = dconv_out.data() + (size_t)t * conv_dim;
                std::copy(dq_pre.begin() + (size_t)t * key_dim,
                          dq_pre.begin() + (size_t)(t + 1) * key_dim, dr);
                std::copy(dk_pre.begin() + (size_t)t * key_dim,
                          dk_pre.begin() + (size_t)(t + 1) * key_dim,
                          dr + key_dim);
                std::copy(dconv_v.begin() + (size_t)t * val_dim,
                          dconv_v.begin() + (size_t)(t + 1) * val_dim,
                          dr + key_dim * 2);
            }
            std::vector<float> dconv_in((size_t)T * conv_dim, 0.0f);
            conv1d_causal_bwd(dconv_out.data(), L.lin_conv_pre.data(),
                              L.lin_conv_in.data(),
                              p.w.at(lb + "conv1d").d.data(), dconv_in.data(),
                              p.g[lb + "conv1d"].d.data(), T, conv_dim,
                              c.lin_conv_kernel);
            // repack flat conv-in grads → per-k-head grouped qkvz layout
            std::vector<float> dqkvz((size_t)T * kh * group_sz, 0.0f);
            for (int t = 0; t < T; ++t) {
                const float* src = dconv_in.data() + (size_t)t * conv_dim;
                float* dst = dqkvz.data() + (size_t)t * kh * group_sz;
                for (int g = 0; g < kh; ++g) {
                    float* gr = dst + (size_t)g * group_sz;
                    std::copy(src + (size_t)g * kd, src + (size_t)(g + 1) * kd, gr);
                    std::copy(src + key_dim + (size_t)g * kd,
                              src + key_dim + (size_t)(g + 1) * kd, gr + kd);
                    std::copy(src + key_dim * 2 + (size_t)g * (vd * ratio),
                              src + key_dim * 2 + (size_t)(g + 1) * (vd * ratio),
                              gr + 2 * kd);
                }
            }
            linear_bwd(dqkvz.data(), L.n1.data(), p.w.at(lb + "in_proj_qkv"),
                       dn1.data(), p.g[lb + "in_proj_qkv"].d.data(),
                       T, H, kh * group_sz);
            linear_bwd(dz.data(), L.n1.data(), p.w.at(lb + "in_proj_z"),
                       dn1.data(), p.g[lb + "in_proj_z"].d.data(),
                       T, H, val_dim);
            linear_bwd(da_raw.data(), L.n1.data(), p.w.at(lb + "in_proj_a"),
                       dn1.data(), p.g[lb + "in_proj_a"].d.data(), T, H, vh);
            linear_bwd(db_raw.data(), L.n1.data(), p.w.at(lb + "in_proj_b"),
                       dn1.data(), p.g[lb + "in_proj_b"].d.data(), T, H, vh);
        } else {
            // -------- full attention backward --------
            std::vector<float> dao((size_t)T * Hq, 0.0f);
            const float* wo_in = c.attn_output_gate ? L.attn_gated.data()
                                                    : L.attn_out.data();
            linear_bwd(dproj_attn.data(), wo_in, p.w.at(ln(l, "wo")),
                       dao.data(), p.g[ln(l, "wo")].d.data(), T, Hq, H);
            std::vector<float> dgate;
            if (c.attn_output_gate) {
                // dao currently flows to gated output; split into raw
                // attention grad and gate-logit grad.
                dgate.assign((size_t)T * Hq, 0.0f);
                tpu_elementwise((int64_t)dao.size(), [&](int64_t i) {
                    float sg = sigmoid_f(L.attn_gate[(size_t)i]);
                    dgate[(size_t)i] =
                        dao[(size_t)i] * L.attn_out[(size_t)i] * sg * (1.0f - sg);
                    dao[(size_t)i] *= sg;
                });
            }
            // Gemma axis mirrors fwd: per-layer kv head count, local window
            // bounds, per-type rope, and K==V unified gradient merge.
            const int kvh = c.kv_heads_at(l);
            const int Hkvl = kvh * hd;
            // CSA2 (V4.1-Flash): CSA layers bound raw coverage to their
            // window and add a selected compressed-KV union term.
            const int crole = c.use_csa(l) ? L.csa_role : -1;
            const int csa_nc = c.use_csa(l) ? L.csa_nc : 0;
            const int win = c.use_csa(l) ? L.csa_win
                                         : (c.is_local_attn(l)
                                                ? c.sliding_window : 0);
            LayerCache* csa_prod = nullptr;
            if (crole >= 0 && csa_nc > 0) {
                const int prod = crole == 0 ? l : L.csa_src;
                csa_prod = &o.layers[prod];
            }
            int group = c.heads / kvh;
            float scale = 1.0f / std::sqrt((float)hd);
            std::vector<float> dq((size_t)T * Hq, 0.0f), dk((size_t)T * Hkvl, 0.0f),
                                dvv((size_t)T * Hkvl, 0.0f);
            // TPU lanes: one lane per kv-head group — the q-heads of a GQA
            // group share its k/v slices, so grouping keeps dk/dv writes
            // disjoint across lanes; per-element order is unchanged. CSA:
            // the compressed-KV grad accumulators are also sliced by kv
            // head, so the same grouping keeps them lane-disjoint.
            parallel_for(kvh, [&](int64_t gb, int64_t ge) {
            for (int64_t g = gb; g < ge; ++g)
            for (int h = (int)g * group;
                 h < std::min((int)(g + 1) * group, c.heads); ++h) {
                int kh2 = h / group;
                std::vector<float> dscore;
                const int csa_K = c.csa_topk;
                for (int t = 0; t < T; ++t) {
                    const float* pr = L.probs.data() + ((size_t)h * T + t) * T;
                    const float* dao_r = dao.data() + ((size_t)t * c.heads + h) * hd;
                    const int s0 = win > 0 ? std::max(0, t - win + 1) : 0;
                    const int nsel = csa_nc > 0 ? L.csa_nsel[(size_t)t] : 0;
                    const float* cp = csa_nc > 0
                        ? L.csa_cp.data() +
                              ((size_t)t * c.heads + h) * csa_K
                        : nullptr;
                    dscore.assign((size_t)(t - s0) + 1 + (size_t)nsel, 0.0f);
                    for (int s = s0; s <= t; ++s) {
                        const float* vr = L.v.data() + ((size_t)s * kvh + kh2) * hd;
                        dscore[(size_t)s - s0] = tpu_dot(dao_r, vr, hd);
                    }
                    for (int j = 0; j < nsel; ++j) {
                        const int cc = L.csa_sel[(size_t)t * csa_K + j];
                        const float* cvr = csa_prod->csa_cv.data() +
                            ((size_t)cc * kvh + kh2) * hd;
                        dscore[(size_t)(t - s0 + 1) + (size_t)j] =
                            tpu_dot(dao_r, cvr, hd);
                    }
                    float dsum = 0.0f;
                    for (int s = s0; s <= t; ++s) dsum += dscore[(size_t)s - s0] * pr[s];
                    for (int j = 0; j < nsel; ++j)
                        dsum += dscore[(size_t)(t - s0 + 1) + (size_t)j] *
                                cp[j];
                    for (int s = s0; s <= t; ++s) dscore[(size_t)s - s0] = pr[s] * (dscore[(size_t)s - s0] - dsum) * scale;
                    for (int j = 0; j < nsel; ++j)
                        dscore[(size_t)(t - s0 + 1) + (size_t)j] =
                            cp[j] * (dscore[(size_t)(t - s0 + 1) + (size_t)j] -
                                     dsum) * scale;
                    const float* qr = L.q.data() + ((size_t)t * c.heads + h) * hd;
                    float* dqr = dq.data() + ((size_t)t * c.heads + h) * hd;
                    for (int s = s0; s <= t; ++s) {
                        const float* kr = L.k.data() + ((size_t)s * kvh + kh2) * hd;
                        float* dkr = dk.data() + ((size_t)s * kvh + kh2) * hd;
                        tpu_axpy(dqr, dscore[(size_t)s - s0], kr, hd);
                        tpu_axpy(dkr, dscore[(size_t)s - s0], qr, hd);
                        float* dvr = dvv.data() + ((size_t)s * kvh + kh2) * hd;
                        tpu_axpy(dvr, pr[s], dao_r, hd);
                    }
                    for (int j = 0; j < nsel; ++j) {
                        const int cc = L.csa_sel[(size_t)t * csa_K + j];
                        const float ds =
                            dscore[(size_t)(t - s0 + 1) + (size_t)j];
                        const float* ckr = csa_prod->csa_ck.data() +
                            ((size_t)cc * kvh + kh2) * hd;
                        // compressed-KV grads live in the producer's
                        // roped-latent space; the producer unropes the sum
                        // once during its own finalize.
                        float* dckr = csa_prod->csa_dck.data() +
                            ((size_t)cc * kvh + kh2) * hd;
                        float* dcvr = csa_prod->csa_dcv.data() +
                            ((size_t)cc * kvh + kh2) * hd;
                        tpu_axpy(dqr, ds, ckr, hd);
                        tpu_axpy(dckr, ds, qr, hd);
                        tpu_axpy(dcvr, cp[j], dao_r, hd);
                    }
                }
            }
            });
            // CSA indexer auxiliary CE backward: d(CE)/d(isc_c) =
            // aux·w·(σ(isc_c) − mean_h p̃_h,c)/(T·heads). Gradients reach
            // the layer's own wiq and the producer's shared index keys.
            if (crole >= 0 && crole != 1 && csa_nc > 0 && c.csa_indexer &&
                c.csa_indexer_w > 0.0f && aux_scale != 0.0f) {
                const int csa_K = c.csa_topk;
                const float w = aux_scale * c.csa_indexer_w /
                                (std::max(1, T) * (float)c.heads);
                std::vector<float> diq((size_t)T * hd, 0.0f);
                for (int t = 0; t < T; ++t) {
                    const int cn = L.csa_ncand[(size_t)t];
                    if (cn <= 0) continue;
                    const float* isc =
                        L.csa_isc.data() + (size_t)t * csa_nc;
                    float imx = -1e30f;
                    for (int cc = 0; cc < cn; ++cc)
                        imx = std::max(imx, isc[cc]);
                    float isum = 0.0f;
                    for (int cc = 0; cc < cn; ++cc)
                        isum += std::exp(isc[cc] - imx);
                    float iinv = 1.0f / isum;
                    const float* iqr = L.csa_iq.data() + (size_t)t * hd;
                    float* diqr = diq.data() + (size_t)t * hd;
                    for (int cc = 0; cc < cn; ++cc) {
                        float sig = std::exp(isc[cc] - imx) * iinv;
                        float tgt = 0.0f;
                        for (int h = 0; h < c.heads; ++h)
                            tgt += L.csa_msc[((size_t)t * c.heads + h) *
                                             csa_nc + cc];
                        float ds = w * (sig - tgt / (float)c.heads);
                        if (ds == 0.0f) continue;
                        const float* ikr = csa_prod->csa_ik.data() +
                            (size_t)cc * hd;
                        tpu_axpy(diqr, ds, ikr, hd);
                        tpu_axpy(csa_prod->csa_dik.data() + (size_t)cc * hd,
                                 ds, iqr, hd);
                    }
                }
                linear_bwd(diq.data(), L.n1.data(), p.w.at(ln(l, "wiq")),
                           dn1.data(), p.g[ln(l, "wiq")].d.data(), T, H, hd);
            }
            // CSA producer finalize: unrope the accumulated compressed-K
            // grads into raw-latent space, fold the shared index-key chain
            // (ik = wik·mean_g ckr), then scatter the compressor grads to
            // member tokens in PRE-rope key space (merged after the raw
            // unrope below).
            std::vector<float> dk_csa, dv_csa;
            if (crole == 0 && csa_nc > 0) {
                const float thc = c.csa_rope_theta > 0.0f
                                      ? c.csa_rope_theta : c.rope_theta;
                const int rd2 = c.rotary_dim_at(l);
                if (rd2 < hd)
                    rope_hf_partial(L.csa_dck.data(), csa_nc, kvh, hd, rd2,
                                    thc, true);
                else
                    rope(L.csa_dck.data(), csa_nc, kvh, hd, thc, true);
                if (c.csa_indexer && !L.csa_dik.empty()) {
                    const float* wik = p.w.at(ln(l, "wik")).d.data();
                    float* gwik = p.g[ln(l, "wik")].d.data();
                    for (int cc = 0; cc < csa_nc; ++cc) {
                        const float* dik = L.csa_dik.data() + (size_t)cc * hd;
                        std::vector<float> mkr((size_t)hd, 0.0f);
                        for (int gg = 0; gg < kvh; ++gg)
                            tpu_axpy(mkr.data(), 1.0f / (float)kvh,
                                     L.csa_ckr.data() +
                                         ((size_t)cc * kvh + gg) * hd,
                                     hd);
                        // dwik += dik ⊗ mk ; dmk = wikᵀ·dik
                        for (int oi = 0; oi < hd; ++oi)
                            for (int ii = 0; ii < hd; ++ii)
                                gwik[(size_t)oi * hd + ii] +=
                                    dik[oi] * mkr[ii];
                        for (int gg = 0; gg < kvh; ++gg) {
                            float* dck = L.csa_dck.data() +
                                ((size_t)cc * kvh + gg) * hd;
                            for (int ii = 0; ii < hd; ++ii) {
                                float s = 0.0f;
                                for (int oi = 0; oi < hd; ++oi)
                                    s += wik[(size_t)oi * hd + ii] * dik[oi];
                                dck[ii] += s / (float)kvh;
                            }
                        }
                    }
                }
                // compressor scatter: dck_raw/dcv per (chunk, kv-head) →
                // member-token grads in pre-rope space + wck/wcv grads.
                const int rr = c.csa_ratio;
                dk_csa.assign((size_t)T * Hkvl, 0.0f);
                dv_csa.assign((size_t)T * Hkvl, 0.0f);
                const float* wck = p.w.at(ln(l, "wck")).d.data();
                const float* wcv = p.w.at(ln(l, "wcv")).d.data();
                float* gwck = p.g[ln(l, "wck")].d.data();
                float* gwcv = p.g[ln(l, "wcv")].d.data();
                std::vector<float> cvec((size_t)rr * hd);
                for (int cc = 0; cc < csa_nc; ++cc)
                    for (int gg = 0; gg < kvh; ++gg) {
                        const float* dck =
                            L.csa_dck.data() + ((size_t)cc * kvh + gg) * hd;
                        const float* dcv =
                            L.csa_dcv.data() + ((size_t)cc * kvh + gg) * hd;
                        for (int j = 0; j < rr; ++j) {
                            const size_t mrow =
                                ((size_t)(cc * rr + j) * kvh + gg) * hd;
                            std::copy(L.csa_kpre.data() + mrow,
                                      L.csa_kpre.data() + mrow + hd,
                                      cvec.data() + (size_t)j * hd);
                        }
                        // dwck += dck ⊗ cvec ; dvec = wckᵀ dck
                        for (int oi = 0; oi < hd; ++oi)
                            for (int ii = 0; ii < rr * hd; ++ii)
                                gwck[(size_t)oi * (size_t)(rr * hd) + ii] +=
                                    dck[oi] * cvec[ii];
                        for (int j = 0; j < rr; ++j) {
                            float* dm =
                                dk_csa.data() +
                                ((size_t)(cc * rr + j) * kvh + gg) * hd;
                            for (int ii = 0; ii < hd; ++ii) {
                                float s = 0.0f;
                                for (int oi = 0; oi < hd; ++oi)
                                    s += wck[(size_t)oi * (size_t)(rr * hd) +
                                             (size_t)j * hd + ii] * dck[oi];
                                dm[ii] += s;
                            }
                        }
                        for (int j = 0; j < rr; ++j) {
                            const size_t mrow =
                                ((size_t)(cc * rr + j) * kvh + gg) * hd;
                            std::copy(L.v.data() + mrow,
                                      L.v.data() + mrow + hd,
                                      cvec.data() + (size_t)j * hd);
                        }
                        for (int oi = 0; oi < hd; ++oi)
                            for (int ii = 0; ii < rr * hd; ++ii)
                                gwcv[(size_t)oi * (size_t)(rr * hd) + ii] +=
                                    dcv[oi] * cvec[ii];
                        for (int j = 0; j < rr; ++j) {
                            float* dm =
                                dv_csa.data() +
                                ((size_t)(cc * rr + j) * kvh + gg) * hd;
                            for (int ii = 0; ii < hd; ++ii) {
                                float s = 0.0f;
                                for (int oi = 0; oi < hd; ++oi)
                                    s += wcv[(size_t)oi * (size_t)(rr * hd) +
                                             (size_t)j * hd + ii] * dcv[oi];
                                dm[ii] += s;
                            }
                        }
                    }
            }
            const int rd = c.rotary_dim_at(l);
            const float th = c.rope_theta_at(l);
            if (rd < hd) {
                rope_hf_partial(dq.data(), T, c.heads, hd, rd, th, true);
                rope_hf_partial(dk.data(), T, kvh, hd, rd, th, true);
            } else {
                rope(dq.data(), T, c.heads, hd, th, true);
                rope(dk.data(), T, kvh, hd, th, true);
            }
            // CSA producer: merge compressor member grads — they live in
            // pre-rope key space, so they join dk only after the unrope.
            if (!dk_csa.empty()) {
                for (size_t i = 0; i < dk.size(); ++i) dk[i] += dk_csa[i];
                for (size_t i = 0; i < dvv.size(); ++i) dvv[i] += dv_csa[i];
            }
            if (c.qk_norm) {
                std::vector<float> dq_raw((size_t)T * Hq, 0.0f),
                                   dk_raw((size_t)T * Hkvl, 0.0f);
                for (int t = 0; t < T; ++t) {
                    for (int h = 0; h < c.heads; ++h) {
                        size_t off = ((size_t)t * c.heads + h) * (size_t)hd;
                        rmsnorm_bwd(dq.data() + off, L.qk_qraw.data() + off,
                                    p.w.at(ln(l, "q_norm")).d.data(),
                                    L.qk_qrms.data() + (size_t)t * c.heads + h,
                                    dq_raw.data() + off,
                                    p.g[ln(l, "q_norm")].d.data(), 1, hd);
                    }
                    for (int h = 0; h < kvh; ++h) {
                        size_t off = ((size_t)t * kvh + h) * (size_t)hd;
                        rmsnorm_bwd(dk.data() + off, L.qk_kraw.data() + off,
                                    p.w.at(ln(l, "k_norm")).d.data(),
                                    L.qk_krms.data() + (size_t)t * kvh + h,
                                    dk_raw.data() + off,
                                    p.g[ln(l, "k_norm")].d.data(), 1, hd);
                    }
                }
                dq.swap(dq_raw); dk.swap(dk_raw);
            }
            if (c.attn_output_gate) {
                // repack [q|gate] per head for the fused wq weight
                std::vector<float> dqf((size_t)T * Hq * 2, 0.0f);
                for (int t = 0; t < T; ++t)
                    for (int h = 0; h < c.heads; ++h) {
                        float* fr = dqf.data() +
                            ((size_t)t * c.heads + h) * (size_t)hd * 2;
                        std::copy(dq.begin() + ((size_t)t * c.heads + h) * hd,
                                  dq.begin() + ((size_t)t * c.heads + h) * hd + hd,
                                  fr);
                        std::copy(dgate.begin() + ((size_t)t * c.heads + h) * hd,
                                  dgate.begin() + ((size_t)t * c.heads + h) * hd + hd,
                                  fr + hd);
                    }
                linear_bwd(dqf.data(), L.n1.data(), p.w.at(ln(l, "wq")),
                           dn1.data(), p.g[ln(l, "wq")].d.data(), T, H, Hq * 2);
            } else {
                linear_bwd(dq.data(), L.n1.data(), p.w.at(ln(l, "wq")),
                           dn1.data(), p.g[ln(l, "wq")].d.data(), T, H, Hq);
            }
            if (c.kv_unified(l)) {
                // unified K==V: the shared projection sees dk + dv.
                tpu_elementwise((int64_t)dk.size(), [&](int64_t i) {
                    dk[(size_t)i] += dvv[(size_t)i];
                });
                linear_bwd(dk.data(), L.n1.data(), p.w.at(ln(l, "wkv")),
                           dn1.data(), p.g[ln(l, "wkv")].d.data(), T, H, Hkvl);
            } else {
                linear_bwd(dk.data(), L.n1.data(), p.w.at(ln(l, "wk")),
                           dn1.data(), p.g[ln(l, "wk")].d.data(), T, H, Hkvl);
                linear_bwd(dvv.data(), L.n1.data(), p.w.at(ln(l, "wv")),
                           dn1.data(), p.g[ln(l, "wv")].d.data(), T, H, Hkvl);
            }
        }
        std::vector<float> dx_in2((size_t)T * H, 0.0f);
        rmsnorm_bwd(dn1.data(), L.x_in.data(), p.w.at(ln(l, "norm1")).d.data(),
                    L.rms1.data(), dx_in2.data(), p.g[ln(l, "norm1")].d.data(), T, H);
        tpu_elementwise((int64_t)dx.size(), [&](int64_t i) {
            dx[(size_t)i] = dx_attn_in[(size_t)i] + dx_in2[(size_t)i];
        });
    }
    // embedding backward (text rows start after the P prefix rows)
    for (int t = 0; t < PT; ++t) {
        float* ger = p.g["embed"].d.data() + (size_t)ids[t] * H;
        for (int i = 0; i < H; ++i) ger[i] += dx[(size_t)(P + t) * H + i];
    }
    if (P > 0) {
        // vision projection grad; patch-side dx is discarded (input).
        linear_bwd(dx.data(), o.vision_in.data(),
                   p.w.at("vision.patch_proj"), nullptr,
                   p.g["vision.patch_proj"].d.data(),
                   P, c.vision_patch_dim, H);
    }
}

// ------------------------------------------------------------ losses/API --

static float ce_loss(const std::vector<float>& logits,
                     const std::vector<int>& labels, int T, int V,
                     std::vector<float>& dlogits) {
    dlogits.assign(logits.size(), 0.0f);
    float loss = 0.0f; int cnt = 0;
    for (int t = 0; t < T; ++t) {
        int y = labels[t];
        if (y < 0 || y >= V) continue;
        ++cnt;
        const float* lr = logits.data() + (size_t)t * V;
        float mx = *std::max_element(lr, lr + V), sum = 0.0f;
        float* dl = dlogits.data() + (size_t)t * V;
        for (int i = 0; i < V; ++i) { dl[i] = std::exp(lr[i] - mx); sum += dl[i]; }
        loss += std::log(sum) - (lr[y] - mx);
        for (int i = 0; i < V; ++i) dl[i] /= sum;
        dl[y] -= 1.0f;
    }
    if (!cnt) return 0.0f;
    float inv = 1.0f / cnt;
    for (auto& d : dlogits) d *= inv;
    return loss * inv;
}

static float seq_logprob(const std::vector<float>& logits,
                         const std::vector<int>& labels, int T, int V) {
    float lp = 0.0f;
    for (int t = 0; t < T; ++t) {
        int y = labels[t];
        if (y < 0 || y >= V) continue;
        const float* lr = logits.data() + (size_t)t * V;
        float mx = *std::max_element(lr, lr + V), sum = 0.0f;
        for (int i = 0; i < V; ++i) sum += std::exp(lr[i] - mx);
        lp += (lr[y] - mx) - std::log(sum);
    }
    return lp;
}

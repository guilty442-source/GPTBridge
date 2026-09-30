// xct_backward.h — B94 fragment of xingcheng_trainer.cpp (backward + losses).
// Included once by xingcheng_trainer.cpp inside namespace xct.
#pragma once

// -------------------------------------------------------------- backward --

static void bwd(Params& p, const ModelConfig& c, const std::vector<int>& ids,
                Fwd& o, const std::vector<float>& dlogits, float aux_scale) {
    const int T = (int)ids.size();
    const int H = c.hidden, hd = H / c.heads;
    const int Hq = c.heads * hd, Hkv = c.kv_heads * hd;
    std::vector<float> dh((size_t)T * H, 0.0f);
    linear_bwd(dlogits.data(), o.hidden.data(), p.w.at("lm_head"),
               dh.data(), p.g["lm_head"].d.data(), T, H, c.vocab);
    std::vector<float> dx_fin((size_t)T * H, 0.0f);
    rmsnorm_bwd(dh.data(), o.x_fin.data(), p.w.at("norm_f").d.data(),
                o.rmsf.data(), dx_fin.data(), p.g["norm_f"].d.data(), T, H);
    std::vector<float> dx = dx_fin;
    for (int l = c.layers - 1; l >= 0; --l) {
        LayerCache& L = o.layers[l];
        bool moe = c.moe_experts > 0 && (l % c.moe_layer_interval == 0);
        // residual split: dx flows to ffn path (through dproj) and to x_res.
        std::vector<float> dproj = dx;                    // [T,H]
        std::vector<float> dx_res = dx;                   // residual branch
        std::vector<float> dn2((size_t)T * H, 0.0f);
        if (!moe) {
            std::vector<float> dfh((size_t)T * c.inter, 0.0f);
            linear_bwd(dproj.data(), L.fh.data(), p.w.at(ln(l, "w2")),
                       dfh.data(), p.g[ln(l, "w2")].d.data(), T, c.inter, H);
            std::vector<float> dfa((size_t)T * c.inter, 0.0f), dfb((size_t)T * c.inter, 0.0f);
            for (size_t i = 0; i < L.fh.size(); ++i) {
                float a = L.fa[i], b = L.fb[i], d = dfh[i];
                float sig = silu_f(a);
                dfa[i] += d * b * sig * (1.0f + a * (1.0f - sig));
                dfb[i] += d * sig;
            }
            linear_bwd(dfa.data(), L.n2.data(), p.w.at(ln(l, "w1")),
                       dn2.data(), p.g[ln(l, "w1")].d.data(), T, H, c.inter);
            linear_bwd(dfb.data(), L.n2.data(), p.w.at(ln(l, "w3")),
                       dn2.data(), p.g[ln(l, "w3")].d.data(), T, H, c.inter);
        } else {
            const int E = c.moe_experts, K = c.moe_top_k;
            const int EI = c.expert_inter();
            const int SI = c.shared_inter();
            std::vector<float> dgl((size_t)T * E, 0.0f);
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
                        float sig = silu_f(a);
                        dfa[i] += d * bb * sig * (1.0f + a * (1.0f - sig));
                        dfb[i] += d * sig;
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
                // softmax backward at router logits (aux + weighted path share
                // the logit grads approximated by direct slot contribution).
                float* gpl = L.gate_probs.data() + (size_t)t * E;
                float dotp = 0.0f;
                const float* dglr = dgl.data() + (size_t)t * E;
                for (int e = 0; e < E; ++e) dotp += dglr[e] * gpl[e];
                float* gll = nullptr; // accumulate into gate weight directly
                std::vector<float> din(E);
                for (int e = 0; e < E; ++e) din[e] = gpl[e] * (dglr[e] - dotp);
                linear_bwd(din.data(), xr, p.w.at(ln(l, "gate")),
                           dxr, p.g[ln(l, "gate")].d.data(), 1, H, E);
                (void)gll; (void)aux_scale;
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
                    for (int t = 0; t < T; ++t) {
                        float g = L.shared_gate_sig[(size_t)t];
                        for (int i = 0; i < H; ++i)
                            dsg_in[(size_t)t * H + i] =
                                dproj[(size_t)t * H + i] * g;
                    }
                    dproj_use = dsg_in.data();
                }
                linear_bwd(dproj_use, sfh.data(), p.w.at(b + "w2"),
                           dsh.data(), p.g[b + "w2"].d.data(), T, SI, H);
                std::vector<float> dsa((size_t)T * SI, 0.0f);
                std::vector<float> dsb((size_t)T * SI, 0.0f);
                for (size_t i = 0; i < sfh.size(); ++i) {
                    float a = sfa[i], bb = sfb[i], d = dsh[i];
                    float sig = silu_f(a);
                    dsa[i] += d * bb * sig * (1.0f + a * (1.0f - sig));
                    dsb[i] += d * sig;
                }
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
        for (size_t i = 0; i < dpre.size(); ++i) dpre[i] = dx_res[i] + dxres2[i];
        // attention block: dpre splits into attn path + layer-input residual
        std::vector<float> dproj_attn = dpre;             // through output proj
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
            for (size_t i = 0; i < don.size(); ++i) {
                float z = L.lin_z[i];
                float sg = sigmoid_f(z);
                donorm[i] = don[i] * z * sg;
                dz[i] = don[i] * L.lin_onorm[i] * sg * (1.0f + z * (1.0f - sg));
            }
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
            // recurrent scan backward (reverse-time)
            std::vector<float> dqn((size_t)T * vh * kd, 0.0f),
                               dkn((size_t)T * vh * kd, 0.0f),
                               dv((size_t)T * val_dim, 0.0f),
                               da_raw((size_t)T * vh, 0.0f),
                               db_raw((size_t)T * vh, 0.0f);
            float* dA_log = p.g[lb + "A_log"].d.data();
            float* ddt_bias = p.g[lb + "dt_bias"].d.data();
            const float* A_log = p.w.at(lb + "A_log").d.data();
            for (int h = 0; h < vh; ++h) {
                std::vector<float> dS(ssz, 0.0f);  // carry: dL/dS_t
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
                    std::vector<float> Sd(ssz), u(vd), kvm(vd);
                    for (size_t i = 0; i < ssz; ++i) Sd[i] = S_prev[i] * dec;
                    for (int i = 0; i < vd; ++i) {
                        float kv = 0.0f;
                        for (int d = 0; d < kd; ++d)
                            kv += Sd[(size_t)d * vd + i] * kr[d];
                        kvm[i] = kv;
                        u[i] = (vr[i] - kv) * bt;
                    }
                    // o_t = S_tᵀq_t → D += q ⊗ do_t ; dq_t = S_t·do_t
                    float* dqr = dqn.data() + ((size_t)t * vh + h) * kd;
                    for (int d = 0; d < kd; ++d) {
                        float s = 0.0f;
                        for (int i = 0; i < vd; ++i)
                            s += S_t[(size_t)d * vd + i] * dor[i];
                        dqr[d] += s;
                        for (int i = 0; i < vd; ++i)
                            dS[(size_t)d * vd + i] += qr[d] * dor[i];
                    }
                    // S_t = S̃ + k⊗u → du = Dᵀk ; dk += D·u ; dS̃ = D + k⊗dkv
                    std::vector<float> du(vd, 0.0f);
                    for (int i = 0; i < vd; ++i)
                        for (int d = 0; d < kd; ++d)
                            du[i] += dS[(size_t)d * vd + i] * kr[d];
                    float* dkr = dkn.data() + ((size_t)t * vh + h) * kd;
                    for (int d = 0; d < kd; ++d)
                        for (int i = 0; i < vd; ++i)
                            dkr[d] += dS[(size_t)d * vd + i] * u[i];
                    // u = β(v − kv_mem)
                    float dbt = 0.0f;
                    for (int i = 0; i < vd; ++i) {
                        float vmkv = vr[i] - kvm[i];
                        dv[(size_t)((size_t)t * vh + h) * vd + i] += bt * du[i];
                        dbt += vmkv * du[i];
                    }
                    db_raw[(size_t)t * vh + h] += dbt * bt * (1.0f - bt);
                    // kv_mem = S̃ᵀk → dS̃ += k⊗dkv ; dk += S̃·dkv
                    for (int i = 0; i < vd; ++i) {
                        float dkv = -bt * du[i];
                        for (int d = 0; d < kd; ++d) {
                            dS[(size_t)d * vd + i] += kr[d] * dkv;
                            dkr[d] += Sd[(size_t)d * vd + i] * dkv;
                        }
                    }
                    // S̃ = dec·S_{t-1}: dS_{t-1} = dec·dS̃ ; dg = Σ dS̃⊙S̃
                    float dg = 0.0f;
                    for (size_t i = 0; i < ssz; ++i) {
                        dg += dS[i] * Sd[i];
                        dS[i] *= dec;
                    }
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
            // fold v-head grads back to k-heads (repeat_interleave inverse)
            const float qscale = 1.0f / std::sqrt((float)kd);
            std::vector<float> dqk_raw((size_t)T * kh * kd, 0.0f),
                               dkk_raw((size_t)T * kh * kd, 0.0f),
                               dconv_v((size_t)T * val_dim);
            for (int t = 0; t < T; ++t)
                for (int h = 0; h < vh; ++h) {
                    int g = h / ratio;
                    for (int d = 0; d < kd; ++d) {
                        dqk_raw[(size_t)(t * kh + g) * kd + d] +=
                            dqn[((size_t)t * vh + h) * kd + d];
                        dkk_raw[(size_t)(t * kh + g) * kd + d] +=
                            dkn[((size_t)t * vh + h) * kd + d];
                    }
                }
            // undo q scale then l2norm backward on q and k
            for (auto& x : dqk_raw) x *= qscale;
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
                for (size_t i = 0; i < dao.size(); ++i) {
                    float sg = sigmoid_f(L.attn_gate[i]);
                    dgate[i] = dao[i] * L.attn_out[i] * sg * (1.0f - sg);
                    dao[i] *= sg;
                }
            }
            int group = c.heads / c.kv_heads;
            float scale = 1.0f / std::sqrt((float)hd);
            std::vector<float> dq((size_t)T * Hq, 0.0f), dk((size_t)T * Hkv, 0.0f),
                                dvv((size_t)T * Hkv, 0.0f);
            for (int h = 0; h < c.heads; ++h) {
                int kh2 = h / group;
                for (int t = 0; t < T; ++t) {
                    const float* pr = L.probs.data() + ((size_t)h * T + t) * T;
                    const float* dao_r = dao.data() + ((size_t)t * c.heads + h) * hd;
                    std::vector<float> dscore(t + 1, 0.0f);
                    for (int s = 0; s <= t; ++s) {
                        float dotv = 0.0f;
                        const float* vr = L.v.data() + ((size_t)s * c.kv_heads + kh2) * hd;
                        for (int i = 0; i < hd; ++i) dotv += dao_r[i] * vr[i];
                        dscore[s] = dotv;
                    }
                    float dsum = 0.0f;
                    for (int s = 0; s <= t; ++s) dsum += dscore[s] * pr[s];
                    for (int s = 0; s <= t; ++s) dscore[s] = pr[s] * (dscore[s] - dsum) * scale;
                    const float* qr = L.q.data() + ((size_t)t * c.heads + h) * hd;
                    float* dqr = dq.data() + ((size_t)t * c.heads + h) * hd;
                    for (int s = 0; s <= t; ++s) {
                        const float* kr = L.k.data() + ((size_t)s * c.kv_heads + kh2) * hd;
                        float* dkr = dk.data() + ((size_t)s * c.kv_heads + kh2) * hd;
                        for (int i = 0; i < hd; ++i) {
                            dqr[i] += dscore[s] * kr[i];
                            dkr[i] += dscore[s] * qr[i];
                        }
                        float* dvr = dvv.data() + ((size_t)s * c.kv_heads + kh2) * hd;
                        for (int i = 0; i < hd; ++i) dvr[i] += pr[s] * dao_r[i];
                    }
                }
            }
            const int rd = c.rotary_dim();
            if (rd < hd) {
                rope_hf_partial(dq.data(), T, c.heads, hd, rd,
                                c.rope_theta, true);
                rope_hf_partial(dk.data(), T, c.kv_heads, hd, rd,
                                c.rope_theta, true);
            } else {
                rope(dq.data(), T, c.heads, hd, c.rope_theta, true);
                rope(dk.data(), T, c.kv_heads, hd, c.rope_theta, true);
            }
            if (c.qk_norm) {
                std::vector<float> dq_raw((size_t)T * Hq, 0.0f),
                                   dk_raw((size_t)T * Hkv, 0.0f);
                for (int t = 0; t < T; ++t) {
                    for (int h = 0; h < c.heads; ++h) {
                        size_t off = ((size_t)t * c.heads + h) * (size_t)hd;
                        rmsnorm_bwd(dq.data() + off, L.qk_qraw.data() + off,
                                    p.w.at(ln(l, "q_norm")).d.data(),
                                    L.qk_qrms.data() + (size_t)t * c.heads + h,
                                    dq_raw.data() + off,
                                    p.g[ln(l, "q_norm")].d.data(), 1, hd);
                    }
                    for (int h = 0; h < c.kv_heads; ++h) {
                        size_t off = ((size_t)t * c.kv_heads + h) * (size_t)hd;
                        rmsnorm_bwd(dk.data() + off, L.qk_kraw.data() + off,
                                    p.w.at(ln(l, "k_norm")).d.data(),
                                    L.qk_krms.data() + (size_t)t * c.kv_heads + h,
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
            linear_bwd(dk.data(), L.n1.data(), p.w.at(ln(l, "wk")),
                       dn1.data(), p.g[ln(l, "wk")].d.data(), T, H, Hkv);
            linear_bwd(dvv.data(), L.n1.data(), p.w.at(ln(l, "wv")),
                       dn1.data(), p.g[ln(l, "wv")].d.data(), T, H, Hkv);
        }
        std::vector<float> dx_in2((size_t)T * H, 0.0f);
        rmsnorm_bwd(dn1.data(), L.x_in.data(), p.w.at(ln(l, "norm1")).d.data(),
                    L.rms1.data(), dx_in2.data(), p.g[ln(l, "norm1")].d.data(), T, H);
        for (size_t i = 0; i < dx.size(); ++i) dx[i] = dx_attn_in[i] + dx_in2[i];
    }
    // embedding backward
    for (int t = 0; t < T; ++t) {
        float* ger = p.g["embed"].d.data() + (size_t)ids[t] * H;
        for (int i = 0; i < H; ++i) ger[i] += dx[(size_t)t * H + i];
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

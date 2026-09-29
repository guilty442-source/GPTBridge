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
                dfa[i] += d * b * sig * (1.0f + a * (1.0f - sig) / (sig == 0.0f ? 1.0f : sig));
                dfb[i] += d * sig;
            }
            linear_bwd(dfa.data(), L.n2.data(), p.w.at(ln(l, "w1")),
                       dn2.data(), p.g[ln(l, "w1")].d.data(), T, H, c.inter);
            linear_bwd(dfb.data(), L.n2.data(), p.w.at(ln(l, "w3")),
                       dn2.data(), p.g[ln(l, "w3")].d.data(), T, H, c.inter);
        } else {
            const int E = c.moe_experts, K = c.moe_top_k;
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
                    std::vector<float> dfh(c.inter, 0.0f);
                    linear_bwd(deo.data(), fh.data(), p.w.at(b + "w2"),
                               dfh.data(), p.g[b + "w2"].d.data(), 1, c.inter, H);
                    std::vector<float> dfa(c.inter, 0.0f), dfb(c.inter, 0.0f);
                    for (int i = 0; i < c.inter; ++i) {
                        float a = fa[i], bb = fb[i], d = dfh[i];
                        float sig = silu_f(a);
                        dfa[i] += d * bb * sig * (1.0f + a * (1.0f - sig) / (sig == 0.0f ? 1.0f : sig));
                        dfb[i] += d * sig;
                    }
                    linear_bwd(dfa.data(), xr, p.w.at(b + "w1"),
                               dxr, p.g[b + "w1"].d.data(), 1, H, c.inter);
                    linear_bwd(dfb.data(), xr, p.w.at(b + "w3"),
                               dxr, p.g[b + "w3"].d.data(), 1, H, c.inter);
                    // router weight grad: d(wgt * eo)/d gp[e]
                    float dot = 0.0f;
                    for (int i = 0; i < H; ++i) {
                        // eo = W2 @ fh recomputed cheaply via fwd cache
                    }
                    // dout/d(gp[e]) = eo/wsum - sum_s(wgt_s*eo_s)*gp[e]/wsum^2 + aux
                    // compute eo once:
                    std::vector<float> eo(H);
                    linear_fwd(fh.data(), p.w.at(b + "w2"), eo.data(), 1, c.inter, H);
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
        }
        // norm2 backward: dn2 -> dx_res (accumulate into residual branch)
        std::vector<float> dxres2((size_t)T * H, 0.0f);
        rmsnorm_bwd(dn2.data(), L.x_res.data(), p.w.at(ln(l, "norm2")).d.data(),
                    L.rms2.data(), dxres2.data(), p.g[ln(l, "norm2")].d.data(), T, H);
        std::vector<float> dpre((size_t)T * H);
        for (size_t i = 0; i < dpre.size(); ++i) dpre[i] = dx_res[i] + dxres2[i];
        // attention block: dpre splits into attn path + layer-input residual
        std::vector<float> dproj_attn = dpre;             // through wo
        std::vector<float> dx_attn_in = dpre;             // residual to x_in
        std::vector<float> dao((size_t)T * Hq, 0.0f);
        linear_bwd(dproj_attn.data(), L.attn_out.data(), p.w.at(ln(l, "wo")),
                   dao.data(), p.g[ln(l, "wo")].d.data(), T, Hq, H);
        // attention backward
        int group = c.heads / c.kv_heads;
        float scale = 1.0f / std::sqrt((float)hd);
        std::vector<float> dq((size_t)T * Hq, 0.0f), dk((size_t)T * Hkv, 0.0f),
                            dv((size_t)T * Hkv, 0.0f);
        for (int h = 0; h < c.heads; ++h) {
            int kh = h / group;
            for (int t = 0; t < T; ++t) {
                const float* pr = L.probs.data() + ((size_t)h * T + t) * T;
                const float* dao_r = dao.data() + ((size_t)t * c.heads + h) * hd;
                // d(probs)/d(scores): softmax jacobian
                std::vector<float> dscore(t + 1, 0.0f);
                for (int s = 0; s <= t; ++s) {
                    float dotv = 0.0f;
                    const float* vr = L.v.data() + ((size_t)s * c.kv_heads + kh) * hd;
                    for (int i = 0; i < hd; ++i) dotv += dao_r[i] * vr[i];
                    dscore[s] = dotv;
                }
                float dsum = 0.0f;
                for (int s = 0; s <= t; ++s) dsum += dscore[s] * pr[s];
                for (int s = 0; s <= t; ++s) dscore[s] = pr[s] * (dscore[s] - dsum) * scale;
                const float* qr = L.q.data() + ((size_t)t * c.heads + h) * hd;
                float* dqr = dq.data() + ((size_t)t * c.heads + h) * hd;
                for (int s = 0; s <= t; ++s) {
                    const float* kr = L.k.data() + ((size_t)s * c.kv_heads + kh) * hd;
                    float* dkr = dk.data() + ((size_t)s * c.kv_heads + kh) * hd;
                    for (int i = 0; i < hd; ++i) {
                        dqr[i] += dscore[s] * kr[i];
                        dkr[i] += dscore[s] * qr[i];
                    }
                    float* dvr = dv.data() + ((size_t)s * c.kv_heads + kh) * hd;
                    for (int i = 0; i < hd; ++i) dvr[i] += pr[s] * dao_r[i];
                }
            }
        }
        rope(dq.data(), T, c.heads, hd, c.rope_theta, true);
        rope(dk.data(), T, c.kv_heads, hd, c.rope_theta, true);
        std::vector<float> dn1((size_t)T * H, 0.0f);
        linear_bwd(dq.data(), L.n1.data(), p.w.at(ln(l, "wq")),
                   dn1.data(), p.g[ln(l, "wq")].d.data(), T, H, Hq);
        linear_bwd(dk.data(), L.n1.data(), p.w.at(ln(l, "wk")),
                   dn1.data(), p.g[ln(l, "wk")].d.data(), T, H, Hkv);
        linear_bwd(dv.data(), L.n1.data(), p.w.at(ln(l, "wv")),
                   dn1.data(), p.g[ln(l, "wv")].d.data(), T, H, Hkv);
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

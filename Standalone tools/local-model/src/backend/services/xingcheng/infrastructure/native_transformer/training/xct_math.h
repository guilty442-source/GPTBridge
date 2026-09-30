// xct_math.h — B94 fragment of xingcheng_trainer.cpp (primitives + forward).
// Included once by xingcheng_trainer.cpp inside namespace xct.
#pragma once

// ------------------------------------------------------------- primitives --

static void linear_fwd(const float* x, const Tensor& W, float* y,
                       int T, int I, int O) {
    for (int t = 0; t < T; ++t) {
        const float* xr = x + (size_t)t * I;
        for (int o = 0; o < O; ++o) {
            const float* wr = W.d.data() + (size_t)o * I;
            float s = 0.0f;
            for (int i = 0; i < I; ++i) s += xr[i] * wr[i];
            y[(size_t)t * O + o] = s;
        }
    }
}

static void linear_bwd(const float* dy, const float* x, const Tensor& W,
                       float* dx, float* dW, int T, int I, int O) {
    for (int o = 0; o < O; ++o) {
        const float* w = W.d.data() + (size_t)o * I;
        float* dw = dW + (size_t)o * I;
        for (int t = 0; t < T; ++t) {
            float d = dy[(size_t)t * O + o];
            const float* xr = x + (size_t)t * I;
            for (int i = 0; i < I; ++i) dw[i] += d * xr[i];
            if (dx) {
                float* dxr = dx + (size_t)t * I;
                for (int i = 0; i < I; ++i) dxr[i] += d * w[i];
            }
        }
    }
}

static void rmsnorm_fwd(const float* x, const float* w, float* y, float* rms,
                        int T, int H, float eps) {
    for (int t = 0; t < T; ++t) {
        const float* xr = x + (size_t)t * H;
        float ss = 0.0f;
        for (int i = 0; i < H; ++i) ss += xr[i] * xr[i];
        float r = std::sqrt(ss / H + eps);
        rms[t] = r;
        float inv = 1.0f / r;
        for (int i = 0; i < H; ++i) y[(size_t)t * H + i] = xr[i] * inv * w[i];
    }
}

static void rmsnorm_bwd(const float* dy, const float* x, const float* w,
                        const float* rms, float* dx, float* dw, int T, int H) {
    for (int t = 0; t < T; ++t) {
        const float* xr = x + (size_t)t * H;
        const float* dyr = dy + (size_t)t * H;
        float inv = 1.0f / rms[t];
        float dot = 0.0f;
        for (int i = 0; i < H; ++i) dot += dyr[i] * xr[i] * w[i];
        for (int i = 0; i < H; ++i) {
            if (dw) dw[i] += dyr[i] * xr[i] * inv;
            dx[(size_t)t * H + i] += (dyr[i] * w[i] - xr[i] * dot * inv * inv / H) * inv;
        }
    }
}

static void rope(float* v, int T, int nh, int hd, float theta, bool inverse) {
    for (int t = 0; t < T; ++t)
        for (int h = 0; h < nh; ++h) {
            float* r = v + ((size_t)t * nh + h) * hd;
            for (int i = 0; i + 1 < hd; i += 2) {
                float fr = std::pow(theta, -(float)i / hd);
                float c = std::cos(t * fr), s = std::sin(t * fr);
                if (inverse) s = -s;
                float a = r[i], b = r[i + 1];
                r[i] = a * c - b * s;
                r[i + 1] = a * s + b * c;
            }
        }
}

static inline float silu_f(float x) { return x / (1.0f + std::exp(-x)); }

// --------------------------------------------------------------- forward --

struct LayerCache {
    std::vector<float> x_in, n1, rms1;       // attention block
    std::vector<float> q, k, v, probs, attn_out, x_res;
    std::vector<float> n2, rms2;             // ffn block
    // dense ffn caches (single buffer) or MoE per-slot caches
    std::vector<float> fa, fb, fh;
    std::vector<int> moe_idx;                // [T*K]
    std::vector<float> moe_w;                // [T*K]
    std::vector<std::vector<float>> mfa, mfb, mfh; // [T*K][expert_inter]
    std::vector<float> gate_logits, gate_probs;    // [T,E]
    std::vector<std::vector<float>> sfa, sfb, sfh; // [shared][T*shared_inter]
};

struct Fwd {
    std::vector<float> logits;   // [T,V]
    std::vector<float> hidden;   // post final norm [T,H]
    std::vector<float> x_fin;    // pre final norm [T,H]
    std::vector<float> rmsf;     // [T]
    std::vector<LayerCache> layers;
    float moe_aux = 0.0f;
};

static void fwd(const Params& p, const ModelConfig& c,
                const std::vector<int>& ids, Fwd& o) {
    const int T = (int)ids.size();
    const int H = c.hidden, hd = H / c.heads;
    const int Hq = c.heads * hd, Hkv = c.kv_heads * hd;
    std::vector<float> x((size_t)T * H);
    for (int t = 0; t < T; ++t) {
        const float* er = p.w.at("embed").d.data() + (size_t)ids[t] * H;
        std::copy(er, er + H, x.data() + (size_t)t * H);
    }
    o.layers.resize(c.layers);
    for (int l = 0; l < c.layers; ++l) {
        LayerCache& L = o.layers[l];
        L.x_in = x;
        L.n1.resize((size_t)T * H); L.rms1.resize(T);
        rmsnorm_fwd(x.data(), p.w.at(ln(l, "norm1")).d.data(),
                    L.n1.data(), L.rms1.data(), T, H, c.rms_eps);
        L.q.resize((size_t)T * Hq); L.k.resize((size_t)T * Hkv); L.v.resize((size_t)T * Hkv);
        linear_fwd(L.n1.data(), p.w.at(ln(l, "wq")), L.q.data(), T, H, Hq);
        linear_fwd(L.n1.data(), p.w.at(ln(l, "wk")), L.k.data(), T, H, Hkv);
        linear_fwd(L.n1.data(), p.w.at(ln(l, "wv")), L.v.data(), T, H, Hkv);
        rope(L.q.data(), T, c.heads, hd, c.rope_theta, false);
        rope(L.k.data(), T, c.kv_heads, hd, c.rope_theta, false);
        int group = c.heads / c.kv_heads;
        float scale = 1.0f / std::sqrt((float)hd);
        L.probs.assign((size_t)c.heads * T * T, 0.0f);
        L.attn_out.assign((size_t)T * Hq, 0.0f);
        for (int h = 0; h < c.heads; ++h) {
            int kh = h / group;
            for (int t = 0; t < T; ++t) {
                float* pr = L.probs.data() + ((size_t)h * T + t) * T;
                float mx = -1e30f;
                const float* qr = L.q.data() + ((size_t)t * c.heads + h) * hd;
                for (int s = 0; s <= t; ++s) {
                    const float* kr = L.k.data() + ((size_t)s * c.kv_heads + kh) * hd;
                    float dot = 0.0f;
                    for (int i = 0; i < hd; ++i) dot += qr[i] * kr[i];
                    pr[s] = dot * scale;
                    mx = std::max(mx, pr[s]);
                }
                float sum = 0.0f;
                for (int s = 0; s <= t; ++s) { pr[s] = std::exp(pr[s] - mx); sum += pr[s]; }
                float inv = 1.0f / sum;
                float* ao = L.attn_out.data() + ((size_t)t * c.heads + h) * hd;
                for (int s = 0; s <= t; ++s) {
                    pr[s] *= inv;
                    const float* vr = L.v.data() + ((size_t)s * c.kv_heads + kh) * hd;
                    for (int i = 0; i < hd; ++i) ao[i] += pr[s] * vr[i];
                }
            }
        }
        std::vector<float> proj((size_t)T * H);
        linear_fwd(L.attn_out.data(), p.w.at(ln(l, "wo")), proj.data(), T, Hq, H);
        L.x_res.resize((size_t)T * H);
        for (size_t i = 0; i < (size_t)T * H; ++i) L.x_res[i] = x[i] + proj[i];
        L.n2.resize((size_t)T * H); L.rms2.resize(T);
        rmsnorm_fwd(L.x_res.data(), p.w.at(ln(l, "norm2")).d.data(),
                    L.n2.data(), L.rms2.data(), T, H, c.rms_eps);
        bool moe = c.moe_experts > 0 && (l % c.moe_layer_interval == 0);
        std::fill(proj.begin(), proj.end(), 0.0f);
        if (!moe) {
            L.fa.resize((size_t)T * c.inter); L.fb.resize((size_t)T * c.inter);
            L.fh.resize((size_t)T * c.inter);
            linear_fwd(L.n2.data(), p.w.at(ln(l, "w1")), L.fa.data(), T, H, c.inter);
            linear_fwd(L.n2.data(), p.w.at(ln(l, "w3")), L.fb.data(), T, H, c.inter);
            for (size_t i = 0; i < L.fh.size(); ++i) L.fh[i] = silu_f(L.fa[i]) * L.fb[i];
            linear_fwd(L.fh.data(), p.w.at(ln(l, "w2")), proj.data(), T, c.inter, H);
        } else {
            const int E = c.moe_experts, K = c.moe_top_k;
            const int EI = c.expert_inter();
            L.gate_logits.resize((size_t)T * E);
            linear_fwd(L.n2.data(), p.w.at(ln(l, "gate")), L.gate_logits.data(), T, H, E);
            L.gate_probs.resize((size_t)T * E);
            L.moe_idx.resize((size_t)T * K); L.moe_w.resize((size_t)T * K);
            L.mfa.resize((size_t)T * K); L.mfb.resize((size_t)T * K); L.mfh.resize((size_t)T * K);
            float aux = 0.0f;
            for (int t = 0; t < T; ++t) {
                const float* gl = L.gate_logits.data() + (size_t)t * E;
                float mx = *std::max_element(gl, gl + E), sum = 0.0f;
                float* gp = L.gate_probs.data() + (size_t)t * E;
                for (int e = 0; e < E; ++e) { gp[e] = std::exp(gl[e] - mx); sum += gp[e]; }
                for (int e = 0; e < E; ++e) gp[e] /= sum;
                std::vector<int> idx(E);
                std::iota(idx.begin(), idx.end(), 0);
                std::partial_sort(idx.begin(), idx.begin() + K, idx.end(),
                                  [&](int a, int b) { return gp[a] > gp[b]; });
                float wsum = 0.0f;
                for (int s = 0; s < K; ++s) wsum += gp[idx[s]];
                const float* xr = L.n2.data() + (size_t)t * H;
                for (int s = 0; s < K; ++s) {
                    int e = idx[s];
                    float wgt = gp[e] / wsum;
                    L.moe_idx[(size_t)t * K + s] = e;
                    L.moe_w[(size_t)t * K + s] = wgt;
                    std::string b = ln(l, "experts.") + std::to_string(e) + ".";
                    auto& fa = L.mfa[(size_t)t * K + s];
                    auto& fb = L.mfb[(size_t)t * K + s];
                    auto& fh = L.mfh[(size_t)t * K + s];
                    fa.resize(EI); fb.resize(EI); fh.resize(EI);
                    linear_fwd(xr, p.w.at(b + "w1"), fa.data(), 1, H, EI);
                    linear_fwd(xr, p.w.at(b + "w3"), fb.data(), 1, H, EI);
                    for (int i = 0; i < EI; ++i) fh[i] = silu_f(fa[i]) * fb[i];
                    std::vector<float> eo(H);
                    linear_fwd(fh.data(), p.w.at(b + "w2"), eo.data(), 1, EI, H);
                    for (int i = 0; i < H; ++i) proj[(size_t)t * H + i] += wgt * eo[i];
                }
                for (int s = 0; s < K; ++s) aux += (gp[idx[s]] / wsum) * (1.0f / K);
            }
            // Shared experts (v26): always-on SwiGLU, weight 1.0 — mirrors
            // the engine's `output + shared(x)` residual contribution.
            const int SI = c.shared_inter();
            L.sfa.resize((size_t)c.moe_shared_experts);
            L.sfb.resize((size_t)c.moe_shared_experts);
            L.sfh.resize((size_t)c.moe_shared_experts);
            for (int se = 0; se < c.moe_shared_experts; ++se) {
                std::string b = ln(l, "shared.") + std::to_string(se) + ".";
                auto& fa = L.sfa[(size_t)se];
                auto& fb = L.sfb[(size_t)se];
                auto& fh = L.sfh[(size_t)se];
                fa.resize((size_t)T * SI); fb.resize((size_t)T * SI);
                fh.resize((size_t)T * SI);
                linear_fwd(L.n2.data(), p.w.at(b + "w1"), fa.data(), T, H, SI);
                linear_fwd(L.n2.data(), p.w.at(b + "w3"), fb.data(), T, H, SI);
                for (size_t i = 0; i < fh.size(); ++i) fh[i] = silu_f(fa[i]) * fb[i];
                std::vector<float> so((size_t)T * H);
                linear_fwd(fh.data(), p.w.at(b + "w2"), so.data(), T, SI, H);
                for (size_t i = 0; i < so.size(); ++i) proj[i] += so[i];
            }
            o.moe_aux += c.moe_aux_w * c.moe_experts * aux / std::max(1, T);
        }
        for (size_t i = 0; i < (size_t)T * H; ++i) x[i] = L.x_res[i] + proj[i];
    }
    o.x_fin = x;
    o.hidden.resize((size_t)T * H); o.rmsf.resize(T);
    rmsnorm_fwd(x.data(), p.w.at("norm_f").d.data(),
                o.hidden.data(), o.rmsf.data(), T, H, c.rms_eps);
    o.logits.resize((size_t)T * c.vocab);
    linear_fwd(o.hidden.data(), p.w.at("lm_head"), o.logits.data(), T, H, c.vocab);
}

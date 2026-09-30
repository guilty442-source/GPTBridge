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
static inline float softplus_f(float x) {
    return x > 20.0f ? x : std::log1p(std::exp(x));
}
static inline float sigmoid_f(float x) { return 1.0f / (1.0f + std::exp(-x)); }

// HF rotate_half convention over the first `rd` channels only (partial
// rotary). rd must be even; channels >= rd pass through. inverse runs the
// transpose (backward / inverse rotation).
static void rope_hf_partial(float* v, int T, int nh, int hd, int rd,
                            float theta, bool inverse) {
    const int half = rd / 2;
    for (int t = 0; t < T; ++t)
        for (int h = 0; h < nh; ++h) {
            float* r = v + ((size_t)t * nh + h) * hd;
            for (int i = 0; i < half; ++i) {
                float fr = std::pow(theta, -(float)(2 * i) / (float)rd);
                float c = std::cos(t * fr), s = std::sin(t * fr);
                if (inverse) s = -s;
                float a = r[i], b = r[i + half];
                r[i] = a * c - b * s;
                r[i + half] = a * s + b * c;
            }
        }
}

// Depthwise causal conv1d (kernel K, no bias) + SiLU over flat [T, D]
// activations; out_pre keeps the pre-activation for backward.
static void conv1d_causal_fwd(const float* x, const float* w, float* y,
                              float* y_pre, int T, int D, int K) {
    for (int t = 0; t < T; ++t)
        for (int c = 0; c < D; ++c) {
            float s = 0.0f;
            for (int j = 0; j < K && t - j >= 0; ++j)
                s += w[(size_t)c * K + j] * x[(size_t)(t - j) * D + c];
            float v = silu_f(s);
            if (y_pre) y_pre[(size_t)t * D + c] = s;
            y[(size_t)t * D + c] = v;
        }
}

static void conv1d_causal_bwd(const float* dy, const float* y_pre,
                              const float* x, const float* w,
                              float* dx, float* dw, int T, int D, int K) {
    std::vector<float> dpre((size_t)T * D);
    for (size_t i = 0; i < (size_t)T * D; ++i) {
        float s = sigmoid_f(y_pre[i]);
        dpre[i] = dy[i] * s * (1.0f + y_pre[i] * (1.0f - s));
    }
    for (int t = 0; t < T; ++t)
        for (int c = 0; c < D; ++c) {
            float d = dpre[(size_t)t * D + c];
            for (int j = 0; j < K && t - j >= 0; ++j) {
                dw[(size_t)c * K + j] += d * x[(size_t)(t - j) * D + c];
                dx[(size_t)(t - j) * D + c] += d * w[(size_t)c * K + j];
            }
        }
}

static void l2norm_fwd(float* v, int rows, int dim, float eps,
                       float* norms_out) {
    for (int t = 0; t < rows; ++t) {
        float* r = v + (size_t)t * dim;
        float ss = 0.0f;
        for (int i = 0; i < dim; ++i) ss += r[i] * r[i];
        float n = std::sqrt(ss + eps);
        if (norms_out) norms_out[t] = n;
        float inv = 1.0f / n;
        for (int i = 0; i < dim; ++i) r[i] *= inv;
    }
}

// L2-norm backward: y = x/||x|| → dx = (dy − ŷ(ŷ·dy)) / ||x||.
// norms holds the forward ||x|| per row.
static void l2norm_bwd(const float* dy, const float* x_normed,
                       const float* norms, float* dx, int rows, int dim) {
    for (int t = 0; t < rows; ++t) {
        const float* xr = x_normed + (size_t)t * dim;
        const float* dr = dy + (size_t)t * dim;
        float dot = 0.0f;
        for (int i = 0; i < dim; ++i) dot += xr[i] * dr[i];
        float inv = 1.0f / norms[t];
        for (int i = 0; i < dim; ++i)
            dx[(size_t)t * dim + i] = (dr[i] - xr[i] * dot) * inv;
    }
}

// --------------------------------------------------------------- forward --

struct LayerCache {
    std::vector<float> x_in, n1, rms1;       // attention block
    std::vector<float> q, k, v, probs, attn_out, x_res;
    std::vector<float> attn_gate;            // [T*Hq] raw gate logits (attn_output_gate)
    std::vector<float> attn_gated;           // [T*Hq] sigmoid(gate) ⊙ attn_out
    std::vector<float> qk_qraw, qk_kraw;     // pre qk_norm q/k (for bwd)
    std::vector<float> qk_qrms, qk_krms;     // per (t,h) rms factors
    // gated deltanet (linear attention) caches
    std::vector<float> lin_conv_in;          // [T*conv_dim] flat q|k|v pre-conv
    std::vector<float> lin_conv_pre;         // [T*conv_dim] pre-SiLU
    std::vector<float> lin_qn, lin_kn;       // post-l2norm q,k [T*vh*kd] (v-head expanded)
    std::vector<float> lin_qrms, lin_krms;   // [T*kh] pre-norm lengths
    std::vector<float> lin_v;                // [T*vh*vd]
    std::vector<float> lin_a_raw;            // [T*vh] a + dt_bias (pre-softplus)
    std::vector<float> lin_b_raw;            // [T*vh] pre-sigmoid beta arg
    std::vector<float> lin_z;                // [T*vh*vd] pre-SiLU gate
    std::vector<float> lin_on;               // gated output (out_proj input) [T*vh*vd]
    std::vector<float> lin_onorm;            // pre-gate rmsnorm output [T*vh*vd]
    std::vector<float> lin_orms;             // [T*vh] per (t,h) rms factors
    std::vector<float> lin_o;                // pre-norm scan output [T*vh*vd]
    std::vector<float> lin_S;                // [(T+1)*vh*kd*vd] state snapshots
    std::vector<float> lin_decay;            // [T*vh] exp(g_t) per step
    std::vector<float> n2, rms2;             // ffn block
    std::vector<float> shared_gate_sig;      // [T] sigmoid(shared_gate@n2)
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
    // Vision early-fusion: raw prefix patches + count. T (all row counts
    // above) includes these P rows when present; labels carry -100 there.
    std::vector<float> vision_in;  // [P*D]
    int vision_patches = 0;
};

static void fwd(const Params& p, const ModelConfig& c,
                const std::vector<int>& ids, Fwd& o,
                const std::vector<float>* vision = nullptr,
                int vision_count = 0) {
    const int PT = (int)ids.size();
    int P = 0;
    if (vision != nullptr) {
        if (!c.use_vision) throw "vision: model has use_vision=false";
        P = vision_count;
        if (P <= 0 || P > c.vision_max_patches) throw "vision: bad patch count";
        if ((int)vision->size() != P * c.vision_patch_dim)
            throw "vision: bad patch data";
    }
    const int T = P + PT;
    const int H = c.hidden, hd = H / c.heads;
    const int Hq = c.heads * hd, Hkv = c.kv_heads * hd;
    std::vector<float> x((size_t)T * H);
    if (P > 0) {
        linear_fwd(vision->data(), p.w.at("vision.patch_proj"),
                   x.data(), P, c.vision_patch_dim, H);
        o.vision_in = *vision;
        o.vision_patches = P;
    } else {
        o.vision_in.clear();
        o.vision_patches = 0;
    }
    for (int t = 0; t < PT; ++t) {
        const float* er = p.w.at("embed").d.data() + (size_t)ids[t] * H;
        std::copy(er, er + H, x.data() + (size_t)(P + t) * H);
    }
    o.layers.resize(c.layers);
    for (int l = 0; l < c.layers; ++l) {
        LayerCache& L = o.layers[l];
        L.x_in = x;
        L.n1.resize((size_t)T * H); L.rms1.resize(T);
        rmsnorm_fwd(x.data(), p.w.at(ln(l, "norm1")).d.data(),
                    L.n1.data(), L.rms1.data(), T, H, c.rms_eps);
        std::vector<float> proj((size_t)T * H);
        if (c.is_linear(l)) {
            // ----- Qwen3.5 gated deltanet (linear attention) -----
            const int kd = c.lin_key_dim, vd = c.lin_value_dim;
            const int kh = c.lin_key_heads, vh = c.lin_value_heads;
            const int ratio = vh / kh;
            const int key_dim = kh * kd, val_dim = vh * vd;
            const int conv_dim = key_dim * 2 + val_dim;
            const int group_sz = 2 * kd + vd * ratio;
            const std::string lb = ln(l, "lin.");
            // in_proj_qkv rows are grouped per key head [q|k|v-group];
            // unpack into flat conv layout [q_flat | k_flat | v_flat].
            std::vector<float> qkvz((size_t)T * kh * group_sz);
            linear_fwd(L.n1.data(), p.w.at(lb + "in_proj_qkv"),
                       qkvz.data(), T, H, kh * group_sz);
            L.lin_conv_in.resize((size_t)T * conv_dim);
            for (int t = 0; t < T; ++t) {
                const float* src = qkvz.data() + (size_t)t * kh * group_sz;
                float* dst = L.lin_conv_in.data() + (size_t)t * conv_dim;
                for (int g = 0; g < kh; ++g) {
                    const float* gr = src + (size_t)g * group_sz;
                    std::copy(gr, gr + kd, dst + (size_t)g * kd);
                    std::copy(gr + kd, gr + 2 * kd,
                              dst + key_dim + (size_t)g * kd);
                    std::copy(gr + 2 * kd, gr + group_sz,
                              dst + key_dim * 2 + (size_t)g * (vd * ratio));
                }
            }
            L.lin_conv_pre.resize((size_t)T * conv_dim);
            std::vector<float> conv_out((size_t)T * conv_dim);
            conv1d_causal_fwd(L.lin_conv_in.data(),
                              p.w.at(lb + "conv1d").d.data(), conv_out.data(),
                              L.lin_conv_pre.data(), T, conv_dim,
                              c.lin_conv_kernel);
            // split conv output → per-v-head-expanded q,k / v
            L.lin_qn.resize((size_t)T * vh * kd);
            L.lin_kn.resize((size_t)T * vh * kd);
            L.lin_v.resize((size_t)T * vd * vh);
            std::vector<float> qraw((size_t)T * vh * kd),
                               kraw((size_t)T * vh * kd);
            for (int t = 0; t < T; ++t) {
                const float* cr = conv_out.data() + (size_t)t * conv_dim;
                for (int g = 0; g < kh; ++g) {
                    for (int r = 0; r < ratio; ++r) {
                        int h = g * ratio + r;
                        std::copy(cr + (size_t)g * kd, cr + (size_t)(g + 1) * kd,
                                  qraw.data() + ((size_t)t * vh + h) * kd);
                        std::copy(cr + key_dim + (size_t)g * kd,
                                  cr + key_dim + (size_t)(g + 1) * kd,
                                  kraw.data() + ((size_t)t * vh + h) * kd);
                    }
                }
                std::copy(cr + key_dim * 2, cr + conv_dim,
                          L.lin_v.data() + (size_t)t * val_dim);
            }
            L.lin_qrms.resize((size_t)T * vh); L.lin_krms.resize((size_t)T * vh);
            std::copy(qraw.begin(), qraw.end(), L.lin_qn.begin());
            std::copy(kraw.begin(), kraw.end(), L.lin_kn.begin());
            l2norm_fwd(L.lin_qn.data(), T * vh, kd, 1e-6f, L.lin_qrms.data());
            l2norm_fwd(L.lin_kn.data(), T * vh, kd, 1e-6f, L.lin_krms.data());
            const float qscale = 1.0f / std::sqrt((float)kd);
            for (auto& x : L.lin_qn) x *= qscale;
            L.lin_z.resize((size_t)T * val_dim);
            linear_fwd(L.n1.data(), p.w.at(lb + "in_proj_z"),
                       L.lin_z.data(), T, H, val_dim);
            L.lin_a_raw.resize((size_t)T * vh);
            L.lin_b_raw.resize((size_t)T * vh);
            linear_fwd(L.n1.data(), p.w.at(lb + "in_proj_a"),
                       L.lin_a_raw.data(), T, H, vh);
            linear_fwd(L.n1.data(), p.w.at(lb + "in_proj_b"),
                       L.lin_b_raw.data(), T, H, vh);
            const float* A_log = p.w.at(lb + "A_log").d.data();
            const float* dt_bias = p.w.at(lb + "dt_bias").d.data();
            L.lin_decay.resize((size_t)T * vh);
            std::vector<float> beta((size_t)T * vh);
            for (int t = 0; t < T; ++t)
                for (int h = 0; h < vh; ++h) {
                    float ar = L.lin_a_raw[(size_t)t * vh + h] + dt_bias[h];
                    L.lin_a_raw[(size_t)t * vh + h] = ar;
                    float g = -std::exp(A_log[h]) * softplus_f(ar);
                    L.lin_decay[(size_t)t * vh + h] = std::exp(g);
                    beta[(size_t)t * vh + h] =
                        sigmoid_f(L.lin_b_raw[(size_t)t * vh + h]);
                }
            // recurrent scan (fp32 state, HF torch_recurrent_gated_delta_rule)
            L.lin_S.assign((size_t)(T + 1) * vh * kd * vd, 0.0f);
            L.lin_o.resize((size_t)T * vh * vd);
            const size_t ssz = (size_t)kd * vd;
            for (int h = 0; h < vh; ++h)
                for (int t = 0; t < T; ++t) {
                    float* S = L.lin_S.data() + ((size_t)t * vh + h) * ssz;
                    float* Sp = L.lin_S.data() + ((size_t)(t + 1) * vh + h) * ssz;
                    float dec = L.lin_decay[(size_t)t * vh + h];
                    const float* kr = L.lin_kn.data() + ((size_t)t * vh + h) * kd;
                    const float* vr = L.lin_v.data() + ((size_t)t * vh + h) * vd;
                    const float* qr = L.lin_qn.data() + ((size_t)t * vh + h) * kd;
                    float bt = beta[(size_t)t * vh + h];
                    for (size_t i = 0; i < ssz; ++i) Sp[i] = S[i] * dec;
                    std::vector<float> u(vd);
                    for (int i = 0; i < vd; ++i) {
                        float kv = 0.0f;
                        for (int d = 0; d < kd; ++d)
                            kv += Sp[(size_t)d * vd + i] * kr[d];
                        u[i] = (vr[i] - kv) * bt;
                    }
                    for (int d = 0; d < kd; ++d)
                        for (int i = 0; i < vd; ++i)
                            Sp[(size_t)d * vd + i] += kr[d] * u[i];
                    float* orow = L.lin_o.data() + ((size_t)t * vh + h) * vd;
                    for (int i = 0; i < vd; ++i) {
                        float s = 0.0f;
                        for (int d = 0; d < kd; ++d)
                            s += Sp[(size_t)d * vd + i] * qr[d];
                        orow[i] = s;
                    }
                }
            // gated RMSNorm per v-head then SiLU(z) gate
            L.lin_on.resize((size_t)T * val_dim);
            L.lin_onorm.resize((size_t)T * val_dim);
            L.lin_orms.resize((size_t)T * vh);
            for (int t = 0; t < T; ++t)
                for (int h = 0; h < vh; ++h) {
                    const float* or_ = L.lin_o.data() + ((size_t)t * vh + h) * vd;
                    float* on = L.lin_onorm.data() + ((size_t)t * vh + h) * vd;
                    float rms;
                    rmsnorm_fwd(or_, p.w.at(lb + "norm").d.data(), on, &rms,
                                1, vd, c.rms_eps);
                    L.lin_orms[(size_t)t * vh + h] = rms;
                    const float* zr = L.lin_z.data() + ((size_t)t * vh + h) * vd;
                    float* og = L.lin_on.data() + ((size_t)t * vh + h) * vd;
                    for (int i = 0; i < vd; ++i) og[i] = on[i] * silu_f(zr[i]);
                }
            linear_fwd(L.lin_on.data(), p.w.at(lb + "out_proj"),
                       proj.data(), T, val_dim, H);
        } else {
            // ----- full attention (optional qk_norm / output gate /
            // partial rotary — all three gate Qwen3.5 parity) -----
            const int qmul = c.attn_output_gate ? 2 : 1;
            std::vector<float> qfused;
            if (c.attn_output_gate) {
                qfused.resize((size_t)T * Hq * 2);
                linear_fwd(L.n1.data(), p.w.at(ln(l, "wq")),
                           qfused.data(), T, H, Hq * 2);
                L.q.resize((size_t)T * Hq);
                L.attn_gate.resize((size_t)T * Hq);
                for (int t = 0; t < T; ++t)
                    for (int h = 0; h < c.heads; ++h) {
                        const float* fr = qfused.data() +
                            ((size_t)t * c.heads + h) * (size_t)hd * 2;
                        std::copy(fr, fr + hd,
                                  L.q.data() + ((size_t)t * c.heads + h) * hd);
                        std::copy(fr + hd, fr + 2 * hd,
                                  L.attn_gate.data() +
                                      ((size_t)t * c.heads + h) * hd);
                    }
            } else {
                L.q.resize((size_t)T * Hq);
                linear_fwd(L.n1.data(), p.w.at(ln(l, "wq")),
                           L.q.data(), T, H, Hq);
            }
            L.k.resize((size_t)T * Hkv); L.v.resize((size_t)T * Hkv);
            linear_fwd(L.n1.data(), p.w.at(ln(l, "wk")), L.k.data(), T, H, Hkv);
            linear_fwd(L.n1.data(), p.w.at(ln(l, "wv")), L.v.data(), T, H, Hkv);
            if (c.qk_norm) {
                L.qk_qraw = L.q; L.qk_kraw = L.k;
                L.qk_qrms.resize((size_t)T * c.heads);
                L.qk_krms.resize((size_t)T * c.kv_heads);
                for (int t = 0; t < T; ++t) {
                    for (int h = 0; h < c.heads; ++h) {
                        float* qr = L.q.data() + ((size_t)t * c.heads + h) * hd;
                        float rms;
                        rmsnorm_fwd(qr, p.w.at(ln(l, "q_norm")).d.data(), qr,
                                    &rms, 1, hd, c.rms_eps);
                        L.qk_qrms[(size_t)t * c.heads + h] = rms;
                    }
                    for (int h = 0; h < c.kv_heads; ++h) {
                        float* kr = L.k.data() + ((size_t)t * c.kv_heads + h) * hd;
                        float rms;
                        rmsnorm_fwd(kr, p.w.at(ln(l, "k_norm")).d.data(), kr,
                                    &rms, 1, hd, c.rms_eps);
                        L.qk_krms[(size_t)t * c.kv_heads + h] = rms;
                    }
                }
            }
            const int rd = c.rotary_dim();
            if (rd < hd) {
                rope_hf_partial(L.q.data(), T, c.heads, hd, rd,
                                c.rope_theta, false);
                rope_hf_partial(L.k.data(), T, c.kv_heads, hd, rd,
                                c.rope_theta, false);
            } else {
                rope(L.q.data(), T, c.heads, hd, c.rope_theta, false);
                rope(L.k.data(), T, c.kv_heads, hd, c.rope_theta, false);
            }
            int group = c.heads / c.kv_heads;
            float scale = 1.0f / std::sqrt((float)hd);
            L.probs.assign((size_t)c.heads * T * T, 0.0f);
            L.attn_out.assign((size_t)T * Hq, 0.0f);
            for (int h = 0; h < c.heads; ++h) {
                int kh2 = h / group;
                for (int t = 0; t < T; ++t) {
                    float* pr = L.probs.data() + ((size_t)h * T + t) * T;
                    float mx = -1e30f;
                    const float* qr = L.q.data() + ((size_t)t * c.heads + h) * hd;
                    for (int s = 0; s <= t; ++s) {
                        const float* kr = L.k.data() + ((size_t)s * c.kv_heads + kh2) * hd;
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
                        const float* vr = L.v.data() + ((size_t)s * c.kv_heads + kh2) * hd;
                        for (int i = 0; i < hd; ++i) ao[i] += pr[s] * vr[i];
                    }
                }
            }
            const float* wo_in = L.attn_out.data();
            if (c.attn_output_gate) {
                L.attn_gated.resize(L.attn_out.size());
                for (size_t i = 0; i < L.attn_out.size(); ++i)
                    L.attn_gated[i] =
                        L.attn_out[i] * sigmoid_f(L.attn_gate[i]);
                wo_in = L.attn_gated.data();
            }
            linear_fwd(wo_in, p.w.at(ln(l, "wo")), proj.data(), T, Hq, H);
        }
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
                                  [&](int a, int b) {
                                      // Deterministic tie-break mirrors the
                                      // inference engine (stable_sort, ties
                                      // keep lower expert index first).
                                      if (gp[a] != gp[b]) return gp[a] > gp[b];
                                      return a < b;
                                  });
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
            // v27: optional sigmoid gate on the shared output
            // (Qwen3.5 `shared_expert_gate`): proj += sigmoid(Wx) * shared.
            const int SI = c.shared_inter();
            if (c.shared_expert_gate && c.moe_shared_experts > 0) {
                L.shared_gate_sig.resize((size_t)T);
                std::vector<float> sg((size_t)T);
                linear_fwd(L.n2.data(), p.w.at(ln(l, "shared_gate")),
                           sg.data(), T, H, 1);
                for (int t = 0; t < T; ++t)
                    L.shared_gate_sig[(size_t)t] = sigmoid_f(sg[(size_t)t]);
            }
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
                if (L.shared_gate_sig.empty())
                    for (size_t i = 0; i < so.size(); ++i) proj[i] += so[i];
                else
                    for (int t = 0; t < T; ++t) {
                        float g = L.shared_gate_sig[(size_t)t];
                        for (int i = 0; i < H; ++i)
                            proj[(size_t)t * H + i] += g * so[(size_t)t * H + i];
                    }
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

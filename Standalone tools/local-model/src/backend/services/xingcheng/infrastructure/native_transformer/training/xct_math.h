// xct_math.h — B94 fragment of xingcheng_trainer.cpp (primitives + forward).
// Included once by xingcheng_trainer.cpp inside namespace xct.
#pragma once

// ------------------------------------------------------------- primitives --

// TPU-cluster lanes: both wrappers ride the tiled disjoint-partition
// kernels in xct_tpu.h (flat output-space lanes for fwd; split dW/dx row
// partitions for bwd). Per-element accumulation order is unchanged, so
// results are identical for any lane count; SIMD only reassociates the
// inner dot product.
static void linear_fwd(const float* x, const Tensor& W, float* y,
                       int T, int I, int O) {
    tpu_linear(x, W.d.data(), y, T, I, O);
}

static void linear_bwd(const float* dy, const float* x, const Tensor& W,
                       float* dx, float* dW, int T, int I, int O) {
    tpu_linear_bwd(dy, x, W.d.data(), dx, dW, T, I, O);
}

static void rmsnorm_fwd(const float* x, const float* w, float* y, float* rms,
                        int T, int H, float eps) {
    parallel_for(T, [&](int64_t b, int64_t e) {
        for (int64_t t = b; t < e; ++t) {
            const float* xr = x + (size_t)t * H;
            float ss = tpu_dot(xr, xr, H);
            float r = std::sqrt(ss / H + eps);
            rms[t] = r;
            float inv = 1.0f / r;
            float* yr = y + (size_t)t * H;
            for (int i = 0; i < H; ++i) yr[i] = xr[i] * inv * w[i];
        }
    });
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

// RoPE cos/sin tables: angle(t,i) = t * theta^(-2i/dim) — identical
// operands to the per-element pow() form, computed once per (T, dim,
// theta) instead of per element. Table bounded to keep memory sane.
struct RopeCs { std::vector<float> c, s; };
static const RopeCs& rope_cs(int T, int dim, float theta) {
    static int cT = -1, cd = -1;
    static float ct = 0.0f;
    static RopeCs tab;
    if (cT != T || cd != dim || ct != theta) {
        const int half = dim / 2;
        tab.c.assign((size_t)T * half, 0.0f);
        tab.s.assign((size_t)T * half, 0.0f);
        for (int i = 0; i < half; ++i) {
            float fr = std::pow(theta, -(float)(2 * i) / (float)dim);
            for (int t = 0; t < T; ++t) {
                tab.c[(size_t)t * half + i] = std::cos(t * fr);
                tab.s[(size_t)t * half + i] = std::sin(t * fr);
            }
        }
        cT = T; cd = dim; ct = theta;
    }
    return tab;
}

static void rope(float* v, int T, int nh, int hd, float theta, bool inverse) {
    const RopeCs& cs = rope_cs(T, hd, theta);
    for (int t = 0; t < T; ++t)
        for (int h = 0; h < nh; ++h) {
            float* r = v + ((size_t)t * nh + h) * hd;
            for (int i = 0; i + 1 < hd; i += 2) {
                float c = cs.c[(size_t)t * (hd / 2) + i / 2];
                float s = cs.s[(size_t)t * (hd / 2) + i / 2];
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

// GeGLU activation (Gemma FFN): tanh-approximated gelu gate.
static inline float gelu_tanh_f(float x) {
    const float c0 = 0.7978845608028654f, c1 = 0.044715f;
    return 0.5f * x * (1.0f + std::tanh(c0 * (x + c1 * x * x * x)));
}
static inline float gelu_tanh_df(float x) {
    const float c0 = 0.7978845608028654f, c1 = 0.044715f;
    float u = c0 * (x + c1 * x * x * x), t = std::tanh(u);
    return 0.5f * (1.0f + t) +
           0.5f * x * (1.0f - t * t) * c0 * (1.0f + 3.0f * c1 * x * x);
}
// FFN gate dispatch: act=0 SwiGLU (silu), act=1 GeGLU (gelu_tanh).
static inline float gate_act_f(float x, int act) {
    return act ? gelu_tanh_f(x) : silu_f(x);
}
static inline float gate_act_df(float x, int act) {
    if (act) return gelu_tanh_df(x);
    float s = sigmoid_f(x);
    return s * (1.0f + x * (1.0f - s));
}

// HF rotate_half convention over the first `rd` channels only (partial
// rotary). rd must be even; channels >= rd pass through. inverse runs the
// transpose (backward / inverse rotation).
static void rope_hf_partial(float* v, int T, int nh, int hd, int rd,
                            float theta, bool inverse) {
    const int half = rd / 2;
    const RopeCs& cs = rope_cs(T, rd, theta);
    for (int t = 0; t < T; ++t)
        for (int h = 0; h < nh; ++h) {
            float* r = v + ((size_t)t * nh + h) * hd;
            for (int i = 0; i < half; ++i) {
                float c = cs.c[(size_t)t * half + i];
                float s = cs.s[(size_t)t * half + i];
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
    std::vector<float> attn_proj;            // post_attn_norm: pre-norm proj [T*H]
    std::vector<float> post_attn;            // rmsnorm(attn_proj) [T*H]
    std::vector<float> post_attn_rms;        // [T]
    std::vector<float> ffn_proj;             // post_ffw_norm: pre-norm proj [T*H]
    std::vector<float> post_ffn;             // rmsnorm(ffn_proj) [T*H]
    std::vector<float> post_ffn_rms;         // [T]
    std::vector<float> qk_qraw, qk_kraw;     // pre qk_norm q/k (for bwd)
    std::vector<float> qk_qrms, qk_krms;     // per (t,h) rms factors
    // DeepSeek MLA caches (attention layers when kv_lora_rank > 0)
    std::vector<float> mla_ckv;              // [T*rank] normed kv latent
    std::vector<float> mla_ckv_raw;          // [T*rank] pre-norm latent
    std::vector<float> mla_ckv_rms;          // [T]
    std::vector<float> mla_cq;               // [T*q_rank] normed q latent
    std::vector<float> mla_cq_raw;           // [T*q_rank]
    std::vector<float> mla_cq_rms;           // [T]
    std::vector<float> mla_qn;               // [T*heads*kn] nope q
    std::vector<float> mla_qr;               // [T*heads*kr] rope q (post)
    std::vector<float> mla_kn;               // [T*heads*kn] nope k
    std::vector<float> mla_kr;               // [T*kr] shared rope k (post)
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

// DeepSeek MTP (depth-1): second-token-ahead prediction through one
// decoder block over text rows only. Shares embed/lm_head/norm contract
// with the trunk; patch-prefix rows never enter the MTP sequence.
struct MtpCache {
    bool on = false;
    std::vector<int> lab;            // [PT] ids[i+2] else -100
    std::vector<float> nh_src;       // [PT*H] pre-norm hidden inputs
    std::vector<float> nh_rms;       // [PT]
    std::vector<float> ne_src;       // [PT*H] pre-norm embed inputs
    std::vector<float> ne_rms;       // [PT]
    std::vector<int> ne_ids;         // [PT] embed row used (ids[i+1]|-1)
    std::vector<float> cin;          // [PT*2H] normed concat input
    std::vector<float> z;            // [PT*H] block input (w_proj out)
    LayerCache lc;                   // block internals (n1..fh)
    std::vector<float> res2;         // [PT*H] pre-norm_out residual
    std::vector<float> out;          // [PT*H] norm_out output
    std::vector<float> out_rms;      // [PT]
    std::vector<float> logits;       // [PT*V]
    float loss = 0.0f;               // lambda-weighted CE contribution
};

struct Fwd {
    std::vector<float> logits;   // [T,V]
    std::vector<float> hidden;   // post final norm [T,H]
    std::vector<float> x_fin;    // pre final norm [T,H]
    std::vector<float> rmsf;     // [T]
    std::vector<LayerCache> layers;
    float moe_aux = 0.0f;
    MtpCache mtp;                    // DeepSeek MTP module caches
    // Vision early-fusion: raw prefix patches + count. T (all row counts
    // above) includes these P rows when present; labels carry -100 there.
    std::vector<float> vision_in;  // [P*D]
    int vision_patches = 0;
};

static void mtp_fwd(const Params& p, const ModelConfig& c,
                    const std::vector<int>& ids, Fwd& o);

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
    const int Hq = c.heads * hd;
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
            tpu_scale(L.lin_qn.data(), qscale, (int64_t)L.lin_qn.size());
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
            // TPU lanes: heads are disjoint lanes; each step is expressed as
            // contiguous axpy/scale sweeps so SIMD tiles the state block.
            L.lin_S.assign((size_t)(T + 1) * vh * kd * vd, 0.0f);
            L.lin_o.resize((size_t)T * vh * vd);
            const size_t ssz = (size_t)kd * vd;
            parallel_for(vh, [&](int64_t hb, int64_t he) {
                std::vector<float> u(vd), kvm(vd);
                for (int64_t h = hb; h < he; ++h)
                for (int t = 0; t < T; ++t) {
                    float* S = L.lin_S.data() + ((size_t)t * vh + h) * ssz;
                    float* Sp = L.lin_S.data() + ((size_t)(t + 1) * vh + h) * ssz;
                    float dec = L.lin_decay[(size_t)t * vh + h];
                    const float* kr = L.lin_kn.data() + ((size_t)t * vh + h) * kd;
                    const float* vr = L.lin_v.data() + ((size_t)t * vh + h) * vd;
                    const float* qr = L.lin_qn.data() + ((size_t)t * vh + h) * kd;
                    float bt = beta[(size_t)t * vh + h];
                    tpu_scale_copy(Sp, S, dec, (int64_t)ssz);
                    std::fill(kvm.begin(), kvm.end(), 0.0f);
                    for (int d = 0; d < kd; ++d)
                        tpu_axpy(kvm.data(), kr[d], Sp + (size_t)d * vd, vd);
                    for (int i = 0; i < vd; ++i)
                        u[i] = (vr[i] - kvm[i]) * bt;
                    for (int d = 0; d < kd; ++d)
                        tpu_axpy(Sp + (size_t)d * vd, kr[d], u.data(), vd);
                    float* orow = L.lin_o.data() + ((size_t)t * vh + h) * vd;
                    std::fill(orow, orow + vd, 0.0f);
                    for (int d = 0; d < kd; ++d)
                        tpu_axpy(orow, qr[d], Sp + (size_t)d * vd, vd);
                }
            });
            // gated RMSNorm per v-head then SiLU(z) gate — disjoint (t,h)
            // rows ride the lane pool when large enough.
            L.lin_on.resize((size_t)T * val_dim);
            L.lin_onorm.resize((size_t)T * val_dim);
            L.lin_orms.resize((size_t)T * vh);
            parallel_for((int64_t)T * vh, [&](int64_t b, int64_t e) {
                for (int64_t th = b; th < e; ++th) {
                    const int t = (int)(th / vh), h = (int)(th % vh);
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
            });
            linear_fwd(L.lin_on.data(), p.w.at(lb + "out_proj"),
                       proj.data(), T, val_dim, H);
        } else if (c.is_mla(l)) {
            // ----- DeepSeek MLA (multi-head latent attention) -----
            // kv latent c = rmsnorm(W_dkv x); per-head nope keys and
            // values up-project from c; a single shared rope key head
            // carries position. q is direct or itself low-rank.
            const int kn = c.qk_nope_head_dim, kr = c.qk_rope_head_dim;
            const int rank = c.kv_lora_rank, qr = c.q_lora_rank;
            const std::string b = ln(l, "");
            const int win = c.is_local_attn(l) ? c.sliding_window : 0;
            L.mla_ckv_raw.resize((size_t)T * rank);
            linear_fwd(L.n1.data(), p.w.at(b + "w_dkv"),
                       L.mla_ckv_raw.data(), T, H, rank);
            L.mla_ckv.resize((size_t)T * rank);
            L.mla_ckv_rms.resize((size_t)T);
            rmsnorm_fwd(L.mla_ckv_raw.data(), p.w.at(b + "norm_kvl").d.data(),
                        L.mla_ckv.data(), L.mla_ckv_rms.data(), T, rank,
                        c.rms_eps);
            const int qd = kn + kr;
            std::vector<float> qf((size_t)T * c.heads * qd);
            if (qr > 0) {
                L.mla_cq_raw.resize((size_t)T * qr);
                linear_fwd(L.n1.data(), p.w.at(b + "w_dq"),
                           L.mla_cq_raw.data(), T, H, qr);
                L.mla_cq.resize((size_t)T * qr);
                L.mla_cq_rms.resize((size_t)T);
                rmsnorm_fwd(L.mla_cq_raw.data(),
                            p.w.at(b + "norm_ql").d.data(), L.mla_cq.data(),
                            L.mla_cq_rms.data(), T, qr, c.rms_eps);
                linear_fwd(L.mla_cq.data(), p.w.at(b + "w_uq"),
                           qf.data(), T, qr, c.heads * qd);
            } else {
                linear_fwd(L.n1.data(), p.w.at(b + "wq"),
                           qf.data(), T, H, c.heads * qd);
            }
            L.mla_qn.resize((size_t)T * c.heads * kn);
            L.mla_qr.resize((size_t)T * c.heads * kr);
            parallel_for(c.heads, [&](int64_t hb, int64_t he) {
                for (int64_t h = hb; h < he; ++h)
                    for (int t = 0; t < T; ++t) {
                        const float* fr = qf.data() +
                            ((size_t)t * c.heads + h) * (size_t)qd;
                        std::copy(fr, fr + kn, L.mla_qn.data() +
                                  ((size_t)t * c.heads + h) * kn);
                        std::copy(fr + kn, fr + qd, L.mla_qr.data() +
                                  ((size_t)t * c.heads + h) * kr);
                    }
            });
            L.mla_kn.resize((size_t)T * c.heads * kn);
            linear_fwd(L.mla_ckv.data(), p.w.at(b + "w_uk"),
                       L.mla_kn.data(), T, rank, c.heads * kn);
            L.v.resize((size_t)T * c.heads * hd);
            linear_fwd(L.mla_ckv.data(), p.w.at(b + "w_uv"),
                       L.v.data(), T, rank, c.heads * hd);
            L.mla_kr.resize((size_t)T * kr);
            linear_fwd(L.n1.data(), p.w.at(b + "w_kr"),
                       L.mla_kr.data(), T, H, kr);
            // decoupled rope: per-head q rope channels + the shared k
            // rope head rotate at the layer's theta.
            const float th = c.rope_theta_at(l);
            rope_hf_partial(L.mla_qr.data(), T, c.heads, kr, kr, th, false);
            rope_hf_partial(L.mla_kr.data(), T, 1, kr, kr, th, false);
            const float scale = 1.0f / std::sqrt((float)qd);
            L.probs.assign((size_t)c.heads * T * T, 0.0f);
            L.attn_out.assign((size_t)T * c.heads * hd, 0.0f);
            parallel_for(c.heads, [&](int64_t hb, int64_t he) {
                for (int64_t h = hb; h < he; ++h)
                for (int t = 0; t < T; ++t) {
                    float* pr = L.probs.data() + ((size_t)h * T + t) * T;
                    const int s0 = win > 0 ? std::max(0, t - win + 1) : 0;
                    float mx = -1e30f;
                    const float* qnr = L.mla_qn.data() +
                                       ((size_t)t * c.heads + h) * kn;
                    const float* qrr = L.mla_qr.data() +
                                       ((size_t)t * c.heads + h) * kr;
                    for (int s = s0; s <= t; ++s) {
                        const float* knr = L.mla_kn.data() +
                                           ((size_t)s * c.heads + h) * kn;
                        const float* krr = L.mla_kr.data() + (size_t)s * kr;
                        pr[s] = (tpu_dot(qnr, knr, kn) +
                                 tpu_dot(qrr, krr, kr)) * scale;
                        mx = std::max(mx, pr[s]);
                    }
                    float sum = 0.0f;
                    for (int s = s0; s <= t; ++s) {
                        pr[s] = std::exp(pr[s] - mx); sum += pr[s];
                    }
                    float inv = 1.0f / sum;
                    float* ao = L.attn_out.data() +
                                ((size_t)t * c.heads + h) * hd;
                    for (int s = s0; s <= t; ++s) {
                        pr[s] *= inv;
                        const float* vr = L.v.data() +
                                          ((size_t)s * c.heads + h) * hd;
                        tpu_axpy(ao, pr[s], vr, hd);
                    }
                }
            });
            linear_fwd(L.attn_out.data(), p.w.at(b + "wo"),
                       proj.data(), T, c.heads * hd, H);
            if (c.post_attn_norm) {
                L.attn_proj = proj;
                L.post_attn.resize((size_t)T * H);
                L.post_attn_rms.resize(T);
                rmsnorm_fwd(L.attn_proj.data(),
                            p.w.at(ln(l, "norm_attn_out")).d.data(),
                            L.post_attn.data(), L.post_attn_rms.data(),
                            T, H, c.rms_eps);
                proj = L.post_attn;
            }
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
            // Gemma axis: local layers keep the job kv fan-out inside a
            // sliding window; global layers may shrink kv heads and unify
            // K==V into one projection (wkv).
            const int kvh = c.kv_heads_at(l);
            const int Hkvl = kvh * hd;
            const bool loc = c.is_local_attn(l);
            L.k.resize((size_t)T * Hkvl); L.v.resize((size_t)T * Hkvl);
            if (c.kv_unified(l)) {
                linear_fwd(L.n1.data(), p.w.at(ln(l, "wkv")),
                           L.k.data(), T, H, Hkvl);
                L.v = L.k;                     // unified K == V
            } else {
                linear_fwd(L.n1.data(), p.w.at(ln(l, "wk")),
                           L.k.data(), T, H, Hkvl);
                linear_fwd(L.n1.data(), p.w.at(ln(l, "wv")),
                           L.v.data(), T, H, Hkvl);
            }
            if (c.qk_norm) {
                L.qk_qraw = L.q; L.qk_kraw = L.k;
                L.qk_qrms.resize((size_t)T * c.heads);
                L.qk_krms.resize((size_t)T * kvh);
                for (int t = 0; t < T; ++t) {
                    for (int h = 0; h < c.heads; ++h) {
                        float* qr = L.q.data() + ((size_t)t * c.heads + h) * hd;
                        float rms;
                        rmsnorm_fwd(qr, p.w.at(ln(l, "q_norm")).d.data(), qr,
                                    &rms, 1, hd, c.rms_eps);
                        L.qk_qrms[(size_t)t * c.heads + h] = rms;
                    }
                    for (int h = 0; h < kvh; ++h) {
                        float* kr = L.k.data() + ((size_t)t * kvh + h) * hd;
                        float rms;
                        rmsnorm_fwd(kr, p.w.at(ln(l, "k_norm")).d.data(), kr,
                                    &rms, 1, hd, c.rms_eps);
                        L.qk_krms[(size_t)t * kvh + h] = rms;
                    }
                }
            }
            // per-layer-type rotary: local full-rope / global p-RoPE,
            // independent base frequencies.
            const int rd = c.rotary_dim_at(l);
            const float th = c.rope_theta_at(l);
            if (rd < hd) {
                rope_hf_partial(L.q.data(), T, c.heads, hd, rd, th, false);
                rope_hf_partial(L.k.data(), T, kvh, hd, rd, th, false);
            } else {
                rope(L.q.data(), T, c.heads, hd, th, false);
                rope(L.k.data(), T, kvh, hd, th, false);
            }
            int group = c.heads / kvh;
            const int win = loc ? c.sliding_window : 0;
            float scale = 1.0f / std::sqrt((float)hd);
            L.probs.assign((size_t)c.heads * T * T, 0.0f);
            L.attn_out.assign((size_t)T * Hq, 0.0f);
            // TPU lanes: heads are disjoint lanes (probs per-h slice,
            // attn_out per-h column slice); per-element order unchanged.
            parallel_for(c.heads, [&](int64_t hb, int64_t he) {
                for (int64_t h = hb; h < he; ++h) {
                int kh2 = (int)h / group;
                for (int t = 0; t < T; ++t) {
                    float* pr = L.probs.data() + ((size_t)h * T + t) * T;
                    // local layers: causal + last-W window; global: causal.
                    const int s0 = win > 0 ? std::max(0, t - win + 1) : 0;
                    float mx = -1e30f;
                    const float* qr = L.q.data() + ((size_t)t * c.heads + h) * hd;
                    for (int s = s0; s <= t; ++s) {
                        const float* kr = L.k.data() + ((size_t)s * kvh + kh2) * hd;
                        pr[s] = tpu_dot(qr, kr, hd) * scale;
                        mx = std::max(mx, pr[s]);
                    }
                    float sum = 0.0f;
                    for (int s = s0; s <= t; ++s) { pr[s] = std::exp(pr[s] - mx); sum += pr[s]; }
                    float inv = 1.0f / sum;
                    float* ao = L.attn_out.data() + ((size_t)t * c.heads + h) * hd;
                    for (int s = s0; s <= t; ++s) {
                        pr[s] *= inv;
                        const float* vr = L.v.data() + ((size_t)s * kvh + kh2) * hd;
                        tpu_axpy(ao, pr[s], vr, hd);
                    }
                }
                }
            });
            const float* wo_in = L.attn_out.data();
            if (c.attn_output_gate) {
                L.attn_gated.resize(L.attn_out.size());
                tpu_elementwise((int64_t)L.attn_out.size(), [&](int64_t i) {
                    L.attn_gated[(size_t)i] =
                        L.attn_out[(size_t)i] * sigmoid_f(L.attn_gate[(size_t)i]);
                });
                wo_in = L.attn_gated.data();
            }
            linear_fwd(wo_in, p.w.at(ln(l, "wo")), proj.data(), T, Hq, H);
            if (c.post_attn_norm) {
                // Gemma sandwich norm: residual adds rmsnorm(attn_out).
                L.attn_proj = proj;
                L.post_attn.resize((size_t)T * H);
                L.post_attn_rms.resize(T);
                rmsnorm_fwd(L.attn_proj.data(),
                            p.w.at(ln(l, "norm_attn_out")).d.data(),
                            L.post_attn.data(), L.post_attn_rms.data(),
                            T, H, c.rms_eps);
                proj = L.post_attn;
            }
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
            tpu_elementwise((int64_t)L.fh.size(), [&](int64_t i) {
                L.fh[(size_t)i] = gate_act_f(L.fa[(size_t)i], c.ffn_act) *
                                  L.fb[(size_t)i];
            });
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
                float* gp = L.gate_probs.data() + (size_t)t * E;
                if (c.moe_router_sigmoid) {
                    // v28 fused scoring (Qwen3.5): per-expert sigmoid —
                    // no shared denominator, so scores stay scale-robust
                    // under many fine-grained experts. The deterministic
                    // top-k + renorm below is unchanged.
                    for (int e = 0; e < E; ++e) gp[e] = sigmoid_f(gl[e]);
                } else {
                    float mx = *std::max_element(gl, gl + E), sum = 0.0f;
                    for (int e = 0; e < E; ++e) { gp[e] = std::exp(gl[e] - mx); sum += gp[e]; }
                    for (int e = 0; e < E; ++e) gp[e] /= sum;
                }
                std::vector<int> idx(E);
                std::iota(idx.begin(), idx.end(), 0);
                // DeepSeek aux-free balance: selection ranks s+b (bias is
                // routing-time only); weights still come from s itself.
                const float* lbb = c.moe_auxfree_balance
                    ? p.w.at(ln(l, "lb_bias")).d.data() : nullptr;
                std::partial_sort(idx.begin(), idx.begin() + K, idx.end(),
                                  [&](int a, int b) {
                                      // Deterministic tie-break mirrors the
                                      // inference engine (stable_sort, ties
                                      // keep lower expert index first).
                                      float sa = gp[a], sb = gp[b];
                                      if (lbb) { sa += lbb[a]; sb += lbb[b]; }
                                      if (sa != sb) return sa > sb;
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
                    for (int i = 0; i < EI; ++i)
                        fh[i] = gate_act_f(fa[i], c.ffn_act) * fb[i];
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
                tpu_elementwise((int64_t)fh.size(), [&](int64_t i) {
                    fh[(size_t)i] = gate_act_f(fa[(size_t)i], c.ffn_act) *
                                    fb[(size_t)i];
                });
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
            // DeepSeek axis: aux-free balancing replaces the aux loss —
            // no gradient is added on the router for load shaping.
            if (!c.moe_auxfree_balance)
                o.moe_aux += c.moe_aux_w * c.moe_experts * aux / std::max(1, T);
        }
        if (c.post_ffw_norm) {
            // Gemma sandwich norm: residual adds rmsnorm(ffn_out).
            L.ffn_proj = proj;
            L.post_ffn.resize((size_t)T * H);
            L.post_ffn_rms.resize(T);
            rmsnorm_fwd(L.ffn_proj.data(),
                        p.w.at(ln(l, "norm_ffw_out")).d.data(),
                        L.post_ffn.data(), L.post_ffn_rms.data(),
                        T, H, c.rms_eps);
            proj = L.post_ffn;
        }
        for (size_t i = 0; i < (size_t)T * H; ++i) x[i] = L.x_res[i] + proj[i];
    }
    o.x_fin = x;
    o.hidden.resize((size_t)T * H); o.rmsf.resize(T);
    rmsnorm_fwd(x.data(), p.w.at("norm_f").d.data(),
                o.hidden.data(), o.rmsf.data(), T, H, c.rms_eps);
    o.logits.resize((size_t)T * c.vocab);
    linear_fwd(o.hidden.data(), p.w.at("lm_head"), o.logits.data(), T, H, c.vocab);
    if (c.final_logit_softcap > 0.0f) {
        // Gemma final logit softcapping: y = cap*tanh(z/cap). The backward
        // factor 1 - (y/cap)^2 is recoverable from the capped values, so no
        // pre-cap cache is kept.
        const float cap = c.final_logit_softcap, inv = 1.0f / cap;
        tpu_elementwise((int64_t)o.logits.size(), [&](int64_t i) {
            o.logits[(size_t)i] = cap * std::tanh(o.logits[(size_t)i] * inv);
        });
    }
    if (c.mtp_num_layers > 0) mtp_fwd(p, c, ids, o);
}

// ------------------------------------------------------ DeepSeek MTP -----

// ce_loss lives in xct_backward.h (same TU) — forward decl for mtp_fwd.
static float ce_loss(const std::vector<float>& logits,
                     const std::vector<int>& labels, int T, int V,
                     std::vector<float>& dlogits);

// Depth-1 multi-token prediction: each text row i predicts ids[i+2]
// through M[w_proj]·[norm_h(h_i) | norm_e(Emb(ids[i+1]))] → one decoder
// block (plain causal attention + dense FFN) → norm_out → shared head.
// Patch-prefix rows never enter the sequence; the trunk is unchanged.
static void mtp_fwd(const Params& p, const ModelConfig& c,
                    const std::vector<int>& ids, Fwd& o) {
    const int PT = (int)ids.size(), P = o.vision_patches;
    const int H = c.hidden, hd = H / c.heads;
    const int kvh = c.kv_heads, Hkvl = kvh * hd, Hq = c.heads * hd;
    MtpCache& M = o.mtp;
    M.on = true;
    LayerCache& L = M.lc;
    M.lab.resize((size_t)PT);
    M.cin.resize((size_t)PT * 2 * H);
    M.nh_src.resize((size_t)PT * H); M.nh_rms.resize((size_t)PT);
    M.ne_src.assign((size_t)PT * H, 0.0f); M.ne_rms.resize((size_t)PT);
    M.ne_ids.assign((size_t)PT, -1);
    for (int i = 0; i < PT; ++i) {
        M.lab[(size_t)i] = i + 2 < PT ? ids[(size_t)i + 2] : -100;
        std::copy(o.hidden.data() + (size_t)(P + i) * H,
                  o.hidden.data() + (size_t)(P + i + 1) * H,
                  M.nh_src.data() + (size_t)i * H);
        rmsnorm_fwd(M.nh_src.data() + (size_t)i * H,
                    p.w.at("mtp.norm_h").d.data(),
                    M.cin.data() + (size_t)i * 2 * H,
                    M.nh_rms.data() + i, 1, H, c.rms_eps);
        if (i + 1 < PT) {
            int nid = ids[(size_t)i + 1];
            M.ne_ids[(size_t)i] = nid;
            const float* er = p.w.at("embed").d.data() + (size_t)nid * H;
            std::copy(er, er + H, M.ne_src.data() + (size_t)i * H);
        }
        rmsnorm_fwd(M.ne_src.data() + (size_t)i * H,
                    p.w.at("mtp.norm_e").d.data(),
                    M.cin.data() + (size_t)i * 2 * H + H,
                    M.ne_rms.data() + i, 1, H, c.rms_eps);
    }
    M.z.resize((size_t)PT * H);
    linear_fwd(M.cin.data(), p.w.at("mtp.w_proj"), M.z.data(), PT, 2 * H, H);
    L.x_in = M.z;
    L.n1.resize((size_t)PT * H); L.rms1.resize(PT);
    rmsnorm_fwd(M.z.data(), p.w.at("mtp.norm1").d.data(), L.n1.data(),
                L.rms1.data(), PT, H, c.rms_eps);
    L.q.resize((size_t)PT * Hq);
    L.k.resize((size_t)PT * Hkvl); L.v.resize((size_t)PT * Hkvl);
    linear_fwd(L.n1.data(), p.w.at("mtp.wq"), L.q.data(), PT, H, Hq);
    linear_fwd(L.n1.data(), p.w.at("mtp.wk"), L.k.data(), PT, H, Hkvl);
    linear_fwd(L.n1.data(), p.w.at("mtp.wv"), L.v.data(), PT, H, Hkvl);
    rope(L.q.data(), PT, c.heads, hd, c.rope_theta, false);
    rope(L.k.data(), PT, kvh, hd, c.rope_theta, false);
    const int group = c.heads / kvh;
    const float scale = 1.0f / std::sqrt((float)hd);
    L.probs.assign((size_t)c.heads * PT * PT, 0.0f);
    L.attn_out.assign((size_t)PT * Hq, 0.0f);
    parallel_for(c.heads, [&](int64_t hb, int64_t he) {
        for (int64_t h = hb; h < he; ++h) {
            int kh2 = (int)h / group;
            for (int t = 0; t < PT; ++t) {
                float* pr = L.probs.data() + ((size_t)h * PT + t) * PT;
                float mx = -1e30f;
                const float* qr = L.q.data() +
                                  ((size_t)t * c.heads + h) * hd;
                for (int s = 0; s <= t; ++s) {
                    const float* kr = L.k.data() +
                                      ((size_t)s * kvh + kh2) * hd;
                    pr[s] = tpu_dot(qr, kr, hd) * scale;
                    mx = std::max(mx, pr[s]);
                }
                float sum = 0.0f;
                for (int s = 0; s <= t; ++s) {
                    pr[s] = std::exp(pr[s] - mx); sum += pr[s];
                }
                float inv = 1.0f / sum;
                float* ao = L.attn_out.data() +
                            ((size_t)t * c.heads + h) * hd;
                for (int s = 0; s <= t; ++s) {
                    pr[s] *= inv;
                    const float* vr = L.v.data() +
                                      ((size_t)s * kvh + kh2) * hd;
                    tpu_axpy(ao, pr[s], vr, hd);
                }
            }
        }
    });
    std::vector<float> proj((size_t)PT * H);
    linear_fwd(L.attn_out.data(), p.w.at("mtp.wo"), proj.data(), PT, Hq, H);
    L.x_res.resize((size_t)PT * H);
    for (size_t i = 0; i < L.x_res.size(); ++i) L.x_res[i] = M.z[i] + proj[i];
    L.n2.resize((size_t)PT * H); L.rms2.resize(PT);
    rmsnorm_fwd(L.x_res.data(), p.w.at("mtp.norm2").d.data(), L.n2.data(),
                L.rms2.data(), PT, H, c.rms_eps);
    L.fa.resize((size_t)PT * c.inter); L.fb.resize((size_t)PT * c.inter);
    L.fh.resize((size_t)PT * c.inter);
    linear_fwd(L.n2.data(), p.w.at("mtp.w1"), L.fa.data(), PT, H, c.inter);
    linear_fwd(L.n2.data(), p.w.at("mtp.w3"), L.fb.data(), PT, H, c.inter);
    tpu_elementwise((int64_t)L.fh.size(), [&](int64_t i) {
        L.fh[(size_t)i] = gate_act_f(L.fa[(size_t)i], c.ffn_act) *
                          L.fb[(size_t)i];
    });
    std::fill(proj.begin(), proj.end(), 0.0f);
    linear_fwd(L.fh.data(), p.w.at("mtp.w2"), proj.data(), PT, c.inter, H);
    M.res2.resize((size_t)PT * H);
    for (size_t i = 0; i < M.res2.size(); ++i)
        M.res2[i] = L.x_res[i] + proj[i];
    M.out.resize((size_t)PT * H); M.out_rms.resize(PT);
    rmsnorm_fwd(M.res2.data(), p.w.at("mtp.norm_out").d.data(), M.out.data(),
                M.out_rms.data(), PT, H, c.rms_eps);
    M.logits.resize((size_t)PT * c.vocab);
    linear_fwd(M.out.data(), p.w.at("lm_head"), M.logits.data(), PT, H,
               c.vocab);
    std::vector<float> dtmp;
    M.loss = c.mtp_loss_weight *
             ce_loss(M.logits, M.lab, PT, c.vocab, dtmp);
}

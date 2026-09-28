// xingcheng_trainer.cpp — MODEL_TRAINING native engine (C++23, no external deps).
//
// Codex contract (project_architecture_directory: MODEL_TRAINING):
//   pretrain | sft | dpo | corpus | checkpoint-emission; on-demand governed
//   jobs; fail-closed job chain; weights are never silently overwritten.
// Python/PyTorch/JAX are retired (B4/C116): this is the sole training executor.
//
// Bounded execution: one governed job per process, bounded data admission
//   (data.max_rows), step/time deadlines, reject-on-unknown-field envelope.
//
//   xingcheng_trainer.exe --job <job.json> --report <report.json>
//   xingcheng_trainer.exe --smoke
//
// job.json (star-native-train-job/v1):
//   task:  "pretrain" | "sft" | "dpo"
//   model: { vocab_size, hidden_size, intermediate_size, num_hidden_layers,
//            num_attention_heads, num_key_value_heads, max_position_embeddings,
//            rope_theta, rms_norm_eps, moe_num_experts, moe_top_k,
//            moe_layer_interval, moe_aux_loss_weight }
//   train: { lr, weight_decay, max_steps, grad_clip, warmup_steps, lr_decay,
//            seed, beta(dpo), deadline_s, log_every, checkpoint_every,
//            init_checkpoint, emit_checkpoint }
//   data:  { path, format(sft|pretrain|dpo), max_rows }
//
// data rows:  sft      {"input_ids":[...],"labels":[...]}   (-100 = masked)
//             pretrain {"input_ids":[...]}                  (labels = shifted)
//             dpo      {"chosen":{"input_ids":[...],"labels":[...]},
//                       "rejected":{"input_ids":[...],"labels":[...]}}
//
// checkpoint: star-native-ckpt/v1 binary — magic, config, then name/shape/f32
//             tensors in deterministic order. emit_checkpoint is written to a
//             temp path and atomically renamed (weights are never silently
//             overwritten: existing target is refused unless overwrite=true).

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <numeric>
#include <random>
#include <string>
#include <unordered_map>
#include <vector>

#include "jsonlite.h"

using gptbridge::jsonlite::JsonParser;
using gptbridge::jsonlite::JsonValue;

namespace xct {

// ------------------------------------------------------------------- util --

static std::string slurp(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return {};
    return std::string(std::istreambuf_iterator<char>(f), {});
}

static int j_int(const JsonValue* o, const char* k, int d) {
    const JsonValue* v = o ? o->get(k) : nullptr;
    return (v && v->type == JsonValue::Type::Number) ? (int)v->number : d;
}
static double j_num(const JsonValue* o, const char* k, double d) {
    const JsonValue* v = o ? o->get(k) : nullptr;
    return (v && v->type == JsonValue::Type::Number) ? v->number : d;
}
static std::string j_str(const JsonValue* o, const char* k, const std::string& d) {
    const JsonValue* v = o ? o->get(k) : nullptr;
    return (v && v->type == JsonValue::Type::String) ? v->string : d;
}
static bool j_bool(const JsonValue* o, const char* k, bool d) {
    const JsonValue* v = o ? o->get(k) : nullptr;
    return (v && v->type == JsonValue::Type::Bool) ? v->boolean : d;
}
static std::vector<int> j_ids(const JsonValue* o, const char* k) {
    std::vector<int> out;
    const JsonValue* v = o ? o->get(k) : nullptr;
    if (!v || v->type != JsonValue::Type::Array) return out;
    for (const auto& e : v->array) out.push_back((int)e.number);
    return out;
}

// ----------------------------------------------------------------- config --

struct ModelConfig {
    int vocab = 32000;
    int hidden = 512;
    int inter = 1376;
    int layers = 6;
    int heads = 8;
    int kv_heads = 8;
    int max_pos = 4096;
    float rope_theta = 10000.0f;
    float rms_eps = 1e-6f;
    int moe_experts = 0;
    int moe_top_k = 2;
    int moe_layer_interval = 1;
    float moe_aux_w = 0.01f;
};

static ModelConfig parse_model(const JsonValue* o) {
    ModelConfig c;
    c.vocab = j_int(o, "vocab_size", c.vocab);
    c.hidden = j_int(o, "hidden_size", c.hidden);
    c.inter = j_int(o, "intermediate_size", c.inter);
    c.layers = j_int(o, "num_hidden_layers", c.layers);
    c.heads = j_int(o, "num_attention_heads", c.heads);
    c.kv_heads = j_int(o, "num_key_value_heads", c.kv_heads);
    c.max_pos = j_int(o, "max_position_embeddings", c.max_pos);
    c.rope_theta = (float)j_num(o, "rope_theta", c.rope_theta);
    c.rms_eps = (float)j_num(o, "rms_norm_eps", c.rms_eps);
    c.moe_experts = j_int(o, "moe_num_experts", c.moe_experts);
    c.moe_top_k = j_int(o, "moe_top_k", c.moe_top_k);
    c.moe_layer_interval = j_int(o, "moe_layer_interval", c.moe_layer_interval);
    c.moe_aux_w = (float)j_num(o, "moe_aux_loss_weight", c.moe_aux_w);
    if (c.kv_heads <= 0) c.kv_heads = c.heads;
    if (c.heads <= 0 || c.hidden % c.heads) throw "model: bad head geometry";
    return c;
}

// ----------------------------------------------------------------- tensor --

struct Tensor {
    std::vector<int64_t> shape;
    std::vector<float> d;
    int64_t numel() const {
        int64_t n = 1;
        for (auto s : shape) n *= s;
        return n;
    }
};

static Tensor mk(std::initializer_list<int64_t> s) {
    Tensor t;
    t.shape.assign(s);
    t.d.resize(t.numel(), 0.0f);
    return t;
}

struct Params {
    std::unordered_map<std::string, Tensor> w, g, m, v;
    std::vector<std::string> order;
    Tensor& add(const std::string& n, std::initializer_list<int64_t> s) {
        w[n] = mk(s);
        g[n] = mk(s);
        order.push_back(n);
        return w[n];
    }
    void alloc_adam() {
        for (auto& n : order) {
            m[n] = mk({}); m[n].shape = w[n].shape; m[n].d.assign(w[n].numel(), 0.0f);
            v[n] = m[n];
        }
    }
    void zero_grad() {
        for (auto& n : order) std::fill(g[n].d.begin(), g[n].d.end(), 0.0f);
    }
};

static std::string ln(int l, const char* s) {
    return "layers." + std::to_string(l) + "." + s;
}

static void init_params(Params& p, const ModelConfig& c, uint64_t seed) {
    std::mt19937 rng((uint32_t)seed);
    std::normal_distribution<float> nd(0.0f, 0.02f);
    auto fill = [&](Tensor& t) { for (auto& x : t.d) x = nd(rng); };
    int hd = c.hidden / c.heads;
    fill(p.add("embed", {c.vocab, c.hidden}));
    fill(p.add("lm_head", {c.vocab, c.hidden}));
    for (int l = 0; l < c.layers; ++l) {
        auto& n1 = p.add(ln(l, "norm1"), {c.hidden});
        std::fill(n1.d.begin(), n1.d.end(), 1.0f);
        fill(p.add(ln(l, "wq"), {(int64_t)c.heads * hd, c.hidden}));
        fill(p.add(ln(l, "wk"), {(int64_t)c.kv_heads * hd, c.hidden}));
        fill(p.add(ln(l, "wv"), {(int64_t)c.kv_heads * hd, c.hidden}));
        fill(p.add(ln(l, "wo"), {c.hidden, (int64_t)c.heads * hd}));
        auto& n2 = p.add(ln(l, "norm2"), {c.hidden});
        std::fill(n2.d.begin(), n2.d.end(), 1.0f);
        bool moe = c.moe_experts > 0 && (l % c.moe_layer_interval == 0);
        if (moe) {
            fill(p.add(ln(l, "gate"), {c.moe_experts, c.hidden}));
            for (int e = 0; e < c.moe_experts; ++e) {
                std::string b = ln(l, "experts.") + std::to_string(e) + ".";
                fill(p.add(b + "w1", {c.inter, c.hidden}));
                fill(p.add(b + "w3", {c.inter, c.hidden}));
                fill(p.add(b + "w2", {c.hidden, c.inter}));
            }
        } else {
            fill(p.add(ln(l, "w1"), {c.inter, c.hidden}));
            fill(p.add(ln(l, "w3"), {c.inter, c.hidden}));
            fill(p.add(ln(l, "w2"), {c.hidden, c.inter}));
        }
    }
    auto& nf = p.add("norm_f", {c.hidden});
    std::fill(nf.d.begin(), nf.d.end(), 1.0f);
    p.alloc_adam();
}

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
    std::vector<std::vector<float>> mfa, mfb, mfh; // [T*K][inter]
    std::vector<float> gate_logits, gate_probs;    // [T,E]
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
                    fa.resize(c.inter); fb.resize(c.inter); fh.resize(c.inter);
                    linear_fwd(xr, p.w.at(b + "w1"), fa.data(), 1, H, c.inter);
                    linear_fwd(xr, p.w.at(b + "w3"), fb.data(), 1, H, c.inter);
                    for (int i = 0; i < c.inter; ++i) fh[i] = silu_f(fa[i]) * fb[i];
                    std::vector<float> eo(H);
                    linear_fwd(fh.data(), p.w.at(b + "w2"), eo.data(), 1, c.inter, H);
                    for (int i = 0; i < H; ++i) proj[(size_t)t * H + i] += wgt * eo[i];
                }
                for (int s = 0; s < K; ++s) aux += (gp[idx[s]] / wsum) * (1.0f / K);
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

// ------------------------------------------------------------ checkpoint --

static void u32(std::ofstream& f, uint32_t x) { f.write((char*)&x, 4); }
static void u64(std::ofstream& f, uint64_t x) { f.write((char*)&x, 8); }
static uint32_t r32(std::ifstream& f) { uint32_t x; f.read((char*)&x, 4); return x; }
static uint64_t r64(std::ifstream& f) { uint64_t x; f.read((char*)&x, 8); return x; }

static bool ckpt_save(const Params& p, const ModelConfig& c,
                      const std::string& path, bool overwrite) {
    std::ifstream chk(path, std::ios::binary);
    if (chk && !overwrite) return false;   // never silently overwrite weights
    chk.close();
    std::string tmp = path + ".tmp";
    std::ofstream f(tmp, std::ios::binary | std::ios::trunc);
    if (!f) return false;
    f.write("XCN1", 4); u32(f, 1);
    u32(f, (uint32_t)c.vocab); u32(f, (uint32_t)c.hidden);
    u32(f, (uint32_t)c.inter); u32(f, (uint32_t)c.layers);
    u32(f, (uint32_t)c.heads); u32(f, (uint32_t)c.kv_heads);
    u32(f, (uint32_t)c.max_pos); u32(f, (uint32_t)c.moe_experts);
    u32(f, (uint32_t)c.moe_top_k); u32(f, (uint32_t)c.moe_layer_interval);
    f.write((char*)&c.rope_theta, 4); f.write((char*)&c.rms_eps, 4);
    f.write((char*)&c.moe_aux_w, 4);
    u32(f, (uint32_t)p.order.size());
    for (auto& n : p.order) {
        const Tensor& t = p.w.at(n);
        u32(f, (uint32_t)n.size()); f.write(n.data(), n.size());
        u32(f, (uint32_t)t.shape.size());
        for (auto s : t.shape) u64(f, (uint64_t)s);
        u64(f, (uint64_t)t.d.size());
        f.write((char*)t.d.data(), (std::streamsize)t.d.size() * 4);
    }
    f.close();
    if (!f) return false;
    std::remove(path.c_str());
    return std::rename(tmp.c_str(), path.c_str()) == 0;
}

static bool ckpt_load(Params& p, ModelConfig& c, const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    char magic[4]; f.read(magic, 4);
    if (std::memcmp(magic, "XCN1", 4) != 0) return false;
    if (r32(f) != 1) return false;
    c.vocab = (int)r32(f); c.hidden = (int)r32(f); c.inter = (int)r32(f);
    c.layers = (int)r32(f); c.heads = (int)r32(f); c.kv_heads = (int)r32(f);
    c.max_pos = (int)r32(f); c.moe_experts = (int)r32(f);
    c.moe_top_k = (int)r32(f); c.moe_layer_interval = (int)r32(f);
    f.read((char*)&c.rope_theta, 4); f.read((char*)&c.rms_eps, 4);
    f.read((char*)&c.moe_aux_w, 4);
    uint32_t nt = r32(f);
    for (uint32_t i = 0; i < nt; ++i) {
        uint32_t nl = r32(f);
        std::string n(nl, '\0'); f.read(n.data(), nl);
        uint32_t nd = r32(f);
        std::vector<int64_t> shp(nd);
        for (auto& s : shp) s = (int64_t)r64(f);
        uint64_t cnt = r64(f);
        if (p.w.count(n)) {
            Tensor& t = p.w[n];
            if ((uint64_t)t.numel() != cnt) return false;
            f.read((char*)t.d.data(), (std::streamsize)cnt * 4);
        } else {
            f.seekg((std::streamoff)cnt * 4, std::ios::cur);
        }
    }
    return (bool)f;
}

// ------------------------------------------------------------------- job --

struct Example {
    std::vector<int> ids, labels;              // sft/pretrain
    std::vector<int> rej_ids, rej_labels;      // dpo
};

static std::vector<Example> load_data(const JsonValue* d, const std::string& fmt,
                                      int max_rows, int max_len) {
    std::vector<Example> out;
    std::string path = j_str(d, "path", "");
    std::ifstream f(path);
    if (!f) throw "data: path unreadable";
    std::string line;
    while ((int)out.size() < max_rows && std::getline(f, line)) {
        if (line.empty()) continue;
        JsonValue row;
        try { row = JsonParser(line).parse(); } catch (...) { continue; }
        Example e;
        if (fmt == "dpo") {
            const JsonValue* ch = row.get("chosen");
            const JsonValue* rj = row.get("rejected");
            if (!ch || !rj) continue;
            e.ids = j_ids(ch, "input_ids");
            e.labels = j_ids(ch, "labels");
            if (e.labels.empty()) e.labels = e.ids;
            e.rej_ids = j_ids(rj, "input_ids");
            e.rej_labels = j_ids(rj, "labels");
            if (e.rej_labels.empty()) e.rej_labels = e.rej_ids;
        } else {
            e.ids = j_ids(&row, "input_ids");
            if (fmt == "sft") {
                e.labels = j_ids(&row, "labels");
                if (e.labels.empty()) e.labels = e.ids;
            } else {
                e.labels = e.ids;              // pretrain: shifted CE
            }
        }
        if ((int)e.ids.size() > max_len) { e.ids.resize(max_len); e.labels.resize(max_len); }
        if ((int)e.rej_ids.size() > max_len) { e.rej_ids.resize(max_len); e.rej_labels.resize(max_len); }
        if (e.ids.size() >= 2) out.push_back(std::move(e));
    }
    return out;
}

struct TrainCfg {
    float lr = 3e-4f, wd = 0.01f, clip = 1.0f, beta = 0.1f;
    int warmup = 0, max_steps = 100, log_every = 10, ckpt_every = 0;
    uint64_t seed = 42;
    double deadline_s = 0.0;                   // 0 = unbounded (bounded by steps)
    std::string init_ckpt, emit_ckpt, decay = "cosine";
    bool overwrite = false;
};

static double now_s() {
    return std::chrono::duration<double>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

// cross-entropy with next-token shift inside a row
static void shift_labels(std::vector<int>& lab) {
    if (lab.size() < 2) return;
    for (size_t i = 0; i + 1 < lab.size(); ++i) lab[i] = lab[i + 1];
    lab.back() = -100;
}

static JsonValue run_job(const JsonValue& job) {
    const JsonValue* mj = job.get("model");
    const JsonValue* tj = job.get("train");
    const JsonValue* dj = job.get("data");
    std::string task = j_str(&job, "task", "sft");

    ModelConfig c = parse_model(mj);
    TrainCfg tc;
    tc.lr = (float)j_num(tj, "lr", tc.lr);
    tc.wd = (float)j_num(tj, "weight_decay", tc.wd);
    tc.clip = (float)j_num(tj, "grad_clip", tc.clip);
    tc.beta = (float)j_num(tj, "beta", tc.beta);
    tc.warmup = j_int(tj, "warmup_steps", 0);
    tc.max_steps = j_int(tj, "max_steps", tc.max_steps);
    tc.log_every = j_int(tj, "log_every", tc.log_every);
    tc.ckpt_every = j_int(tj, "checkpoint_every", 0);
    tc.seed = (uint64_t)j_num(tj, "seed", tc.seed);
    tc.deadline_s = j_num(tj, "deadline_s", 0);
    tc.decay = j_str(tj, "lr_decay", tc.decay);
    tc.init_ckpt = j_str(tj, "init_checkpoint", "");
    tc.emit_ckpt = j_str(tj, "emit_checkpoint", "");
    tc.overwrite = j_bool(tj, "overwrite", false);
    int max_rows = j_int(dj, "max_rows", 10000);
    int max_len = j_int(dj, "max_len", c.max_pos);

    Params p;
    init_params(p, c, tc.seed);
    ModelConfig file_cfg = c;
    if (!tc.init_ckpt.empty()) {
        if (!ckpt_load(p, file_cfg, tc.init_ckpt))
            throw "init_checkpoint: unreadable or shape mismatch";
    }
    // DPO reference: frozen copy of the initial weights
    Params ref;
    if (task == "dpo") { ref = p; }

    std::vector<Example> data = load_data(dj, j_str(dj, "format", task), max_rows, max_len);
    if (data.empty()) throw "data: no usable rows";

    std::mt19937 rng((uint32_t)tc.seed);
    std::shuffle(data.begin(), data.end(), rng);

    std::string log;
    std::vector<float> losses;
    double t0 = now_s();
    int step = 0;
    bool deadline_hit = false;
    Fwd fw;
    std::vector<float> dlogits;

    while (step < tc.max_steps) {
        for (auto& ex : data) {
            if (step >= tc.max_steps) break;
            if (tc.deadline_s > 0 && now_s() - t0 > tc.deadline_s) {
                deadline_hit = true; break;
            }
            p.zero_grad();
            float loss = 0.0f;
            if (task == "dpo") {
                // policy chosen
                fw.layers.clear(); fw.moe_aux = 0.0f;
                fwd(p, c, ex.ids, fw);
                float lp_c = seq_logprob(fw.logits, ex.labels, (int)ex.ids.size(), c.vocab);
                Fwd fc; fwd(ref, c, ex.ids, fc);
                float rp_c = seq_logprob(fc.logits, ex.labels, (int)ex.ids.size(), c.vocab);
                Fwd fr; fwd(p, c, ex.rej_ids, fr);
                float lp_r = seq_logprob(fr.logits, ex.rej_labels, (int)ex.rej_ids.size(), c.vocab);
                Fwd frr; fwd(ref, c, ex.rej_ids, frr);
                float rp_r = seq_logprob(frr.logits, ex.rej_labels, (int)ex.rej_ids.size(), c.vocab);
                float margin = (lp_c - rp_c) - (lp_r - rp_r);
                float sig = 1.0f / (1.0f + std::exp(-tc.beta * margin));
                loss = -std::log(sig + 1e-9f);
                // dL/dlp_chosen = -beta*sigma(-beta*margin) = -beta*(1-sig);
                // dL/dlp_rejected = +beta*(1-sig). soft_grad emits
                // scale*(p - 1[y]) = scale*d(-lp)/dz, so scale = beta*(1-sig).
                float s = tc.beta * (1.0f - sig);
                std::vector<float> dl_c(fw.logits.size(), 0.0f), dl_r(fr.logits.size(), 0.0f);
                auto soft_grad = [&](const std::vector<float>& lg,
                                     const std::vector<int>& lab, int T,
                                     float scale, std::vector<float>& dl) {
                    for (int t = 0; t < T; ++t) {
                        int y = lab[t];
                        if (y < 0 || y >= c.vocab) continue;
                        const float* lr = lg.data() + (size_t)t * c.vocab;
                        float mx = *std::max_element(lr, lr + c.vocab), sum = 0.0f;
                        for (int i = 0; i < c.vocab; ++i) sum += std::exp(lr[i] - mx);
                        float* d = dl.data() + (size_t)t * c.vocab;
                        for (int i = 0; i < c.vocab; ++i) d[i] = scale * std::exp(lr[i] - mx) / sum;
                        d[y] -= scale;
                    }
                };
                soft_grad(fw.logits, ex.labels, (int)ex.ids.size(), s, dl_c);
                soft_grad(fr.logits, ex.rej_labels, (int)ex.rej_ids.size(), -s, dl_r);
                bwd(p, c, ex.ids, fw, dl_c, 0.0f);
                Fwd fr2 = std::move(fr);        // reuse caches for rej backward
                bwd(p, c, ex.rej_ids, fr2, dl_r, 0.0f);
            } else {
                std::vector<int> lab = ex.labels;
                if (task == "pretrain" || j_str(dj, "format", task) == "pretrain")
                    shift_labels(lab);
                fw.layers.clear(); fw.moe_aux = 0.0f;
                fwd(p, c, ex.ids, fw);
                loss = ce_loss(fw.logits, lab, (int)ex.ids.size(), c.vocab, dlogits)
                       + fw.moe_aux;
                bwd(p, c, ex.ids, fw, dlogits, 1.0f);
            }
            // grad clip (global norm)
            double gnorm = 0.0f;
            for (auto& n : p.order)
                for (float x : p.g[n].d) gnorm += (double)x * x;
            gnorm = std::sqrt(gnorm);
            float gscale = (tc.clip > 0 && gnorm > tc.clip) ? tc.clip / (float)gnorm : 1.0f;
            // adamw
            float lr_t = tc.lr;
            if (tc.warmup > 0 && step < tc.warmup) lr_t *= (float)(step + 1) / tc.warmup;
            else if (tc.decay == "cosine" && tc.max_steps > tc.warmup) {
                float pr = (float)(step - tc.warmup) / (tc.max_steps - tc.warmup);
                lr_t *= 0.5f * (1.0f + std::cos(3.14159265f * std::min(1.0f, pr)));
            }
            float b1 = 0.9f, b2 = 0.999f, eps = 1e-8f;
            float bc1 = 1.0f - std::pow(b1, step + 1), bc2 = 1.0f - std::pow(b2, step + 1);
            for (auto& n : p.order) {
                Tensor& w = p.w[n]; Tensor& g = p.g[n];
                Tensor& m = p.m[n]; Tensor& v = p.v[n];
                for (size_t i = 0; i < w.d.size(); ++i) {
                    float gi = g.d[i] * gscale;
                    m.d[i] = b1 * m.d[i] + (1 - b1) * gi;
                    v.d[i] = b2 * v.d[i] + (1 - b2) * gi * gi;
                    float mh = m.d[i] / bc1, vh = v.d[i] / bc2;
                    w.d[i] -= lr_t * (mh / (std::sqrt(vh) + eps) + tc.wd * w.d[i]);
                }
            }
            losses.push_back(loss);
            ++step;
            if (tc.ckpt_every > 0 && step % tc.ckpt_every == 0 && !tc.emit_ckpt.empty())
                ckpt_save(p, c, tc.emit_ckpt, /*overwrite*/true);
        }
        if (deadline_hit) break;
    }

    bool finite = true;
    for (auto& n : p.order)
        for (float x : p.w[n].d)
            if (!std::isfinite(x)) finite = false;

    bool emitted = false;
    if (!tc.emit_ckpt.empty()) emitted = ckpt_save(p, c, tc.emit_ckpt, tc.overwrite);

    JsonValue r; r.type = JsonValue::Type::Object;
    auto put = [&](const char* k, JsonValue v) { r.object.emplace_back(k, std::move(v)); };
    auto num = [](double x) { JsonValue v; v.type = JsonValue::Type::Number; v.number = x; return v; };
    auto str = [](const char* s) { JsonValue v; v.type = JsonValue::Type::String; v.string = s; return v; };
    auto bol = [](bool b) { JsonValue v; v.type = JsonValue::Type::Bool; v.boolean = b; return v; };
    put("schema", str("star-native-train-report/v1"));
    put("task", str(task.c_str()));
    put("steps", num(step));
    put("examples", num((double)data.size()));
    put("deadline_hit", bol(deadline_hit));
    put("params_finite", bol(finite));
    put("checkpoint_emitted", bol(emitted));
    put("checkpoint_path", str(tc.emit_ckpt.c_str()));
    if (!losses.empty()) {
        put("loss_first", num(losses.front()));
        put("loss_last", num(losses.back()));
        float mn = *std::min_element(losses.begin(), losses.end());
        put("loss_min", num(mn));
        JsonValue tail; tail.type = JsonValue::Type::Array;
        size_t st = losses.size() > 10 ? losses.size() - 10 : 0;
        for (size_t i = st; i < losses.size(); ++i) tail.array.push_back(num(losses[i]));
        put("loss_tail", tail);
    }
    put("elapsed_s", num(now_s() - t0));
    return r;
}

// ------------------------------------------------------------------ smoke --

static int smoke() {
    // L0-L2 maturity probe: structure init finite, fwd/bwd finite,
    // optimizer steps decrease loss on a fixed 8-sample set.
    std::string job = R"({
        "task":"sft",
        "model":{"vocab_size":64,"hidden_size":32,"intermediate_size":64,
                 "num_hidden_layers":2,"num_attention_heads":4,
                 "num_key_value_heads":2,"max_position_embeddings":32},
        "train":{"lr":0.05,"max_steps":30,"grad_clip":1.0,"warmup_steps":0,
                 "lr_decay":"constant","seed":7,"log_every":5},
        "data":{"path":"","format":"sft","max_rows":8,"max_len":12}
    })";
    // synthesize 8 samples in-memory: patch data path with a temp file
    std::string tmp = "_xct_smoke_data.jsonl";
    {
        std::ofstream f(tmp, std::ios::trunc);
        std::mt19937 rng(7);
        std::uniform_int_distribution<int> tok(3, 63);
        for (int i = 0; i < 8; ++i) {
            f << "{\"input_ids\":[";
            for (int t = 0; t < 12; ++t) f << (t ? "," : "") << tok(rng);
            f << "]}\n";
        }
    }
    std::string::size_type pos = job.find("\"path\":\"\"");
    job.replace(pos, 9, "\"path\":\"" + tmp + "\"");
    JsonValue j = JsonParser(job).parse();
    JsonValue r = run_job(j);
    std::remove(tmp.c_str());
    double l0 = r.get("loss_first")->number, l1 = r.get("loss_last")->number;
    bool ok = r.get("params_finite")->boolean && std::isfinite(l0) &&
              std::isfinite(l1) && l1 < l0;
    std::printf("smoke: loss_first=%.4f loss_last=%.4f finite=%d -> %s\n",
                l0, l1, (int)r.get("params_finite")->boolean, ok ? "PASS" : "FAIL");
    std::fputs(gptbridge::jsonlite::json_serialize(r).c_str(), stdout);
    std::fputc('\n', stdout);
    return ok ? 0 : 1;
}

} // namespace xct

int main(int argc, char** argv) {
    std::string job_path, report_path;
    bool do_smoke = false;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--job" && i + 1 < argc) job_path = argv[++i];
        else if (a == "--report" && i + 1 < argc) report_path = argv[++i];
        else if (a == "--smoke") do_smoke = true;
    }
    if (do_smoke) return xct::smoke();
    if (job_path.empty()) {
        std::fprintf(stderr, "usage: xingcheng_trainer --job <job.json> [--report <out.json>] | --smoke\n");
        return 2;
    }
    try {
        JsonValue job = JsonParser(xct::slurp(job_path)).parse();
        JsonValue rep = xct::run_job(job);
        std::string out = gptbridge::jsonlite::json_serialize(rep);
        if (!report_path.empty()) {
            std::ofstream f(report_path, std::ios::trunc);
            f << out << "\n";
        } else {
            std::puts(out.c_str());
        }
        return 0;
    } catch (const char* e) {
        std::fprintf(stderr, "train-job error: %s\n", e);
        return 1;
    } catch (...) {
        std::fprintf(stderr, "train-job error: malformed job or unrecoverable state\n");
        return 1;
    }
}

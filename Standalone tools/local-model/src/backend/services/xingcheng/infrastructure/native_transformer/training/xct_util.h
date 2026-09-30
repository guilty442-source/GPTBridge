// xct_util.h — B94 fragment of xingcheng_trainer.cpp (util/config/tensor params).
// Included once by xingcheng_trainer.cpp inside namespace xct.
#pragma once

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

// Vision patch grid: array of P rows, each an array of D numbers.
// Returns false when absent (text-only row); throws VISION_DATA_* when
// present but malformed (fail-closed, never silently partial).
static bool j_patch_grid(const JsonValue* o, const char* k,
                         std::vector<float>& out, int& patches, int& dim) {
    const JsonValue* v = o ? o->get(k) : nullptr;
    if (!v) return false;
    if (v->type != JsonValue::Type::Array) throw "data: VISION_DATA_NOT_ARRAY";
    patches = (int)v->array.size();
    dim = -1;
    for (const auto& pr : v->array) {
        if (pr.type != JsonValue::Type::Array) throw "data: VISION_DATA_ROW";
        if (dim < 0) dim = (int)pr.array.size();
        if ((int)pr.array.size() != dim || dim <= 0)
            throw "data: VISION_DATA_RAGGED";
        for (const auto& x : pr.array) {
            if (x.type != JsonValue::Type::Number) throw "data: VISION_DATA_NAN";
            out.push_back((float)x.number);
        }
    }
    if (patches <= 0 || dim <= 0) throw "data: VISION_DATA_EMPTY";
    return true;
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
    // v28 fused router scoring: true = Qwen3.5 per-expert sigmoid scores
    // (independent under many fine-grained experts); false keeps the
    // Qwen3-A3B softmax denominator. Top-k + renorm contract is shared.
    bool moe_router_sigmoid = false;
    // v26 fine-grained/shared-expert fields (XCN2): 0 = fall back to
    // inter (expert) / moe_expert_inter (shared), mirroring the engine's
    // moe_expert_intermediate_size / moe_shared_intermediate_size.
    int moe_expert_inter = 0;
    int moe_shared_experts = 0;
    int moe_shared_inter = 0;
    // Qwen3.5-A3B hybrid fields (v27): gated-deltanet linear attention
    // interleaved with gated full attention. full_attention_interval > 0
    // means layer l is full attention iff (l+1) % interval == 0, matching
    // HF layer_types = [linear*3, full]*n. 0 = every layer is full
    // attention (legacy dense behaviour).
    int full_attention_interval = 0;
    bool attn_output_gate = false;      // full-attn q_proj emits [q|gate]
    bool qk_norm = false;               // per-head RMSNorm on q/k pre-rope
    float partial_rotary = 1.0f;        // fraction of head_dim rotated
    int lin_key_heads = 0;              // deltanet: key/query heads
    int lin_key_dim = 0;                // deltanet: key head dim
    int lin_value_heads = 0;            // deltanet: value heads
    int lin_value_dim = 0;              // deltanet: value head dim
    int lin_conv_kernel = 4;            // depthwise causal conv width
    bool shared_expert_gate = false;    // sigmoid gate on shared expert out
    // Gemma 4 26B A4B signatures (all default-off; zero/false keeps the
    // Qwen-style fused behaviour bit-identical):
    int global_attn_interval = 0;  // >0: non-linear layers with
                                   // (l+1)%interval != 0 become local
                                   // sliding-window attention; the rest are
                                   // global (5 local : 1 global for A4B).
    int sliding_window = 0;        // local-attn causal window (1024 in A4B)
    int num_global_kv_heads = 0;   // global-layer kv heads (0 = kv_heads)
    bool k_eq_v_global = false;    // global layers: unified K==V projection
    float local_rope_proportion = 0.0f;   // 0 -> partial_rotary
    float global_rope_proportion = 0.0f;  // 0 -> partial_rotary (A4B: 0.25)
    float rope_theta_local = 0.0f;        // 0 -> rope_theta (A4B: 1e4)
    float rope_theta_global = 0.0f;       // 0 -> rope_theta (A4B: 1e6)
    float final_logit_softcap = 0.0f;     // A4B: 30.0
    bool post_attn_norm = false;          // rmsnorm on attn out-proj
    bool post_ffw_norm = false;           // rmsnorm on ffn out-proj
    int ffn_act = 0;                      // 0 = silu, 1 = gelu_tanh
    // DeepSeek V4-Pro signatures (all default-off; zero/false keeps the
    // fused behaviour bit-identical):
    int kv_lora_rank = 0;        // >0: MLA on non-linear attention layers —
                                 // keys/values flow through a shared latent
                                 // c = rmsnorm(W_dkv x) [kv_lora_rank] and
                                 // per-head up-projections; decoupled rope
                                 // channels ride a shared W_kr head.
    int q_lora_rank = 0;         // >0: low-rank q (W_dq -> norm -> W_uq);
                                 // 0 = direct wq projection.
    int qk_nope_head_dim = 0;    // per-head non-rope q/k dim (V3: 128)
    int qk_rope_head_dim = 0;    // decoupled rope channels (V3: 64; even)
    bool moe_auxfree_balance = false;  // V3 aux-loss-free balancing:
                                 // expert selection ranks s_e + b_e while
                                 // weights stay s_e; b_e is a non-gradient
                                 // buffer nudged by sign(mean - load)*u.
    float moe_lb_bias_rate = 0.0f;     // bias update rate u (V3: ~1e-3)
    int mtp_num_layers = 0;      // 1: multi-token-prediction module —
                                 // predicts t+2 from [norm(h_i)|norm(Emb
                                 // (t+1))] through one decoder block and
                                 // the shared head; loss weight below.
    float mtp_loss_weight = 0.0f;      // lambda on the MTP CE (V3: 0.3)
    // Native vision early-fusion (v1): optional linear patch projection.
    // use_vision=false (default) keeps text-only behaviour bit-identical.
    bool use_vision = false;
    int vision_patch_dim = 0;
    int vision_max_patches = 0;
    int expert_inter() const {
        return moe_expert_inter > 0 ? moe_expert_inter : inter;
    }
    int shared_inter() const {
        return moe_shared_inter > 0 ? moe_shared_inter : expert_inter();
    }
    bool is_linear(int l) const {
        return full_attention_interval > 0 && lin_key_heads > 0 &&
               ((l + 1) % full_attention_interval) != 0;
    }
    int rotary_dim() const {
        int hd = heads > 0 ? hidden / heads : 0;
        int rd = (int)(hd * partial_rotary);
        return rd > 0 && rd < hd ? rd & ~1 : hd;
    }
    // Gemma hybrid axis (independent of the deltanet/full axis): an
    // attention layer is LOCAL sliding-window when a global interval and
    // window are configured and the layer sits off the global beat; all
    // other attention layers are GLOBAL (k_eq_v / global kv heads /
    // p-RoPE / global theta apply there).
    bool is_local_attn(int l) const {
        return !is_linear(l) && global_attn_interval > 0 &&
               sliding_window > 0 && ((l + 1) % global_attn_interval) != 0;
    }
    bool is_global_attn(int l) const {
        return !is_linear(l) && !is_local_attn(l);
    }
    int kv_heads_at(int l) const {
        return is_global_attn(l) && num_global_kv_heads > 0
                   ? num_global_kv_heads : kv_heads;
    }
    bool kv_unified(int l) const {
        return is_global_attn(l) && k_eq_v_global;
    }
    float rope_theta_at(int l) const {
        float th = is_global_attn(l) ? rope_theta_global : rope_theta_local;
        return th > 0.0f ? th : rope_theta;
    }
    float rope_prop_at(int l) const {
        float pr = is_global_attn(l) ? global_rope_proportion
                                     : local_rope_proportion;
        return pr > 0.0f ? pr : partial_rotary;
    }
    int rotary_dim_at(int l) const {
        int hd = heads > 0 ? hidden / heads : 0;
        int rd = (int)(hd * rope_prop_at(l));
        return rd > 0 && rd < hd ? rd & ~1 : hd;
    }
    // DeepSeek MLA axis (independent of the deltanet/local-global axes):
    // every non-linear attention layer swaps its kv projections for the
    // latent path when kv_lora_rank is set.
    bool is_mla(int l) const {
        return !is_linear(l) && kv_lora_rank > 0;
    }
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
    c.moe_router_sigmoid = j_bool(o, "moe_router_sigmoid", c.moe_router_sigmoid);
    c.moe_expert_inter = j_int(o, "moe_expert_intermediate_size", c.moe_expert_inter);
    c.moe_shared_experts = j_int(o, "moe_num_shared_experts", c.moe_shared_experts);
    c.moe_shared_inter = j_int(o, "moe_shared_intermediate_size", c.moe_shared_inter);
    c.full_attention_interval = j_int(o, "full_attention_interval", c.full_attention_interval);
    c.attn_output_gate = j_bool(o, "attn_output_gate", c.attn_output_gate);
    c.qk_norm = j_bool(o, "qk_norm", c.qk_norm);
    c.partial_rotary = (float)j_num(o, "partial_rotary_factor", c.partial_rotary);
    c.lin_key_heads = j_int(o, "linear_num_key_heads", c.lin_key_heads);
    c.lin_key_dim = j_int(o, "linear_key_head_dim", c.lin_key_dim);
    c.lin_value_heads = j_int(o, "linear_num_value_heads", c.lin_value_heads);
    c.lin_value_dim = j_int(o, "linear_value_head_dim", c.lin_value_dim);
    c.lin_conv_kernel = j_int(o, "linear_conv_kernel_dim", c.lin_conv_kernel);
    c.shared_expert_gate = j_bool(o, "shared_expert_gate", c.shared_expert_gate);
    // Gemma 4 A4B fields (gm.nn.Gemma4_26B_A4B naming where applicable)
    c.global_attn_interval = j_int(o, "global_attention_interval",
                                   c.global_attn_interval);
    c.sliding_window = j_int(o, "sliding_window_size", c.sliding_window);
    c.num_global_kv_heads = j_int(o, "num_global_kv_heads",
                                  c.num_global_kv_heads);
    c.k_eq_v_global = j_bool(o, "k_eq_v_global", c.k_eq_v_global);
    c.local_rope_proportion =
        (float)j_num(o, "local_rope_proportion", c.local_rope_proportion);
    c.global_rope_proportion =
        (float)j_num(o, "global_rope_proportion", c.global_rope_proportion);
    c.rope_theta_local =
        (float)j_num(o, "local_base_frequency", c.rope_theta_local);
    c.rope_theta_global =
        (float)j_num(o, "global_base_frequency", c.rope_theta_global);
    c.final_logit_softcap =
        (float)j_num(o, "final_logit_softcap", c.final_logit_softcap);
    c.post_attn_norm = j_bool(o, "use_post_attn_norm", c.post_attn_norm);
    c.post_ffw_norm = j_bool(o, "use_post_ffw_norm", c.post_ffw_norm);
    if (j_str(o, "ffn_activation", "") == "gelu_tanh") c.ffn_act = 1;
    // DeepSeek V4-Pro fields (HF deepseek_v3 naming where applicable)
    c.kv_lora_rank = j_int(o, "kv_lora_rank", c.kv_lora_rank);
    c.q_lora_rank = j_int(o, "q_lora_rank", c.q_lora_rank);
    c.qk_nope_head_dim = j_int(o, "qk_nope_head_dim", c.qk_nope_head_dim);
    c.qk_rope_head_dim = j_int(o, "qk_rope_head_dim", c.qk_rope_head_dim);
    c.moe_auxfree_balance =
        j_bool(o, "moe_auxfree_balance", c.moe_auxfree_balance);
    c.moe_lb_bias_rate =
        (float)j_num(o, "moe_lb_bias_rate", c.moe_lb_bias_rate);
    c.mtp_num_layers = j_int(o, "num_nextn_predict_layers",
                             c.mtp_num_layers);
    c.mtp_loss_weight =
        (float)j_num(o, "mtp_loss_weight", c.mtp_loss_weight);
    c.use_vision = j_bool(o, "use_vision", c.use_vision);
    c.vision_patch_dim = j_int(o, "vision_patch_dim", c.vision_patch_dim);
    c.vision_max_patches = j_int(o, "vision_max_patches", c.vision_max_patches);
    if (c.use_vision && (c.vision_patch_dim <= 0 || c.vision_max_patches <= 0))
        throw "model: bad vision geometry";
    if (c.kv_heads <= 0) c.kv_heads = c.heads;
    if (c.heads <= 0 || c.hidden % c.heads) throw "model: bad head geometry";
    if (c.full_attention_interval > 0 &&
        (c.lin_key_heads <= 0 || c.lin_key_dim <= 0 ||
         c.lin_value_heads <= 0 || c.lin_value_dim <= 0 ||
         c.lin_value_heads % c.lin_key_heads != 0))
        throw "model: bad linear-attention geometry";
    // Gemma-axis validation (fail-closed, same style as above).
    if (c.sliding_window > 0 && c.global_attn_interval <= 0)
        throw "model: sliding_window_size needs global_attention_interval";
    if (c.num_global_kv_heads < 0 ||
        (c.num_global_kv_heads > 0 &&
         c.heads % c.num_global_kv_heads != 0))
        throw "model: bad num_global_kv_heads";
    if (c.local_rope_proportion < 0.0f || c.local_rope_proportion > 1.0f ||
        c.global_rope_proportion < 0.0f || c.global_rope_proportion > 1.0f)
        throw "model: rope proportion out of (0,1]";
    if (c.final_logit_softcap < 0.0f)
        throw "model: bad final_logit_softcap";
    // DeepSeek-axis validation (fail-closed). MLA replaces the whole
    // kv-projection family on its layers, so the alternate attention
    // knobs that target wq/wk/wv rows are rejected rather than ignored.
    if (c.kv_lora_rank > 0) {
        if (c.qk_nope_head_dim <= 0 || c.qk_rope_head_dim <= 0 ||
            (c.qk_rope_head_dim & 1))
            throw "model: bad MLA qk head dims";
        if (c.q_lora_rank < 0)
            throw "model: bad q_lora_rank";
        if (c.attn_output_gate || c.qk_norm || c.k_eq_v_global ||
            c.num_global_kv_heads > 0)
            throw "model: MLA conflicts with gate/qk_norm/kv axes";
    } else if (c.q_lora_rank != 0 || c.qk_nope_head_dim != 0 ||
               c.qk_rope_head_dim != 0)
        throw "model: MLA dims need kv_lora_rank";
    if (c.moe_auxfree_balance && c.moe_lb_bias_rate < 0.0f)
        throw "model: bad moe_lb_bias_rate";
    if (c.mtp_num_layers < 0 || c.mtp_num_layers > 1)
        throw "model: mtp_num_layers >1 not supported";
    if (c.mtp_num_layers > 0 && c.mtp_loss_weight <= 0.0f)
        throw "model: mtp needs mtp_loss_weight";
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
    if (c.use_vision)
        fill(p.add("vision.patch_proj", {c.hidden, c.vision_patch_dim}));
    for (int l = 0; l < c.layers; ++l) {
        auto& n1 = p.add(ln(l, "norm1"), {c.hidden});
        std::fill(n1.d.begin(), n1.d.end(), 1.0f);
        if (c.is_linear(l)) {
            // Qwen3.5 gated deltanet: fused qkv rows follow the HF
            // checkpoint layout — per key-head group [q|k|v-group].
            const int kd = c.lin_key_dim, vd = c.lin_value_dim;
            const int kh = c.lin_key_heads, vh = c.lin_value_heads;
            const int64_t key_dim = (int64_t)kh * kd;
            const int64_t val_dim = (int64_t)vh * vd;
            const int64_t conv_dim = key_dim * 2 + val_dim;
            std::string b = ln(l, "lin.");
            fill(p.add(b + "in_proj_qkv",
                       {kh * (2 * kd + vd * (vh / kh)), c.hidden}));
            fill(p.add(b + "in_proj_z", {val_dim, c.hidden}));
            fill(p.add(b + "in_proj_a", {vh, c.hidden}));
            fill(p.add(b + "in_proj_b", {vh, c.hidden}));
            fill(p.add(b + "conv1d", {conv_dim, c.lin_conv_kernel}));
            std::uniform_real_distribution<float> ua(0.01f, 16.0f);
            auto& al = p.add(b + "A_log", {vh});
            for (auto& x : al.d) x = std::log(ua(rng));
            auto& dt = p.add(b + "dt_bias", {vh});
            std::fill(dt.d.begin(), dt.d.end(), 1.0f);
            auto& gn = p.add(b + "norm", {vd});
            std::fill(gn.d.begin(), gn.d.end(), 1.0f);
            fill(p.add(b + "out_proj", {c.hidden, val_dim}));
        } else if (c.is_mla(l)) {
            // DeepSeek MLA: kv latent w_dkv -> rmsnorm -> per-head
            // up-projections (w_uk nope keys, w_uv values); decoupled
            // shared rope key w_kr; q either direct or low-rank.
            const int kn = c.qk_nope_head_dim, kr = c.qk_rope_head_dim;
            const int rank = c.kv_lora_rank, qr = c.q_lora_rank;
            const std::string b = ln(l, "");
            fill(p.add(b + "w_dkv", {rank, c.hidden}));
            auto& nk = p.add(b + "norm_kvl", {rank});
            std::fill(nk.d.begin(), nk.d.end(), 1.0f);
            fill(p.add(b + "w_uk", {(int64_t)c.heads * kn, rank}));
            fill(p.add(b + "w_uv", {(int64_t)c.heads * hd, rank}));
            fill(p.add(b + "w_kr", {kr, c.hidden}));
            if (qr > 0) {
                fill(p.add(b + "w_dq", {qr, c.hidden}));
                auto& nq = p.add(b + "norm_ql", {qr});
                std::fill(nq.d.begin(), nq.d.end(), 1.0f);
                fill(p.add(b + "w_uq",
                           {(int64_t)c.heads * (kn + kr), qr}));
            } else {
                fill(p.add(b + "wq",
                           {(int64_t)c.heads * (kn + kr), c.hidden}));
            }
            fill(p.add(b + "wo", {c.hidden, (int64_t)c.heads * hd}));
            if (c.post_attn_norm) {
                auto& pn = p.add(ln(l, "norm_attn_out"), {c.hidden});
                std::fill(pn.d.begin(), pn.d.end(), 1.0f);
            }
        } else {
            // attn_output_gate: q_proj rows are per-head [q|gate] pairs
            // (HF Qwen3NextAttention layout — verbatim transplantable).
            // Gemma axis: global layers may shrink the kv fan-out
            // (num_global_kv_heads) or unify K==V into one projection
            // (k_eq_v_global -> `wkv`).
            const int kvh = c.kv_heads_at(l);
            const int64_t Hkvl = (int64_t)kvh * hd;
            const int64_t qrows =
                (int64_t)c.heads * hd * (c.attn_output_gate ? 2 : 1);
            fill(p.add(ln(l, "wq"), {qrows, c.hidden}));
            if (c.kv_unified(l)) {
                fill(p.add(ln(l, "wkv"), {Hkvl, c.hidden}));
            } else {
                fill(p.add(ln(l, "wk"), {Hkvl, c.hidden}));
                fill(p.add(ln(l, "wv"), {Hkvl, c.hidden}));
            }
            fill(p.add(ln(l, "wo"), {c.hidden, (int64_t)c.heads * hd}));
            if (c.qk_norm) {
                auto& qn = p.add(ln(l, "q_norm"), {hd});
                auto& kn = p.add(ln(l, "k_norm"), {hd});
                std::fill(qn.d.begin(), qn.d.end(), 1.0f);
                std::fill(kn.d.begin(), kn.d.end(), 1.0f);
            }
            if (c.post_attn_norm) {
                auto& pn = p.add(ln(l, "norm_attn_out"), {c.hidden});
                std::fill(pn.d.begin(), pn.d.end(), 1.0f);
            }
        }
        auto& n2 = p.add(ln(l, "norm2"), {c.hidden});
        std::fill(n2.d.begin(), n2.d.end(), 1.0f);
        bool moe = c.moe_experts > 0 && (l % c.moe_layer_interval == 0);
        if (moe) {
            const int ei = c.expert_inter();
            const int si = c.shared_inter();
            fill(p.add(ln(l, "gate"), {c.moe_experts, c.hidden}));
            for (int e = 0; e < c.moe_experts; ++e) {
                std::string b = ln(l, "experts.") + std::to_string(e) + ".";
                fill(p.add(b + "w1", {ei, c.hidden}));
                fill(p.add(b + "w3", {ei, c.hidden}));
                fill(p.add(b + "w2", {c.hidden, ei}));
            }
            for (int s = 0; s < c.moe_shared_experts; ++s) {
                std::string b = ln(l, "shared.") + std::to_string(s) + ".";
                fill(p.add(b + "w1", {si, c.hidden}));
                fill(p.add(b + "w3", {si, c.hidden}));
                fill(p.add(b + "w2", {c.hidden, si}));
            }
            if (c.shared_expert_gate && c.moe_shared_experts > 0)
                fill(p.add(ln(l, "shared_gate"), {1, c.hidden}));
            if (c.moe_auxfree_balance) {
                // DeepSeek aux-free balance bias: routing-time additive
                // term only — zero-initialised, updated by the sign rule
                // (never by the optimizer; see adamw_step's skip).
                p.add(ln(l, "lb_bias"), {c.moe_experts});
            }
        } else {
            fill(p.add(ln(l, "w1"), {c.inter, c.hidden}));
            fill(p.add(ln(l, "w3"), {c.inter, c.hidden}));
            fill(p.add(ln(l, "w2"), {c.hidden, c.inter}));
        }
        if (c.post_ffw_norm) {
            auto& pn = p.add(ln(l, "norm_ffw_out"), {c.hidden});
            std::fill(pn.d.begin(), pn.d.end(), 1.0f);
        }
    }
    auto& nf = p.add("norm_f", {c.hidden});
    std::fill(nf.d.begin(), nf.d.end(), 1.0f);
    if (c.mtp_num_layers > 0) {
        // DeepSeek MTP module (depth-1): projected [norm(h)|norm(emb)]
        // through one decoder block (plain causal attention + dense FFN)
        // and the shared embed/lm_head.
        const int kvh = c.kv_heads;
        auto one = [&](const char* s) {
            auto& t = p.add(std::string("mtp.") + s, {c.hidden});
            std::fill(t.d.begin(), t.d.end(), 1.0f);
        };
        one("norm_h"); one("norm_e");
        fill(p.add("mtp.w_proj", {c.hidden, (int64_t)c.hidden * 2}));
        one("norm1");
        fill(p.add("mtp.wq", {(int64_t)c.heads * hd, c.hidden}));
        fill(p.add("mtp.wk", {(int64_t)kvh * hd, c.hidden}));
        fill(p.add("mtp.wv", {(int64_t)kvh * hd, c.hidden}));
        fill(p.add("mtp.wo", {c.hidden, (int64_t)c.heads * hd}));
        one("norm2");
        fill(p.add("mtp.w1", {c.inter, c.hidden}));
        fill(p.add("mtp.w3", {c.inter, c.hidden}));
        fill(p.add("mtp.w2", {c.hidden, c.inter}));
        one("norm_out");
    }
    p.alloc_adam();
}

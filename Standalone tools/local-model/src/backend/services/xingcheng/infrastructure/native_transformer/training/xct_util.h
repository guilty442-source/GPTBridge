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
static std::vector<std::string> j_strs(const JsonValue* o, const char* k) {
    std::vector<std::string> out;
    const JsonValue* v = o ? o->get(k) : nullptr;
    if (!v || v->type != JsonValue::Type::Array) return out;
    for (const auto& e : v->array)
        if (e.type == JsonValue::Type::String) out.push_back(e.string);
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
    // v29 Qwen3.8-Max signature (default-off; fused lane only): router
    // z-loss on the raw gate logits — the B133 companion to load-balance.
    float moe_zloss_w = 0.0f;
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
    // Native vision early-fusion (v1): optional linear patch projection.
    // use_vision=false (default) keeps text-only behaviour bit-identical.
    bool use_vision = false;
    int vision_patch_dim = 0;
    int vision_max_patches = 0;
    // DeepSeek-V4.1-Flash CSA2 lane (compressed sparse attention): global
    // attention layers bound raw-KV coverage to a sliding window and reach
    // the far past through top-K compressed r-to-1 latent KV selected by a
    // lightweight indexer. All fields default-off: csa_ratio<2 disables
    // the lane bit-identically.
    int csa_ratio = 0;            // tokens folded into one compressed latent
    int csa_topk = 0;             // compressed latents selected per query
    int csa_window = 0;           // raw-KV window on CSA layers
                                  // (0 -> sliding_window; both 0 is an error)
    float csa_rope_theta = 0.0f;  // compressed-KV rope base (0 -> rope_theta;
                                  // V4.1: 40000 — one latent spans r tokens)
    int csa_group = 0;            // CSA2 cross-layer sharing: <=1 every CSA
                                  // layer is Full; N>1 groups CSA layers N-
                                  // wise — the group head produces the
                                  // shared compressed KV, followers Reuse it
    bool csa_reindex = false;     // followers rescore shared index keys with
                                  // their own indexer Q (Reindex) instead of
                                  // reusing the head's top-K indices (Reuse)
    bool csa_indexer = true;      // learned indexer; false = recency top-K
    float csa_indexer_w = 1.0f;   // aux CE weight aligning index scores with
                                  // the main attention mass (0 disables)
    int expert_inter() const {
        return moe_expert_inter > 0 ? moe_expert_inter : inter;
    }
    int shared_inter() const {
        return moe_shared_inter > 0 ? moe_shared_inter : expert_inter();
    }
    // ---- gemma4 hybrid-attention profile (dense E-family replica) ----
    std::string model_type;              // "gemma4_text" | "gemma4"
    std::string hidden_act;              // "" (silu) | "gelu_pytorch_tanh"
    std::vector<std::string> layer_types;// "sliding_attention"|"full_attention"
    int head_dim = 0;                    // 0 -> hidden/heads
    int global_head_dim = 0;             // full-attention head dim
    // sliding_window is declared once above (shared by the A4B and
    // gemma4 profiles — same semantics, both parsers write it).
    float rope_theta_full = 0.0f;        // full-attention rope theta
    float rope_partial_full = 1.0f;      // partial_rotary_factor (full)
    int num_kv_shared_layers = 0;        // tail layers reuse the anchor KV
    int ple_hidden = 0;                  // hidden_size_per_layer_input
    int ple_vocab = 0;                   // vocab_size_per_layer_input
    float final_logit_softcapping = 0.0f;
    float attention_scale = 0.0f;        // 0 -> 1/sqrt(head_dim)
    bool use_double_wide_mlp = false;    // shared layers use 2x inter
    bool tie_embed = false;              // lm_head aliases embed
    bool is_gemma4() const {
        return model_type == "gemma4_text" || model_type == "gemma4";
    }
    int hd() const { return head_dim > 0 ? head_dim : hidden / heads; }
    const std::string& layer_type(int l) const {
        static const std::string sw = "sliding_attention";
        return (!layer_types.empty() && l < (int)layer_types.size())
            ? layer_types[l] : sw;
    }
    bool sliding_at(int l) const { return layer_type(l) != "full_attention"; }
    int hd_at(int l) const {
        return !is_gemma4() || sliding_at(l) ? hd()
            : (global_head_dim > 0 ? global_head_dim : hd());
    }
    float theta_at(int l) const {
        return !is_gemma4() || sliding_at(l) ? rope_theta
            : (rope_theta_full > 0.0f ? rope_theta_full : rope_theta);
    }
    int rotary_at(int l) const {         // rotated dims (even, pair layout)
        const int d = hd_at(l);
        if (!is_gemma4() || sliding_at(l)) return d & ~1;
        int n = (int)(rope_partial_full * (float)d);
        return std::min(d, n) & ~1;
    }
    // shared tail layers consume the last same-type non-shared layer's
    // K/V (HF shared_kv_states[layer_type]); returns the producing owner.
    int kv_owner(int l) const {
        if (!is_gemma4() || num_kv_shared_layers <= 0 ||
            l < layers - num_kv_shared_layers) return l;
        const std::string& want = layer_type(l);
        for (int j = layers - num_kv_shared_layers - 1; j >= 0; --j)
            if (layer_type(j) == want) return j;
        return l;
    }
    bool kv_shared(int l) const { return kv_owner(l) != l; }
    int inter_at(int l) const {
        return use_double_wide_mlp && kv_shared(l) ? 2 * inter : inter;
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
    // CSA2 axis: applies to global full-attention layers only — linear
    // (deltanet) layers and Gemma local windows stay untouched.
    bool use_csa(int l) const {
        return is_global_attn(l) && csa_ratio > 1 && csa_topk > 0;
    }
    int csa_win() const { return csa_window > 0 ? csa_window : sliding_window; }
    // CSA2 group role: ordinal-th CSA layer. The group head (Full)
    // produces the shared compressed stream + index keys; followers are
    // Reuse (share head's top-K too) or Reindex (own indexer Q).
    // Returns: <0 not CSA; 0 Full; 1 Reuse; 2 Reindex. src_out receives
    // the producer layer for followers.
    int csa_role(int l, int* src_out) const {
        if (!use_csa(l)) return -1;
        int ord = 0, head = -1;
        for (int i = 0; i <= l; ++i) {
            if (!use_csa(i)) continue;
            if (csa_group > 1 && ord % csa_group == 0) head = i;
            if (i == l) {
                if (src_out) *src_out = (ord % std::max(1, csa_group))
                                            ? head : -1;
                if (csa_group <= 1 || ord % csa_group == 0) return 0;
                return csa_reindex ? 2 : 1;
            }
            ++ord;
        }
        return -1;
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
    // ---- gemma4 fields ----
    c.model_type = j_str(o, "model_type", j_str(o, "model_family", ""));
    c.hidden_act = j_str(o, "hidden_activation", j_str(o, "hidden_act", ""));
    c.head_dim = j_int(o, "head_dim", c.head_dim);
    c.global_head_dim = j_int(o, "global_head_dim", c.head_dim);
    c.sliding_window = j_int(o, "sliding_window", c.sliding_window);
    c.num_kv_shared_layers =
        j_int(o, "num_kv_shared_layers", c.num_kv_shared_layers);
    c.ple_hidden = j_int(o, "hidden_size_per_layer_input", 0);
    c.ple_vocab = j_int(o, "vocab_size_per_layer_input", 0);
    c.final_logit_softcapping =
        (float)j_num(o, "final_logit_softcapping", 0.0);
    c.use_double_wide_mlp = j_bool(o, "use_double_wide_mlp", false);
    c.tie_embed = j_bool(o, "tie_word_embeddings", false);
    const JsonValue* rp = o ? o->get("rope_parameters") : nullptr;
    const JsonValue* rpf = rp ? rp->get("full_attention") : nullptr;
    c.rope_theta_full = (float)j_num(
        rpf, "rope_theta", j_num(o, "rope_theta_full", 0.0));
    c.rope_partial_full = (float)j_num(
        rpf, "partial_rotary_factor",
        j_num(o, "rope_partial_rotary_factor", 1.0));
    const double qpas = j_num(o, "query_pre_attn_scalar", 0.0);
    c.attention_scale = (float)j_num(
        o, "attention_scale",
        j_num(o, "attn_scale", qpas > 0.0 ? std::pow(qpas, -0.5) : 0.0));
    c.layer_types = j_strs(o, "layer_types");
    // ---- hybrid linear-attention + vision fields ----
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
    // v29 router z-loss (B133)
    c.moe_zloss_w = (float)j_num(o, "moe_z_loss_weight", c.moe_zloss_w);
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
    c.use_vision = j_bool(o, "use_vision", c.use_vision);
    c.vision_patch_dim = j_int(o, "vision_patch_dim", c.vision_patch_dim);
    c.vision_max_patches = j_int(o, "vision_max_patches", c.vision_max_patches);
    if (c.use_vision && (c.vision_patch_dim <= 0 || c.vision_max_patches <= 0))
        throw "model: bad vision geometry";
    // DeepSeek-V4.1-Flash CSA2 fields (use_csa_* naming, model.py-aligned)
    c.csa_ratio = j_int(o, "csa_compress_ratio", c.csa_ratio);
    c.csa_topk = j_int(o, "csa_topk", c.csa_topk);
    c.csa_window = j_int(o, "csa_window_size", c.csa_window);
    c.csa_rope_theta =
        (float)j_num(o, "csa_compress_rope_theta", c.csa_rope_theta);
    c.csa_group = j_int(o, "csa_share_group", c.csa_group);
    c.csa_reindex = j_bool(o, "csa_reindex", c.csa_reindex);
    c.csa_indexer = j_bool(o, "csa_indexer", c.csa_indexer);
    c.csa_indexer_w =
        (float)j_num(o, "csa_indexer_loss_weight", c.csa_indexer_w);
    if (c.csa_ratio > 0 && c.csa_ratio < 2)
        throw "model: csa_compress_ratio must be >=2";
    if (c.csa_ratio >= 2) {
        if (c.csa_topk <= 0) throw "model: csa_topk must be >0";
        if (c.csa_win() <= 0)
            throw "model: CSA needs csa_window_size or sliding_window_size";
        if (c.csa_group < 0) throw "model: bad csa_share_group";
        if (c.csa_indexer_w < 0.0f)
            throw "model: bad csa_indexer_loss_weight";
    }
    if (c.kv_heads <= 0) c.kv_heads = c.heads;
    if (c.heads <= 0) throw "model: bad head geometry";
    if (c.is_gemma4()) {
        if (c.moe_experts > 0 || j_bool(o, "enable_moe_block", false))
            throw "model: gemma4 MoE unsupported (dense lane)";
        if (c.head_dim <= 0) throw "model: gemma4 requires head_dim";
        if (c.global_head_dim <= 0) c.global_head_dim = c.head_dim;
        if (c.sliding_window <= 0)
            throw "model: gemma4 requires sliding_window";
        if (c.layer_types.empty())
            c.layer_types.assign((size_t)c.layers, "sliding_attention");
        if ((int)c.layer_types.size() != c.layers)
            throw "model: layer_types count != num_hidden_layers";
        for (const auto& t : c.layer_types)
            if (t != "sliding_attention" && t != "full_attention")
                throw "model: unknown layer_type";
        if (c.layer_types.back() != "full_attention")
            throw "model: gemma4 last layer must be full_attention";
        if (c.num_kv_shared_layers < 0 ||
            c.num_kv_shared_layers >= c.layers)
            throw "model: bad num_kv_shared_layers";
        if (c.rope_theta_full <= 0.0f) c.rope_theta_full = c.rope_theta;
        if (c.rope_partial_full <= 0.0f || c.rope_partial_full > 1.0f)
            throw "model: bad partial_rotary_factor";
        if (c.ple_vocab <= 0) c.ple_vocab = c.vocab;
    } else if (c.hidden % c.heads) {
        throw "model: bad head geometry";
    }
    if (c.full_attention_interval > 0 &&
        (c.lin_key_heads <= 0 || c.lin_key_dim <= 0 ||
         c.lin_value_heads <= 0 || c.lin_value_dim <= 0 ||
         c.lin_value_heads % c.lin_key_heads != 0))
        throw "model: bad linear-attention geometry";
    // Gemma-axis validation (fail-closed, same style as above). The
    // shared sliding_window field is exempt for the gemma4 profile: there
    // the window applies via layer_types/sliding_at, not the A4B
    // global/local interval axis.
    if (c.sliding_window > 0 && c.global_attn_interval <= 0 &&
        !c.is_gemma4())
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

// Gemma4 dense profile (HF modeling_gemma4 tensor roles):
//   norm1=input_layernorm, q_norm/k_norm=QK norms, norm_attn=post_attn,
//   norm2=pre_ffn, norm_ffn=post_ffn; shared (kv-owner != self) layers
//   carry no wk/wv/k_norm; PLE adds embed_ple + ple_model_proj +
//   ple_proj_norm + per-layer ple_gate/ple_proj/ple_post.
static void init_params_g4(Params& p, const ModelConfig& c,
                           std::mt19937& rng,
                           std::normal_distribution<float>& nd) {
    auto fill = [&](Tensor& t) { for (auto& x : t.d) x = nd(rng); };
    auto ones = [&](const std::string& n, int64_t d) {
        Tensor& t = p.add(n, {d});
        std::fill(t.d.begin(), t.d.end(), 1.0f);
    };
    fill(p.add("embed", {c.vocab, c.hidden}));
    if (!c.tie_embed) fill(p.add("lm_head", {c.vocab, c.hidden}));
    if (c.ple_hidden > 0) {
        fill(p.add("embed_ple",
                   {c.ple_vocab, (int64_t)c.layers * c.ple_hidden}));
        fill(p.add("ple_model_proj",
                   {(int64_t)c.layers * c.ple_hidden, c.hidden}));
        ones("ple_proj_norm", c.ple_hidden);
    }
    for (int l = 0; l < c.layers; ++l) {
        const int hd = c.hd_at(l);
        const bool shared = c.kv_shared(l);
        ones(ln(l, "norm1"), c.hidden);
        fill(p.add(ln(l, "wq"), {(int64_t)c.heads * hd, c.hidden}));
        if (!shared) {
            fill(p.add(ln(l, "wk"), {(int64_t)c.kv_heads * hd, c.hidden}));
            fill(p.add(ln(l, "wv"), {(int64_t)c.kv_heads * hd, c.hidden}));
            ones(ln(l, "k_norm"), hd);
        }
        fill(p.add(ln(l, "wo"), {c.hidden, (int64_t)c.heads * hd}));
        ones(ln(l, "q_norm"), hd);
        ones(ln(l, "norm_attn"), c.hidden);
        ones(ln(l, "norm2"), c.hidden);
        const int inter = c.inter_at(l);
        fill(p.add(ln(l, "w1"), {inter, c.hidden}));
        fill(p.add(ln(l, "w3"), {inter, c.hidden}));
        fill(p.add(ln(l, "w2"), {c.hidden, inter}));
        ones(ln(l, "norm_ffn"), c.hidden);
        if (c.ple_hidden > 0) {
            fill(p.add(ln(l, "ple_gate"), {c.ple_hidden, c.hidden}));
            fill(p.add(ln(l, "ple_proj"), {c.hidden, c.ple_hidden}));
            ones(ln(l, "ple_post"), c.hidden);
        }
    }
    ones("norm_f", c.hidden);
    p.alloc_adam();
}

static void init_params(Params& p, const ModelConfig& c, uint64_t seed) {
    std::mt19937 rng((uint32_t)seed);
    std::normal_distribution<float> nd(0.0f, 0.02f);
    auto fill = [&](Tensor& t) { for (auto& x : t.d) x = nd(rng); };
    if (c.is_gemma4()) { init_params_g4(p, c, rng, nd); return; }
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
            // CSA2 (V4.1-Flash): compressed-KV machinery on the producer
            // (Full) layer; Reindex followers carry only an indexer Q;
            // Reuse followers carry none. Compressor maps one chunk of r
            // pre-rope per-head keys [r*hd] -> one latent [hd].
            int csa_src = -1;
            int role = c.csa_role(l, &csa_src);
            if (role == 0) {
                const int r = c.csa_ratio;
                fill(p.add(ln(l, "wck"), {hd, (int64_t)r * hd}));
                fill(p.add(ln(l, "wcv"), {hd, (int64_t)r * hd}));
                if (c.csa_indexer) {
                    fill(p.add(ln(l, "wiq"), {hd, c.hidden}));
                    fill(p.add(ln(l, "wik"), {hd, hd}));
                }
            } else if (role == 2 && c.csa_indexer) {
                fill(p.add(ln(l, "wiq"), {hd, c.hidden}));
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

// Xingcheng formal C++ inference layer (P3b–P3f).
//
// The C++ layer owns model orchestration: bundle loading, tokenization,
// Transformer execution, KV cache, decoding and bounded output parsing.
// Primitive tensor math is delegated to the stable public C ABI in
// native/include/gptbridge_native.h; this layer does not reimplement the
// pure-C compute core or bypass its boundary.

#ifndef XINGCHENG_INFERENCE_HPP
#define XINGCHENG_INFERENCE_HPP

#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "gptbridge_kv_pool.h"

namespace xingcheng::inference {

struct TensorView {
    const double* data = nullptr;
    std::vector<int64_t> shape;

    int64_t size() const;
};

struct ModelConfig {
    int64_t vocab_size = 0;
    int64_t hidden_size = 0;
    int64_t intermediate_size = 0;
    int64_t num_hidden_layers = 0;
    int64_t num_attention_heads = 0;
    int64_t num_key_value_heads = 0;
    int64_t head_dim = 0;
    int64_t max_position_embeddings = 0;
    int64_t bos_token_id = 1;
    int64_t eos_token_id = 2;
    int64_t pad_token_id = 0;
    double rms_norm_eps = 1e-6;
    double rope_theta = 10000.0;
    bool use_swiglu = true;
    bool tie_word_embeddings = true;
    std::string norm_type = "rmsnorm";
    std::string hidden_act = "silu";
    std::string position_embedding_type = "rope";
    bool use_moe = false;
    int64_t moe_num_experts = 8;
    int64_t moe_top_k = 2;
    int64_t moe_layer_interval = 1;
    // DeepSeek-MoE style (v26): always-on shared experts plus
    // fine-grained routed experts whose intermediate width may be
    // narrower than the dense FFN. 0 = fall back to intermediate_size
    // (expert) / moe_expert_intermediate_size (shared).
    int64_t moe_num_shared_experts = 0;
    int64_t moe_expert_intermediate_size = 0;
    int64_t moe_shared_intermediate_size = 0;
    // Native vision early-fusion (v1): optional linear patch projection.
    // When use_vision, each span may carry vision_num_patches rows of
    // vision_patch_dim floats; they are projected to hidden_size and
    // prepended to the text embeddings (single decoder stream, causal).
    // Default off: text-only behaviour is bit-identical.
    bool use_vision = false;
    int64_t vision_patch_dim = 0;
    int64_t vision_max_patches = 0;
    // Fused hybrid Decoder (v27/XCN3 contract): gated DeltaNet
    // linear-attention layers interleaved with periodic gated full
    // attention.  full_attention_interval > 0 makes layer l full
    // attention iff (l+1) % interval == 0 (HF layer_types =
    // [linear*(interval-1), full]*n); every other layer runs the
    // DeltaNet recurrence.  0 = legacy all-full-attention dense layout.
    int64_t full_attention_interval = 0;
    bool attn_output_gate = false;    // full-attn q_proj emits [q|gate]
    bool qk_norm = false;             // per-head RMSNorm on q/k pre-RoPE
    double partial_rotary_factor = 1.0;   // fraction of head_dim rotated
    int64_t linear_num_key_heads = 0;    // DeltaNet key/query heads
    int64_t linear_key_head_dim = 0;     // DeltaNet key head dim
    int64_t linear_num_value_heads = 0;  // DeltaNet value heads
    int64_t linear_value_head_dim = 0;   // DeltaNet value head dim
    int64_t linear_conv_kernel_dim = 0;  // depthwise causal conv width
    bool shared_expert_gate = false;  // sigmoid gate on shared expert out
    // v28 fused router: Qwen3.5 per-expert sigmoid scoring replaces the
    // Qwen3-A3B softmax denominator — scores stay independent under many
    // fine-grained experts; deterministic top-k + renorm are unchanged.
    bool moe_router_sigmoid = false;
    // Qwen3-Coder YaRN context extension (v29/XCN8 contract, default
    // off): per-channel blend of raw and factor-interpolated rope
    // inv-freqs plus attention-factor mscale.
    double yarn_factor = 0.0;
    int64_t yarn_original_max_position_embeddings = 0;
    double yarn_beta_fast = 32.0;
    double yarn_beta_slow = 1.0;
    double yarn_attention_factor = 0.0;
    bool use_yarn() const {
        return yarn_factor > 1.0 &&
               yarn_original_max_position_embeddings > 0;
    }
    bool is_linear_layer(int64_t layer) const {
        return full_attention_interval > 0 && linear_num_key_heads > 0 &&
               ((layer + 1) % full_attention_interval) != 0;
    }
    // Any layer carries DeltaNet weights → the model owns per-slot
    // recurrent state and prefix cache entries (K/V only) cannot
    // reconstruct it.
    bool has_linear_layers() const {
        return full_attention_interval > 0 && linear_num_key_heads > 0;
    }
    // Trainer rope contract for fused bundles: rd channels rotated
    // (rotate-half pairing over the first rd channels); rd == head_dim
    // selects the trainer's interleaved full-rotary form.  rd <= 0 or
    // rd >= head_dim clamp to full, mirroring ModelConfig::rotary_dim().
    int64_t rotary_dim() const {
        int64_t rd = static_cast<int64_t>(
            static_cast<double>(head_dim) * partial_rotary_factor);
        return rd > 0 && rd < head_dim ? rd & ~int64_t{1} : head_dim;
    }
    // Fused-contract bundles (XCN3 fields present or interval set) use
    // the trainer's rope pairing; pre-v27 bundles keep the legacy
    // rotate-half kernel unchanged.
    bool fused_rope_contract() const {
        return full_attention_interval > 0 || attn_output_gate ||
               qk_norm || partial_rotary_factor < 1.0;
    }
    std::string quantization = "none";
};

struct SamplingConfig {
    bool do_sample = false;
    double temperature = 1.0;
    int64_t top_k = 0;
    double top_p = 1.0;
    double repetition_penalty = 1.0;
    uint64_t seed = 0;
};

class WeightBundle {
public:
    struct Blob;

    WeightBundle() = default;
    ~WeightBundle();
    WeightBundle(WeightBundle&&) noexcept;
    WeightBundle& operator=(WeightBundle&&) noexcept;
    WeightBundle(const WeightBundle&) = delete;
    WeightBundle& operator=(const WeightBundle&) = delete;

    static WeightBundle load(const std::string& manifest_path);

    const ModelConfig& config() const { return config_; }
    const TensorView& tensor(const std::string& name) const;
    bool has_tensor(const std::string& name) const;
    std::vector<std::string> tensor_names() const;
    int64_t weights_bytes() const { return weights_bytes_; }
    const std::string& weights_sha256() const { return weights_sha256_; }

private:
    struct TensorInfo {
        int64_t offset = 0;
        int64_t bytes = 0;
        std::vector<int64_t> shape;
    };

    ModelConfig config_;
    std::unordered_map<std::string, TensorInfo> tensors_;
    std::unordered_map<std::string, TensorView> views_;
    // Owned dequantized weights for int8/int4_packed tensors — deque keeps
    // element addresses stable so TensorView::data stays valid as it grows.
    std::deque<std::vector<double>> owned_tensors_;
    std::unique_ptr<Blob> blob_;
    int64_t weights_bytes_ = 0;
    std::string weights_sha256_;
};

class ByteLevelBPETokenizer {
public:
    static ByteLevelBPETokenizer load(const std::string& tokenizer_json_path);

    std::vector<int64_t> encode(
        const std::string& text,
        bool add_bos = true,
        bool add_eos = false,
        int64_t max_length = 0) const;
    std::string decode(const std::vector<int64_t>& ids, bool skip_special = true) const;
    int64_t vocab_size() const { return vocab_size_; }

private:
    struct SpecialToken {
        std::string text;
        int64_t id = 0;
    };

    std::unordered_map<std::string, int64_t> vocab_;
    std::vector<std::string> id_to_token_;
    std::vector<std::pair<std::string, std::string>> merges_;
    std::unordered_map<std::string, int64_t> merge_rank_;
    std::vector<SpecialToken> special_tokens_;
    // Ids of special_tokens_ actually present in vocab_ — decode()
    // skip_special consults this set instead of a hardcoded id range, so
    // ordinary tokens (bundle vocab: '!' '"' '#' '$' '%' at ids 4-8) are
    // never dropped when the bundle carries fewer special tokens.
    std::unordered_set<int64_t> special_ids_;
    std::vector<std::string> byte_to_token_;
    std::unordered_map<std::string, unsigned char> token_to_byte_;
    int64_t vocab_size_ = 0;

    std::vector<int64_t> encode_segment(const std::string& text) const;
};

class NativeInferenceEngine {
public:
    NativeInferenceEngine() = default;
    ~NativeInferenceEngine();

    void load(const std::string& bundle_dir);
    void unload();
    bool loaded() const { return bundle_ != nullptr; }
    bool cuda_active() const;

    std::vector<int64_t> encode(
        const std::string& text,
        bool add_bos = true,
        bool add_eos = false,
        int64_t max_length = 0) const;
    std::string decode(const std::vector<int64_t>& ids, bool skip_special = true) const;
    std::vector<double> logits(const std::vector<int64_t>& input_ids);
    // Vision early-fusion prefill probe: text ids + num_patches rows of
    // patch_dim doubles → last-position logits. Pure feedforward (no KV
    // writes); throws VISION_* on contract violation. This is the only
    // entry that attaches vision — text-only callers are unaffected.
    std::vector<double> forward_vision_logits(
        const std::vector<int64_t>& input_ids,
        const std::vector<double>& patches,
        int64_t num_patches);
    // Teacher-forced next-token NLL: one packed forward (no KV writes),
    // returns {sum of -log p(id[i+1] | id[0..i]), scored tokens}.
    // perplexity = exp(nll / count); deterministic, mirrors the Python
    // eval-suite metric (native_eval_suite.evaluate_checkpoint).
    std::pair<double, int64_t> sequence_nll(
        const std::vector<int64_t>& input_ids);
    // G41 layerwise parity probe: RMS of the hidden stream at each stage
    // (embedding, each transformer layer output, final norm). No KV writes.
    std::vector<double> layer_metrics(const std::vector<int64_t>& input_ids);
    // Phase-5E per-module parity probe: RMS at each module boundary —
    // [embedding] + per-layer {input_norm, post-RoPE q, attention out
    // (post-o_proj, pre-residual), post_attention_norm, MLP out
    // (pre-residual)} + [final_norm]. For non-RoPE configs the q tap
    // reads the post-projection queries. No KV writes.
    std::vector<double> module_metrics(const std::vector<int64_t>& input_ids);
    std::vector<int64_t> generate(
        const std::vector<int64_t>& prompt_ids,
        int64_t max_new_tokens,
        const SamplingConfig& sampling);
    // R9 batch>1: packed prefill over all prompts, then continuous decode —
    // every step packs the active sequences' tokens into one forward and
    // finished sequences drop out (EOS/max), releasing their KV blocks.
    // Bounded by kMaxBatchSeqs; prefix cache stays a single-sequence path.
    std::vector<std::vector<int64_t>> generate_batch(
        const std::vector<std::vector<int64_t>>& prompts,
        int64_t max_new_tokens,
        const SamplingConfig& sampling);
    std::string generate_text(
        const std::string& prompt,
        int64_t max_new_tokens,
        const SamplingConfig& sampling);

    int64_t memory_bytes() const;
    int64_t kv_memory_bytes() const;
    std::string describe() const;
    void set_kv_memory_limit(int64_t bytes);
    void set_prefix_cache_limit(int64_t max_entries, int64_t max_bytes);

private:
    struct LayerWeights {
        TensorView input_norm;
        TensorView q_proj;
        TensorView k_proj;
        TensorView v_proj;
        TensorView o_proj;
        TensorView post_norm;
        TensorView gate_proj;
        TensorView up_proj;
        TensorView down_proj;
        // R5 sparse MoE: when is_moe the dense MLP views are empty and the
        // router + per-expert SwiGLU weights are used instead.
        bool is_moe = false;
        TensorView router;
        std::vector<TensorView> expert_gate;
        std::vector<TensorView> expert_up;
        std::vector<TensorView> expert_down;
        std::vector<double> router_t;
        // Column-fused transposed weights: one GEMM produces [gate|up]
        // (or [q|k|v]) per input row. Every output column keeps the exact
        // same k-length dot product as the unfused weights; the win is
        // fewer host→device round trips per forward pass.
        std::vector<std::vector<double>> expert_gate_up_t;
        std::vector<std::vector<double>> expert_down_t;
        // Always-on shared experts (DeepSeek-MoE): weight-1.0
        // contribution on every token, mirroring modules/moe.py.
        std::vector<TensorView> shared_gate;
        std::vector<TensorView> shared_up;
        std::vector<TensorView> shared_down;
        std::vector<std::vector<double>> shared_gate_up_t;
        std::vector<std::vector<double>> shared_down_t;
        std::vector<double> qkv_t;
        std::vector<double> o_proj_t;
        std::vector<double> gate_up_t;
        std::vector<double> down_proj_t;
        // v27 gated DeltaNet (linear attention): populated when
        // ModelConfig::is_linear_layer(i); full-attention views stay
        // empty on those layers.
        bool is_linear = false;
        TensorView lin_in_proj_qkv;   // [kh*(2kd + vd*ratio), hidden]
        TensorView lin_in_proj_z;     // [vh*vd, hidden]
        TensorView lin_in_proj_a;     // [vh, hidden]
        TensorView lin_in_proj_b;     // [vh, hidden]
        TensorView lin_conv1d;        // [conv_dim, kernel]
        TensorView lin_a_log;         // [vh]
        TensorView lin_dt_bias;       // [vh]
        TensorView lin_out_norm;      // [vd]
        TensorView lin_out_proj;      // [hidden, vh*vd]
        // Load-time fused operand: one GEMM produces [qkv|z|a|b] per
        // input row — the deltanet projection block runs as a single
        // matmul dispatch instead of four.
        std::vector<double> lin_fused_t;
        std::vector<double> lin_out_proj_t;
        // Full-attention extras (v27): per-head q/k RMSNorm weights.
        TensorView q_norm;            // [head_dim]
        TensorView k_norm;            // [head_dim]
        // v27 shared-expert sigmoid gate [1, hidden]; empty unless the
        // MoE layer opts in via shared_expert_gate.
        TensorView shared_expert_gate;
        std::vector<double> shared_expert_gate_t;
    };

    // Per-slot DeltaNet recurrence state (linear layers only): the
    // causal conv's last kernel-1 raw input rows and the delta-rule
    // state S[kd x vd] per value head.  Mirroring the KV block tables,
    // state persists only across append_cache forwards and is released
    // with the slot.
    struct LinLayerState {
        std::vector<double> conv_tail;   // [(kernel-1) * conv_dim]
        std::vector<double> s;           // [vh * kd * vd]
        int64_t tokens = 0;              // positions folded into s
    };

    struct PrefixEntry {
        std::vector<int64_t> tokens;
        std::vector<double> k;
        std::vector<double> v;
        uint64_t tick = 0;
    };

    // R9: one span = one sequence's tokens inside a packed forward call.
    // Vision early-fusion: vision_patches (flat vision_num_patches x
    // ModelConfig::vision_patch_dim, row-major) is projected and prepended
    // to the text embeddings. Prefix-cache paths never attach vision, so a
    // vision span always recomputes (no false prefix hits by construction).
    struct BatchSpan {
        int64_t slot = 0;
        const std::vector<int64_t>* ids = nullptr;
        int64_t position_offset = 0;
        bool append_cache = false;
        const std::vector<double>* vision_patches = nullptr;
        int64_t vision_num_patches = 0;
    };
    static constexpr int64_t kMaxBatchSeqs = 64;

    std::unique_ptr<WeightBundle> bundle_;
    std::unique_ptr<ByteLevelBPETokenizer> tokenizer_;
    std::vector<LayerWeights> layers_;
    std::vector<PrefixEntry> prefix_cache_;
    int64_t prefix_cache_max_entries_ = 8;
    int64_t prefix_cache_max_bytes_ = 256LL * 1024 * 1024;
    uint64_t prefix_tick_ = 0;
    int64_t prefix_hits_ = 0;
    int64_t prefix_misses_ = 0;
    TensorView embedding_;
    TensorView final_norm_;
    TensorView lm_head_;
    std::vector<double> lm_head_t_;
    // Vision early-fusion: raw [hidden x patch_dim] view plus transposed
    // [patch_dim x hidden] GEMM operand. Empty unless use_vision.
    TensorView vision_patch_proj_;
    std::vector<double> vision_patch_proj_t_;
    // R6 paged KV: shared block table maps logical position blocks to
    // physical blocks covering all layers; blocks allocate on demand and
    // return to kv_free_blocks_ on reset_cache (bounded, audited via
    // kv_memory_bytes / KV_MEMORY_LIMIT_EXCEEDED).
    gptbridge_kv_pool* kv_pool_ = nullptr;
    // R9: per-slot block tables so batched sequences share the pool without
    // aliasing each other's KV; slot 0 serves the single-sequence path.
    std::vector<std::vector<int32_t>> kv_block_tables_;
    std::vector<bool> kv_slot_active_;
    std::vector<int64_t> kv_lens_;
    // Dense ordinal of each full-attention layer (linear layers map to
    // -1): KV blocks are sized by the full-attention count only, so a
    // hybrid layout does not reserve dead regions for DeltaNet layers.
    std::vector<int64_t> kv_layer_ord_;
    // Per-slot DeltaNet states, parallel to kv_block_tables_:
    // lin_states_[slot][layer] — non-linear entries stay empty.
    std::vector<std::vector<LinLayerState>> lin_states_;
    int64_t kv_block_stride_ = 0;
    int64_t kv_limit_bytes_ = 0;
    // KV INT8 (opt-in via governed env): per-token/per-head symmetric
    // quantization — packed elem = int8[align8(head_dim)] + double scale,
    // shrinking the paged block stride ~8x so kv_memory_bytes reflects the
    // real footprint. Read sites dequantize via KvSrc dispatch.
    bool kv_int8_ = false;
    int64_t kv_elem_stride_bytes_ = 0;
    // P1-1② device-resident KV (opt-in via governed env): when active the
    // slot-0 fp64 KV is mirrored on-device and attention runs in the CUDA
    // kernel; host pool stays the source of truth. Only reachable when
    // !kv_int8_ (int8 format unsupported on device → load fails closed).
    bool kv_device_active_ = false;
    // CUDA session ownership: the weight cache / KV buffers / cuBLAS handle
    // are process-global, so only the instance that activated CUDA may tear
    // them down — otherwise destructing a stale engine would wipe a live
    // engine's device state (write-through mirror then fails mid-forward).
    bool cuda_session_owned_ = false;
    std::vector<int64_t> sequence_;

    struct KvSrc {
        const double* fp = nullptr;
        const int8_t* q8 = nullptr;
        double scale = 1.0;
    };

    // Forward-scratch arena: per-layer temporaries live in persistent
    // buffers whose capacity survives across layers and decode steps —
    // the layer loop performs zero per-layer heap allocation once warm
    // (resize() keeps capacity).  Buffers are write-through temporaries:
    // contents never carry meaning across forward calls, so reusing them
    // is bit-identical.  The engine is single-threaded per call chain
    // (kv state is mutable), so member scratch needs no isolation.
    struct ForwardScratch {
        std::vector<double> hidden;
        std::vector<double> normed;
        std::vector<double> qkv;
        std::vector<double> q_flat;
        std::vector<double> k_flat;
        std::vector<double> v_flat;
        std::vector<double> q_heads;
        std::vector<double> k_heads;
        std::vector<double> v_heads;
        std::vector<double> q_rope;
        std::vector<double> k_rope;
        std::vector<double> attn_flat;
        std::vector<double> attn_out;
        std::vector<KvSrc> k_srcs;
        std::vector<KvSrc> v_srcs;
        std::vector<double> tile_scores;
        std::vector<double> acc;
        std::vector<double> gate_up;
        std::vector<double> mlp_in;
        std::vector<double> mlp_out;
        // MoE-only lanes (dense models never touch these).
        std::vector<double> moe_probs;
        std::vector<int64_t> moe_order;
        std::vector<int64_t> moe_top_idx;
        std::vector<double> moe_top_w;
        std::vector<int64_t> moe_group_count;
        std::vector<int64_t> moe_group_offset;
        std::vector<int64_t> moe_group_fill;
        std::vector<double> moe_grouped_in;
        std::vector<int64_t> moe_row_token;
        std::vector<double> moe_row_weight;
        std::vector<double> moe_gate_up;
        std::vector<double> moe_act;
        std::vector<double> moe_grouped_out;
        std::vector<double> moe_mlp_out;
        // Always-on shared experts.
        std::vector<double> shared_sgu;
        std::vector<double> shared_sg;
        std::vector<double> shared_sd;
        // Vision early-fusion prefix projection output.
        std::vector<double> vision_prefix;
        // v27 fused-hybrid lanes (dense models never touch these).
        std::vector<double> lin_fused;      // fused [qkv|z|a|b] rows
        std::vector<double> lin_qkvz;       // [T, kh*group_sz]
        std::vector<double> lin_z;          // [T, vh*vd]
        std::vector<double> lin_a;          // [T, vh]
        std::vector<double> lin_b;          // [T, vh]
        std::vector<double> lin_conv_in;    // [T, conv_dim] flat q|k|v
        std::vector<double> lin_conv_pad;   // [(K-1)+T, conv_dim]
        std::vector<double> lin_conv_out;   // [T, conv_dim]
        std::vector<double> lin_qn;         // [T, vh*kd] normed+scaled
        std::vector<double> lin_kn;         // [T, vh*kd]
        std::vector<double> lin_v;          // [T, vh*vd]
        std::vector<double> lin_decay;      // [T, vh]
        std::vector<double> lin_beta;       // [T, vh]
        std::vector<double> lin_o;          // [T, vh*vd] scan output
        std::vector<double> lin_on;         // [T, vh*vd] normed+gated
        std::vector<double> attn_gate;      // [T, q_dim] gate logits
        std::vector<double> qk_tmp;         // per-head qk_norm/scan scratch
        std::vector<double> shared_sig;     // [T] shared-expert gate
    };
    ForwardScratch fs_;
    // RoPE frequency-base cache (dim/theta keyed): pow() once per model.
    std::vector<double> rope_base_;
    int64_t rope_base_dim_ = 0;
    double rope_base_theta_ = 0.0;

    void validate_supported() const;
    void reset_cache();
    int32_t kv_alloc_block();
    int64_t kv_alloc_slot();
    void kv_free_slot(int64_t slot);
    void kv_ensure_position(int64_t slot, int64_t position);
    char* kv_slot_bytes(
        int64_t slot, bool key_cache,
        int64_t layer, int64_t position, int64_t head);
    void kv_write(
        int64_t slot, bool key_cache, int64_t layer, int64_t position,
        int64_t head, const double* src);
    KvSrc kv_src(
        int64_t slot, bool key_cache,
        int64_t layer, int64_t position, int64_t head);
    void kv_read_head(
        int64_t slot, bool key_cache, int64_t layer, int64_t position,
        int64_t head, double* out);
    std::vector<double> forward_last_logits(
        const std::vector<int64_t>& input_ids,
        int64_t position_offset,
        bool append_cache);
    std::vector<double> forward_batch_hidden(
        const std::vector<BatchSpan>& spans,
        std::vector<double>* layer_rms = nullptr,
        std::vector<double>* module_rms = nullptr);
    std::vector<std::vector<double>> forward_batch_last_logits(
        const std::vector<BatchSpan>& spans);
    std::vector<double> forward_hidden(
        const std::vector<int64_t>& input_ids,
        int64_t position_offset,
        bool append_cache,
        std::vector<double>* layer_rms = nullptr,
        std::vector<double>* module_rms = nullptr);
    int64_t sample_next(
        const std::vector<double>& logits,
        const std::vector<int64_t>& previous,
        const SamplingConfig& sampling,
        uint64_t& rng_state) const;
};

std::string parse_generated_output(const std::string& text, int64_t max_json_bytes);

}  // namespace xingcheng::inference

#endif  // XINGCHENG_INFERENCE_HPP

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
    std::string quantization = "none";
    // Gemma4 profile (architecture="gemma4"): hybrid sliding/full
    // attention with per-layer-type head_dim and RoPE, QK-norm,
    // post-norms, KV-sharing (Q-only tail layers), Per-Layer
    // Embeddings, sqrt(hidden) embedding scale, attention scaling 1.0
    // and final logit softcapping. layer_types empty -> legacy uniform
    // path. Values mirror the Gemma4 text_config field names.
    std::string model_family;               // "gemma4_text" (HF model_type)
    std::vector<std::string> layer_types;   // "sliding_attention"|"full_attention"
    int64_t sliding_window = 0;
    int64_t global_head_dim = 0;            // head_dim of full layers
    double rope_theta_full = 1000000.0;
    double rope_partial_factor_full = 1.0;  // proportional RoPE fraction
    int64_t num_kv_shared_layers = 0;
    int64_t hidden_size_per_layer_input = 0;
    int64_t vocab_size_per_layer_input = 0;
    double final_logit_softcapping = 0.0;   // 0 = disabled
    double embedding_scale = 0.0;           // 0 = 1.0 (no scale)
    double attention_scale = 0.0;           // 0 = 1/sqrt(head_dim)
    bool attention_k_eq_v = false;
    bool use_double_wide_mlp = false;

    bool is_gemma4() const {
        return !layer_types.empty() || model_family == "gemma4_text" ||
               model_family == "gemma4";
    }
    bool layer_sliding(int64_t i) const {
        return layer_types[static_cast<size_t>(i)] == "sliding_attention";
    }
    int64_t layer_head_dim(int64_t i) const {
        return layer_sliding(i) ? head_dim : global_head_dim;
    }
    // Head dim a layer's K/V rows carry: shared layers reuse their
    // type anchor's geometry, which equals their own type's head_dim.
    int64_t layer_kv_dim(int64_t i) const {
        return num_key_value_heads * layer_head_dim(i);
    }
    int64_t first_kv_shared_layer() const {
        return num_hidden_layers - num_kv_shared_layers;
    }
    bool layer_kv_shared(int64_t i) const {
        return num_kv_shared_layers > 0 &&
               i >= first_kv_shared_layer();
    }
    // Anchor layer that produces the shared full-length KV for a
    // type: the last non-shared layer of the same layer_type
    // (HF Gemma4 `store_full_length_kv` semantics). -1 = none.
    int64_t kv_anchor(int64_t i) const {
        const std::string& type = layer_types[static_cast<size_t>(i)];
        for (int64_t j = first_kv_shared_layer() - 1; j >= 0; --j) {
            if (layer_types[static_cast<size_t>(j)] == type) return j;
        }
        return -1;
    }
    // Max head_dim across layers (KV block element stride).
    int64_t max_head_dim() const {
        return global_head_dim > head_dim ? global_head_dim : head_dim;
    }
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

    // Native thinking (latent-space reasoning): after the prompt prefill the
    // engine runs `think_steps` continuous-thought iterations — each feeds
    // the last hidden state back as the next input embedding — then
    // decodes `branches` parallel hypothesis continuations on private KV
    // slots, self-verifies them by mean token logprob (model confidence,
    // the latent Chain-of-Verification signal), and returns the winning
    // branch's tokens. Bounded: think_steps<=32, branches<=8.
    struct ThinkingResult {
        std::vector<int64_t> answer_ids;
        int64_t chosen_branch = -1;
        int64_t think_steps = 0;
        std::vector<double> branch_scores;
        std::vector<std::vector<int64_t>> branch_ids;
    };
    ThinkingResult generate_thinking(
        const std::vector<int64_t>& prompt_ids,
        int64_t think_steps,
        int64_t branches,
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
        // Gemma4 layer wiring (unused on the legacy path): per-head
        // QK RMSNorm scales, the post-attention norm applied to the
        // o_proj output before the residual add, the pre/post FFN
        // norms, and the PLE correction branch (gate Linear ->
        // gelu -> *per_layer_input -> projection -> norm -> residual).
        // kv_shared layers carry no k/v projections or k/v norms.
        bool kv_shared = false;
        int64_t g4_head_dim = 0;
        int64_t g4_q_dim = 0;
        int64_t g4_kv_dim = 0;
        int64_t kv_anchor_layer = -1;
        TensorView q_norm;
        TensorView k_norm;
        TensorView post_attn_norm;
        TensorView pre_ffn_norm;
        TensorView post_ffn_norm;
        TensorView ple_gate;
        TensorView ple_proj;
        TensorView ple_post_norm;
        std::vector<double> q_proj_t;
        std::vector<double> k_proj_t;
        std::vector<double> v_proj_t;
        std::vector<double> ple_gate_t;
        std::vector<double> ple_proj_t;
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
        // Latent-thinking input (COCONUT-style continuous thought): when
        // non-null it must hold seq*hidden_size doubles that replace the
        // token-embedding rows verbatim — a previous hidden state is fed
        // straight back into hidden space without detokenizing. ids still
        // bound the span length; the token ids themselves are ignored.
        const std::vector<double>* embed_override = nullptr;
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
    // Gemma4 model-level PLE tensors (empty views when disabled).
    TensorView embed_per_layer_;
    TensorView ple_model_projection_;
    TensorView ple_projection_norm_;
    std::vector<double> ple_model_projection_t_;
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
    // Slot-addressed variants used by the thinking/branching lane (slot 0
    // is the single-sequence namespace; branch slots are private).
    std::vector<double> forward_hidden_span(
        const BatchSpan& span);
    std::vector<double> forward_last_logits_span(
        const BatchSpan& span);
    // Deep-copy `count` positions of KV rows between slots (branch
    // forking): every layer/head row, fp64 path only.
    void kv_copy_slot(int64_t dst, int64_t src, int64_t count);
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
    std::vector<double> forward_batch_hidden_gemma4(
        const std::vector<BatchSpan>& spans,
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

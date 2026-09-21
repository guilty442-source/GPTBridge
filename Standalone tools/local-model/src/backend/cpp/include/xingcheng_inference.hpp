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
#include <memory>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

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
    std::vector<std::string> byte_to_token_;
    std::unordered_map<std::string, unsigned char> token_to_byte_;
    int64_t vocab_size_ = 0;

    std::vector<int64_t> encode_segment(const std::string& text) const;
};

class NativeInferenceEngine {
public:
    NativeInferenceEngine() = default;

    void load(const std::string& bundle_dir);
    void unload();
    bool loaded() const { return bundle_ != nullptr; }

    std::vector<int64_t> encode(
        const std::string& text,
        bool add_bos = true,
        bool add_eos = false,
        int64_t max_length = 0) const;
    std::string decode(const std::vector<int64_t>& ids, bool skip_special = true) const;
    std::vector<double> logits(const std::vector<int64_t>& input_ids);
    std::vector<int64_t> generate(
        const std::vector<int64_t>& prompt_ids,
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
        std::vector<double> q_proj_t;
        std::vector<double> k_proj_t;
        std::vector<double> v_proj_t;
        std::vector<double> o_proj_t;
        std::vector<double> gate_proj_t;
        std::vector<double> up_proj_t;
        std::vector<double> down_proj_t;
    };

    std::unique_ptr<WeightBundle> bundle_;
    std::unique_ptr<ByteLevelBPETokenizer> tokenizer_;
    std::vector<LayerWeights> layers_;
    TensorView embedding_;
    TensorView final_norm_;
    TensorView lm_head_;
    std::vector<double> lm_head_t_;
    std::vector<double> kv_k_;
    std::vector<double> kv_v_;
    int64_t kv_len_ = 0;
    int64_t kv_limit_bytes_ = 0;
    std::vector<int64_t> sequence_;

    void validate_supported() const;
    void reset_cache();
    std::vector<double> forward_last_logits(
        const std::vector<int64_t>& input_ids,
        int64_t position_offset,
        bool append_cache);
    std::vector<double> forward_hidden(
        const std::vector<int64_t>& input_ids,
        int64_t position_offset,
        bool append_cache);
    int64_t sample_next(
        const std::vector<double>& logits,
        const std::vector<int64_t>& previous,
        const SamplingConfig& sampling,
        uint64_t& rng_state) const;
};

std::string parse_generated_output(const std::string& text, int64_t max_json_bytes);

}  // namespace xingcheng::inference

#endif  // XINGCHENG_INFERENCE_HPP

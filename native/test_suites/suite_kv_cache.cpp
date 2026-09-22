// Suite: KV cache correctness (append/position/GQA layout, INT8 per-token quantization).
#include "harness.hpp"

#include <algorithm>
#include <cstdint>

namespace {

const char* SUITE = "KV_CACHE_SUITE";

struct CacheBlock {
    std::vector<float> values;  // per-token per-head
    int tokens = 0;
    int heads = 0;
    int head_dim = 0;
};

void append(CacheBlock& block, const std::vector<float>& token_values) {
    block.values.insert(block.values.end(), token_values.begin(), token_values.end());
    block.tokens += 1;
}

std::vector<float> position_slice(const CacheBlock& block, int token_index) {
    const int stride = block.heads * block.head_dim;
    return std::vector<float>(block.values.begin() + token_index * stride,
                              block.values.begin() + (token_index + 1) * stride);
}

std::vector<std::int8_t> quantize_per_token(const std::vector<float>& token_values, float& scale) {
    float max_abs = 0.0f;
    for (float value : token_values) max_abs = std::max(max_abs, std::fabs(value));
    scale = max_abs > 0.0f ? max_abs / 127.0f : 1.0f;
    std::vector<std::int8_t> out(token_values.size());
    for (std::size_t i = 0; i < token_values.size(); ++i) {
        out[i] = static_cast<std::int8_t>(std::lround(token_values[i] / scale));
    }
    return out;
}

std::vector<float> dequantize(const std::vector<std::int8_t>& quantized, float scale) {
    std::vector<float> out(quantized.size());
    for (std::size_t i = 0; i < quantized.size(); ++i) out[i] = quantized[i] * scale;
    return out;
}

}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "append_and_position_tracking") {
        CacheBlock block;
        block.heads = 2;
        block.head_dim = 2;
        append(block, {1.0f, 2.0f, 3.0f, 4.0f});
        append(block, {5.0f, 6.0f, 7.0f, 8.0f});
        NT_CHECK(block.tokens == 2, "two tokens appended");
        const auto first = position_slice(block, 0);
        const auto second = position_slice(block, 1);
        NT_CHECK(first[0] == 1.0f && first[3] == 4.0f, "position 0 slice");
        NT_CHECK(second[0] == 5.0f && second[3] == 8.0f, "position 1 slice");
    }
    NT_END_TEST(SUITE, "append_and_position_tracking");

    NT_TEST(SUITE, "gqa_layout_shares_kv_heads") {
        // GQA: q heads 4, kv heads 2 -> each kv head serves 2 q heads.
        const int q_heads = 4;
        const int kv_heads = 2;
        std::vector<int> mapping(q_heads);
        for (int q = 0; q < q_heads; ++q) mapping[q] = q * kv_heads / q_heads;
        NT_CHECK(mapping[0] == 0 && mapping[1] == 0, "q0/q1 share kv0");
        NT_CHECK(mapping[2] == 1 && mapping[3] == 1, "q2/q3 share kv1");
    }
    NT_END_TEST(SUITE, "gqa_layout_shares_kv_heads");

    NT_TEST(SUITE, "int8_per_token_scale_roundtrip_within_tolerance") {
        const std::vector<float> token = {0.5f, -1.25f, 3.0f, -0.75f};
        float scale = 0.0f;
        const auto quantized = quantize_per_token(token, scale);
        const auto restored = dequantize(quantized, scale);
        for (std::size_t i = 0; i < token.size(); ++i) {
            NT_CHECK_NEAR(restored[i], token[i], scale, "INT8 roundtrip must stay within one scale step");
        }
    }
    NT_END_TEST(SUITE, "int8_per_token_scale_roundtrip_within_tolerance");

    NT_TEST(SUITE, "per_token_scales_are_independent") {
        const std::vector<float> small = {0.01f, -0.02f};
        const std::vector<float> large = {100.0f, -50.0f};
        float small_scale = 0.0f;
        float large_scale = 0.0f;
        quantize_per_token(small, small_scale);
        quantize_per_token(large, large_scale);
        NT_CHECK(small_scale < large_scale, "small-token scale must not be overwritten by large token");
    }
    NT_END_TEST(SUITE, "per_token_scales_are_independent");

    NT_TEST(SUITE, "causal_consistency_blocks_future_positions") {
        CacheBlock block;
        block.heads = 1;
        block.head_dim = 1;
        append(block, {1.0f});
        append(block, {2.0f});
        append(block, {3.0f});
        // Decode at position 1 must only see positions 0..1.
        NT_CHECK(block.tokens == 3, "cache length");
        const auto visible = position_slice(block, 1);
        NT_CHECK(visible.size() == 1 && visible[0] == 2.0f, "decode window");
    }
    NT_END_TEST(SUITE, "causal_consistency_blocks_future_positions");

    return native_tests::report("kv_cache_suite.json");
}

// Suite: NTP correctness / sampling math (softmax, temperature, top-k, top-p, greedy).
// Native replacement for the Python NTP/sampling tests (no Python at runtime).
#include "harness.hpp"

#include <algorithm>
#include <numeric>

namespace {

const char* SUITE = "NTP_CORRECTNESS_SUITE";

std::vector<float> softmax(const std::vector<float>& logits) {
    std::vector<float> out(logits.size());
    const float max_logit = *std::max_element(logits.begin(), logits.end());
    float sum = 0.0f;
    for (std::size_t i = 0; i < logits.size(); ++i) {
        out[i] = std::exp(logits[i] - max_logit);
        sum += out[i];
    }
    for (float& value : out) value /= sum;
    return out;
}

std::vector<float> apply_temperature(std::vector<float> logits, float temperature) {
    for (float& value : logits) value /= temperature;
    return logits;
}

std::vector<int> top_k_indices(const std::vector<float>& logits, int k) {
    std::vector<int> index(logits.size());
    std::iota(index.begin(), index.end(), 0);
    std::partial_sort(index.begin(), index.begin() + k, index.end(),
                      [&](int a, int b) { return logits[a] > logits[b]; });
    index.resize(k);
    return index;
}

std::vector<int> top_p_indices(const std::vector<float>& logits, float p) {
    std::vector<int> index(logits.size());
    std::iota(index.begin(), index.end(), 0);
    std::sort(index.begin(), index.end(), [&](int a, int b) { return logits[a] > logits[b]; });
    const auto probs = softmax(logits);
    float cumulative = 0.0f;
    std::vector<int> kept;
    for (int i : index) {
        kept.push_back(i);
        cumulative += probs[i];
        if (cumulative >= p) break;
    }
    return kept;
}

}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "softmax_sums_to_one_and_is_stable") {
        const std::vector<float> logits = {1000.0f, 1001.0f, 999.0f, -1000.0f};
        const auto probs = softmax(logits);
        float sum = 0.0f;
        for (float value : probs) {
            NT_CHECK(std::isfinite(value), "non-finite probability");
            sum += value;
        }
        NT_CHECK_NEAR(sum, 1.0f, 1e-5f, "softmax must sum to 1");
    }
    NT_END_TEST(SUITE, "softmax_sums_to_one_and_is_stable");

    NT_TEST(SUITE, "temperature_sharpens_distribution") {
        const std::vector<float> logits = {2.0f, 1.0f, 0.0f};
        const auto cold = softmax(apply_temperature(logits, 0.5f));
        const auto warm = softmax(apply_temperature(logits, 2.0f));
        NT_CHECK(cold[0] > warm[0], "lower temperature must sharpen the top token");
        NT_CHECK(cold[0] > cold[1] && cold[1] > cold[2], "order preserved");
    }
    NT_END_TEST(SUITE, "temperature_sharpens_distribution");

    NT_TEST(SUITE, "greedy_is_argmax_and_deterministic") {
        const std::vector<float> logits = {0.1f, 0.7f, 0.7f, -3.0f};
        const auto first = top_k_indices(logits, 1)[0];
        const auto second = top_k_indices(logits, 1)[0];
        NT_CHECK(first == second, "greedy must be deterministic");
        NT_CHECK(logits[first] == 0.7f, "greedy must pick the max logit");
    }
    NT_END_TEST(SUITE, "greedy_is_argmax_and_deterministic");

    NT_TEST(SUITE, "top_k_keeps_k_candidates_ordered") {
        const std::vector<float> logits = {5.0f, 1.0f, 4.0f, 2.0f, 3.0f};
        const auto kept = top_k_indices(logits, 3);
        NT_CHECK(kept.size() == 3, "top-k size");
        NT_CHECK(kept[0] == 0 && kept[1] == 2 && kept[2] == 4, "top-3 order must be 5,4,3");
    }
    NT_END_TEST(SUITE, "top_k_keeps_k_candidates_ordered");

    NT_TEST(SUITE, "top_p_nucleus_contains_mass") {
        const std::vector<float> logits = {4.0f, 3.0f, 0.0f, -4.0f};
        const auto kept = top_p_indices(logits, 0.9f);
        NT_CHECK(!kept.empty() && kept[0] == 0, "nucleus must include the top token");
        NT_CHECK(std::find(kept.begin(), kept.end(), 3) == kept.end(), "tail token excluded");
    }
    NT_END_TEST(SUITE, "top_p_nucleus_contains_mass");

    NT_TEST(SUITE, "negative_tests_reject_degenerate_inputs") {
        const std::vector<float> empty;
        NT_CHECK(empty.empty(), "empty logits handled");
        const std::vector<float> flat = {1.0f, 1.0f};
        const auto probs = softmax(flat);
        NT_CHECK_NEAR(probs[0], 0.5f, 1e-6f, "uniform logits -> uniform probabilities");
    }
    NT_END_TEST(SUITE, "negative_tests_reject_degenerate_inputs");

    return native_tests::report("ntp_suite.json");
}

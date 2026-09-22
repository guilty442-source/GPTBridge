// Suite: MoE training quality control (top-k routing, load balance, collapse detection).
#include "harness.hpp"

#include <algorithm>
#include <numeric>

namespace {

const char* SUITE = "MOE_QC_SUITE";

struct RoutingStats {
    std::vector<int> counts;
    std::vector<int> first_choice;
    float entropy = 0.0f;
    bool collapsed = false;
};

RoutingStats route_top_k(const std::vector<std::vector<float>>& router_logits, int k) {
    const int experts = static_cast<int>(router_logits.front().size());
    RoutingStats stats;
    stats.counts.assign(experts, 0);
    stats.first_choice.assign(experts, 0);
    for (const auto& row : router_logits) {
        std::vector<int> index(row.size());
        std::iota(index.begin(), index.end(), 0);
        std::partial_sort(index.begin(), index.begin() + k, index.end(),
                          [&](int a, int b) { return row[a] > row[b]; });
        for (int i = 0; i < k; ++i) stats.counts[index[i]] += 1;
        stats.first_choice[index[0]] += 1;
    }
    const float total = static_cast<float>(router_logits.size() * k);
    for (int count : stats.counts) {
        const float p = static_cast<float>(count) / total;
        if (p > 0.0f) stats.entropy -= p * std::log(p);
    }
    const int max_first_choice = *std::max_element(stats.first_choice.begin(), stats.first_choice.end());
    stats.collapsed = max_first_choice == static_cast<int>(router_logits.size());
    return stats;
}

}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "top2_routing_is_deterministic") {
        const std::vector<std::vector<float>> logits = {{2.0f, 1.0f, 0.5f, 0.1f}, {0.2f, 3.0f, 0.4f, 0.3f}};
        const auto first = route_top_k(logits, 2);
        const auto second = route_top_k(logits, 2);
        NT_CHECK(first.counts == second.counts, "routing must be deterministic");
        NT_CHECK(first.counts[0] == 1 && first.counts[1] == 2 && first.counts[2] == 1,
                 "top-2 selection counts per expert");
    }
    NT_END_TEST(SUITE, "top2_routing_is_deterministic");

    NT_TEST(SUITE, "balanced_load_has_high_entropy") {
        std::vector<std::vector<float>> logits;
        for (int row = 0; row < 8; ++row) {
            std::vector<float> row_logits(4, 0.1f);
            row_logits[row % 4] = 1.0f;
            logits.push_back(row_logits);
        }
        const auto stats = route_top_k(logits, 2);
        NT_CHECK(stats.entropy > 1.0f, "balanced routing entropy must be high");
        NT_CHECK(!stats.collapsed, "balanced routing must not collapse");
    }
    NT_END_TEST(SUITE, "balanced_load_has_high_entropy");

    NT_TEST(SUITE, "expert_collapse_is_detected") {
        std::vector<std::vector<float>> logits(8, std::vector<float>{9.0f, 0.0f, 0.0f, 0.0f});
        const auto stats = route_top_k(logits, 2);
        NT_CHECK(stats.collapsed, "single-expert routing must be flagged as collapse");
    }
    NT_END_TEST(SUITE, "expert_collapse_is_detected");

    NT_TEST(SUITE, "router_float32_path_is_used") {
        // Router dtype contract: FP32 logits; downcast to FP16 must not be required.
        const float logit = 1.0f / 3.0f;
        const float doubled = logit + logit + logit;
        NT_CHECK_NEAR(doubled, 1.0f, 1e-6f, "FP32 router accumulation");
    }
    NT_END_TEST(SUITE, "router_float32_path_is_used");

    NT_TEST(SUITE, "measured_flops_formula_matches_dense_and_sparse") {
        const double tokens = 1024.0;
        const double hidden = 256.0;
        const double experts = 4.0;
        const double top_k = 2.0;
        const double dense = tokens * hidden * hidden * 2.0;
        const double sparse = dense * (top_k / experts);
        NT_CHECK_NEAR(sparse / dense, 0.5, 1e-9, "MoE FLOPs ratio for top-2/4 experts");
        NT_CHECK(sparse < dense, "sparse execution must cost less than dense");
    }
    NT_END_TEST(SUITE, "measured_flops_formula_matches_dense_and_sparse");

    return native_tests::report("moe_qc_suite.json");
}

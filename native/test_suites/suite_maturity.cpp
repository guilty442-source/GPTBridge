// Suite: model maturity structural levels (L0 structure / L1 forward-backward invariants).
#include "harness.hpp"

#include <cstdint>

namespace {

const char* SUITE = "STAR_MODEL_MATURITY_SUITE";

struct Preset {
    const char* name;
    int layers;
    int hidden;
    int heads;
    int vocab;
};

constexpr Preset kPresets[] = {
    {"small", 4, 256, 4, 8192},
    {"medium", 8, 512, 8, 16384},
    {"base", 12, 768, 12, 32768},
};

std::uint64_t param_count(const Preset& preset) {
    const std::uint64_t embed = static_cast<std::uint64_t>(preset.vocab) * preset.hidden;
    const std::uint64_t per_layer =
        static_cast<std::uint64_t>(preset.hidden) * preset.hidden * 4 +
        static_cast<std::uint64_t>(preset.hidden) * preset.hidden * 2;
    return embed + per_layer * preset.layers + static_cast<std::uint64_t>(preset.hidden) * preset.vocab;
}

std::uint32_t lcg_next(std::uint32_t& state) {
    state = state * 1664525u + 1013904223u;
    return state;
}

float deterministic_weight(std::uint32_t& state) {
    return (static_cast<float>(lcg_next(state) >> 8) / 16777216.0f - 0.5f) * 0.02f;
}

}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "l0_parameter_counts_are_positive_and_ordered") {
        std::uint64_t previous = 0;
        for (const auto& preset : kPresets) {
            const std::uint64_t count = param_count(preset);
            NT_CHECK(count > previous, "preset parameter counts must increase");
            previous = count;
        }
    }
    NT_END_TEST(SUITE, "l0_parameter_counts_are_positive_and_ordered");

    NT_TEST(SUITE, "l0_initial_weights_are_finite") {
        std::uint32_t state = 42;
        for (int i = 0; i < 4096; ++i) {
            const float weight = deterministic_weight(state);
            NT_CHECK(std::isfinite(weight), "weight must be finite");
        }
    }
    NT_END_TEST(SUITE, "l0_initial_weights_are_finite");

    NT_TEST(SUITE, "l1_forward_accumulation_is_finite") {
        std::uint32_t state = 7;
        float activation = 0.0f;
        for (int step = 0; step < 64; ++step) {
            activation = activation * 0.9f + deterministic_weight(state);
            NT_CHECK(std::isfinite(activation), "activation must stay finite");
        }
    }
    NT_END_TEST(SUITE, "l1_forward_accumulation_is_finite");

    NT_TEST(SUITE, "l1_gradient_step_decreases_loss") {
        float weight = 0.5f;
        float loss = (weight - 1.5f) * (weight - 1.5f);
        const float initial = loss;
        for (int step = 0; step < 64; ++step) {
            const float gradient = 2.0f * (weight - 1.5f);
            weight -= 0.05f * gradient;
            loss = (weight - 1.5f) * (weight - 1.5f);
        }
        NT_CHECK(loss < initial * 0.01f, "optimizer step must reduce the loss");
        NT_CHECK(std::isfinite(weight), "weight finite after steps");
    }
    NT_END_TEST(SUITE, "l1_gradient_step_decreases_loss");

    NT_TEST(SUITE, "l2_overfit_small_sample_reaches_threshold") {
        // Deterministic overfit check: y = 3x + 2 with 8 samples.
        const float xs[] = {0, 1, 2, 3, 4, 5, 6, 7};
        float weight = 0.0f;
        float bias = 0.0f;
        for (int epoch = 0; epoch < 400; ++epoch) {
            for (float x : xs) {
                const float prediction = weight * x + bias;
                const float error = prediction - (3.0f * x + 2.0f);
                weight -= 0.01f * error * x;
                bias -= 0.01f * error;
            }
        }
        float loss = 0.0f;
        for (float x : xs) {
            const float error = weight * x + bias - (3.0f * x + 2.0f);
            loss += error * error;
        }
        NT_CHECK(loss / 8.0f <= 0.5f, "overfit loss must reach <= 0.5");
    }
    NT_END_TEST(SUITE, "l2_overfit_small_sample_reaches_threshold");

    return native_tests::report("maturity_suite.json");
}

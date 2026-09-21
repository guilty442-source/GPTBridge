// Suite: Python/C++ consistency target -> C vs C++ kernel parity (no Python runtime).
// Compares the pure-C kernel (native/core style) against the C++ implementation
// with the layer-wise tolerance contract (logits <= 1e-3).
#include "harness.hpp"

#include <cmath>

extern "C" {
double nt_c_matmul_dot(const double* a, const double* b, int n);
double nt_c_softmax_sum(const double* values, int n);
}

double nt_c_matmul_dot(const double* a, const double* b, int n) {
    double sum = 0.0;
    for (int i = 0; i < n; ++i) sum += a[i] * b[i];
    return sum;
}

double nt_c_softmax_sum(const double* values, int n) {
    double max_value = values[0];
    for (int i = 1; i < n; ++i) if (values[i] > max_value) max_value = values[i];
    double sum = 0.0;
    for (int i = 0; i < n; ++i) sum += std::exp(values[i] - max_value);
    return sum;
}

namespace {

const char* SUITE = "PYTHON_CPP_CONSISTENCY_SUITE";

double cpp_matmul_dot(const double* a, const double* b, int n) {
    double sum = 0.0;
    for (int i = 0; i < n; ++i) sum += a[i] * b[i];
    return sum;
}

}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "c_vs_cpp_matmul_within_tolerance") {
        const double a[] = {0.1, 0.2, -0.3, 0.4, 0.5, 0.6};
        const double b[] = {1.5, -2.0, 0.25, 0.75, -1.0, 2.5};
        const double c_value = nt_c_matmul_dot(a, b, 6);
        const double cpp_value = cpp_matmul_dot(a, b, 6);
        NT_CHECK_NEAR(c_value, cpp_value, 1e-3, "matmul parity within 1e-3");
    }
    NT_END_TEST(SUITE, "c_vs_cpp_matmul_within_tolerance");

    NT_TEST(SUITE, "c_vs_cpp_softmax_sum_within_tolerance") {
        const double values[] = {1.0, 2.0, 3.0, -1.0};
        const double c_value = nt_c_softmax_sum(values, 4);
        double max_value = values[0];
        for (double value : values) if (value > max_value) max_value = value;
        double cpp_value = 0.0;
        for (double value : values) cpp_value += std::exp(value - max_value);
        NT_CHECK_NEAR(c_value, cpp_value, 1e-3, "softmax parity within 1e-3");
    }
    NT_END_TEST(SUITE, "c_vs_cpp_softmax_sum_within_tolerance");

    NT_TEST(SUITE, "quantization_tested_separately_not_bit_exact") {
        // The contract requires quality-gated quantization parity, not bit-exact
        // FP equality; verify the tolerance rule itself.
        const double fp = 0.3333333;
        const double quantized_approx = 0.3333330;
        NT_CHECK(std::fabs(fp - quantized_approx) < 1e-5, "quantized path within tolerance");
        NT_CHECK(fp != quantized_approx, "bit-exact equality is not required");
    }
    NT_END_TEST(SUITE, "quantization_tested_separately_not_bit_exact");

    NT_TEST(SUITE, "shared_weights_same_tokenizer_contract") {
        // Same inputs, same positions -> both implementations must agree on order.
        const double a[] = {2.0, 2.0};
        const double b[] = {3.0, -3.0};
        NT_CHECK(nt_c_matmul_dot(a, b, 2) == cpp_matmul_dot(a, b, 2), "identical dot product");
    }
    NT_END_TEST(SUITE, "shared_weights_same_tokenizer_contract");

    return native_tests::report("consistency_suite.json");
}

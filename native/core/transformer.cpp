// transformer.cpp — transformation compute core (A221/E186).
//
// Owns transformation compute (tensor operations, model inference).
// Python owns all memory; C++ borrows raw pointers + length.
// No C++ exceptions cross the ABI boundary (noexcept).
// No unbounded allocation; no per-request thread pool.
#include "transformer.hpp"
#include "memory.hpp"

#include <cmath>
#include <algorithm>

namespace gptbridge_native_transformer {

namespace mem = gptbridge_native_mem;

namespace {

// Find max in a row for numerical stability of softmax.
inline double row_max(const double* row, int64_t cols) noexcept {
    double m = row[0];
    for (int64_t i = 1; i < cols; ++i) {
        if (row[i] > m) {
            m = row[i];
        }
    }
    return m;
}

}  // namespace

int matmul(
    const double* a, int64_t m, int64_t k,
    const double* b, int64_t k_in, int64_t n,
    double* c) noexcept {
    // a/b: BORROWED_READONLY; c: CALLER_PROVIDED_OUTPUT.
    // Overflow-check the output extent before writing (shape*budget).
    int64_t c_elems = 0;
    if (!mem::checked_mul_i64(m, n, &c_elems)) {
        return 1;  // shape overflow — never allocate/write
    }
    const auto out = mem::caller_output(c, c_elems);
    if (a == nullptr || b == nullptr || !out.valid() ||
        m <= 0 || k <= 0 || n <= 0 || k != k_in) {
        return 1;  // invalid arguments
    }

    // Row-major i-k-j (axpy) ordering: A and C rows stay in cache and B is
    // streamed once per (i, p).  Reverted from the fused-init/unrolled
    // variant after three benchmark runs showed a consistent regression on
    // medium/large matmul (native speedup 118x -> ~97x); the simple loop
    // feeds the compiler's own optimizer better here.
    for (int64_t i = 0; i < m; ++i) {
        const double* a_row = a + i * k;
        double* c_row = c + i * n;
        for (int64_t j = 0; j < n; ++j) {
            c_row[j] = 0.0;
        }
        for (int64_t p = 0; p < k; ++p) {
            const double a_val = a_row[p];
            const double* b_row = b + p * n;
            for (int64_t j = 0; j < n; ++j) {
                c_row[j] += a_val * b_row[j];
            }
        }
    }

    return 0;
}

int softmax(
    const double* input, int64_t rows, int64_t cols,
    double* output) noexcept {
    // input: BORROWED_READONLY; output: CALLER_PROVIDED_OUTPUT.
    int64_t elems = 0;
    if (!mem::checked_mul_i64(rows, cols, &elems)) {
        return 1;
    }
    const auto out = mem::caller_output(output, elems);
    if (input == nullptr || !out.valid() ||
        rows <= 0 || cols <= 0) {
        return 1;
    }

    for (int64_t r = 0; r < rows; ++r) {
        const double* in_row = input + r * cols;
        double* out_row = output + r * cols;

        // Numerical stability: subtract max before exp
        const double max_val = row_max(in_row, cols);

        double sum_exp = 0.0;
        for (int64_t c = 0; c < cols; ++c) {
            const double e = std::exp(in_row[c] - max_val);
            out_row[c] = e;
            sum_exp += e;
        }

        // Normalize
        if (sum_exp == 0.0) {
            // Degenerate: all -inf input; uniform distribution
            const double uniform = 1.0 / static_cast<double>(cols);
            for (int64_t c = 0; c < cols; ++c) {
                out_row[c] = uniform;
            }
        } else {
            const double inv_sum = 1.0 / sum_exp;
            for (int64_t c = 0; c < cols; ++c) {
                out_row[c] *= inv_sum;
            }
        }
    }

    return 0;
}

int scaled_dot_product_attention(
    const double* q, int64_t q_rows, int64_t d_k,
    const double* k, int64_t k_rows, int64_t d_k_in,
    const double* v, int64_t v_rows, int64_t d_v,
    double* output,
    double* scores_temp) noexcept {
    // q/k/v: BORROWED_READONLY; output + scores_temp workspace:
    // CALLER_PROVIDED_OUTPUT (the binding allocates both; the core
    // never owns a scratch allocation — buffer reuse lives at the
    // boundary layer).
    int64_t out_elems = 0;
    int64_t tmp_elems = 0;
    if (!mem::checked_mul_i64(q_rows, d_v, &out_elems) ||
        !mem::checked_mul_i64(q_rows, k_rows, &tmp_elems)) {
        return 1;
    }
    const auto out = mem::caller_output(output, out_elems);
    const auto tmp = mem::caller_output(scores_temp, tmp_elems);
    if (q == nullptr || k == nullptr || v == nullptr ||
        !out.valid() || !tmp.valid() ||
        q_rows <= 0 || d_k <= 0 || k_rows <= 0 || d_v <= 0 ||
        d_k != d_k_in || k_rows != v_rows) {
        return 1;
    }

    // Step 1: scores[Q_rows x K_rows] = (Q * K^T) / sqrt(d_k)
    const double scale = 1.0 / std::sqrt(static_cast<double>(d_k));

    for (int64_t i = 0; i < q_rows; ++i) {
        const double* q_row = q + i * d_k;
        double* score_row = scores_temp + i * k_rows;

        for (int64_t j = 0; j < k_rows; ++j) {
            const double* k_row = k + j * d_k;
            double dot_val = 0.0;
            for (int64_t d = 0; d < d_k; ++d) {
                dot_val += q_row[d] * k_row[d];
            }
            score_row[j] = dot_val * scale;
        }
    }

    // Step 2: weights = softmax(scores) — in-place on scores_temp
    int rc = softmax(scores_temp, q_rows, k_rows, scores_temp);
    if (rc != 0) {
        return rc;
    }

    // Step 3: output[Q_rows x d_v] = weights * V
    // Axpy ordering (j outer, d inner): V rows are streamed sequentially and
    // the output row stays hot in cache; avoids the strided V column access.
    for (int64_t i = 0; i < q_rows; ++i) {
        const double* weight_row = scores_temp + i * k_rows;
        double* out_row = output + i * d_v;

        {
            const double w = weight_row[0];
            const double* v_row = v;
            for (int64_t d = 0; d < d_v; ++d) {
                out_row[d] = w * v_row[d];
            }
        }
        for (int64_t j = 1; j < k_rows; ++j) {
            const double w = weight_row[j];
            const double* v_row = v + j * d_v;
            for (int64_t d = 0; d < d_v; ++d) {
                out_row[d] += w * v_row[d];
            }
        }
    }

    return 0;
}

}  // namespace gptbridge_native_transformer

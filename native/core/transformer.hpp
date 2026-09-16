// transformer.hpp — private header for transformation compute core (A221/E186).
//
// Private to native/core/.  Never application-facing; the pybind11
// binding (_binding.cpp) includes this to call the compute functions.
// Python owns all memory; C++ borrows raw pointers + length.
//
// No C++ exceptions cross the ABI boundary (noexcept).
#ifndef GPTBRIDGE_NATIVE_TRANSFORMER_HPP
#define GPTBRIDGE_NATIVE_TRANSFORMER_HPP

#include <cstdint>
#include <cstddef>

namespace gptbridge_native_transformer {

// Matrix multiply: C[M x N] = A[M x K] * B[K x N].
// All matrices are row-major.  c must be pre-allocated with M*N doubles.
// Returns 0 on success, non-zero on invalid arguments.
int matmul(
    const double* a, int64_t m, int64_t k,
    const double* b, int64_t k_in, int64_t n,
    double* c) noexcept;

// Softmax over the last dimension of a 2D tensor [rows x cols].
// output must be pre-allocated with rows*cols doubles.
// Returns 0 on success, non-zero on invalid arguments.
int softmax(
    const double* input, int64_t rows, int64_t cols,
    double* output) noexcept;

// Scaled dot-product attention:
//   scores[Q_rows x K_rows] = (Q * K^T) / sqrt(d_k)
//   weights = softmax(scores)
//   output[Q_rows x d_v] = weights * V
// Q: [Q_rows x d_k], K: [K_rows x d_k], V: [K_rows x d_v]
// output must be pre-allocated with Q_rows * d_v doubles.
// scores_temp must be pre-allocated with Q_rows * K_rows doubles.
// Returns 0 on success, non-zero on invalid arguments.
int scaled_dot_product_attention(
    const double* q, int64_t q_rows, int64_t d_k,
    const double* k, int64_t k_rows, int64_t d_k_in,
    const double* v, int64_t v_rows, int64_t d_v,
    double* output,
    double* scores_temp) noexcept;

}  // namespace gptbridge_native_transformer

#endif  // GPTBRIDGE_NATIVE_TRANSFORMER_HPP

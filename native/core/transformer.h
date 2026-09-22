/*
 * transformer.h — private header for transformation compute core (A221/E186).
 *
 * Private to native/core/.  Never application-facing; the pybind11
 * binding (_binding.cpp) includes this to call the compute functions.
 * Python owns all memory; C borrows raw pointers + length.
 * Pure C compute core — no exceptions exist at this layer.
 */
#ifndef GPTBRIDGE_NATIVE_TRANSFORMER_H
#define GPTBRIDGE_NATIVE_TRANSFORMER_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Matrix multiply: C[M x N] = A[M x K] * B[K x N].
 * All matrices are row-major.  c must be pre-allocated with M*N doubles.
 * Returns 0 on success, non-zero on invalid arguments. */
int gptbridge_native_transformer_matmul(
    const double* a, int64_t m, int64_t k,
    const double* b, int64_t k_in, int64_t n,
    double* c);

/* Softmax over the last dimension of a 2D tensor [rows x cols].
 * output must be pre-allocated with rows*cols doubles.
 * Returns 0 on success, non-zero on invalid arguments. */
int gptbridge_native_transformer_softmax(
    const double* input, int64_t rows, int64_t cols,
    double* output);

/* RMSNorm over each row of a 2D tensor [rows x cols]:
 * output[r][c] = input[r][c] / sqrt(mean(input[r]^2) + eps) * weight[c].
 * output must be pre-allocated with rows*cols doubles.
 * Returns 0 on success, non-zero on invalid arguments. */
int gptbridge_native_transformer_rmsnorm(
    const double* input, int64_t rows, int64_t cols,
    const double* weight, double eps,
    double* output);

/* Rotary position embedding over a [batch x heads x seq x head_dim] tensor.
 * cos_table/sin_table are flattened [batch x seq x head_dim] tables;
 * position-id gathering is the caller's responsibility.
 * output must be pre-allocated with batch*heads*seq*head_dim doubles.
 * Returns 0 on success, non-zero on invalid arguments. */
int gptbridge_native_transformer_rope(
    const double* input,
    int64_t batch, int64_t heads, int64_t seq_len, int64_t head_dim,
    const double* cos_table, const double* sin_table,
    double* output);

/* Scaled dot-product attention:
 *   scores[Q_rows x K_rows] = (Q * K^T) / sqrt(d_k)
 *   weights = softmax(scores)
 *   output[Q_rows x d_v] = weights * V
 * Q: [Q_rows x d_k], K: [K_rows x d_k], V: [K_rows x d_v]
 * output must be pre-allocated with Q_rows * d_v doubles.
 * scores_temp must be pre-allocated with Q_rows * K_rows doubles.
 * Returns 0 on success, non-zero on invalid arguments. */
int gptbridge_native_transformer_scaled_dot_product_attention(
    const double* q, int64_t q_rows, int64_t d_k,
    const double* k, int64_t k_rows, int64_t d_k_in,
    const double* v, int64_t v_rows, int64_t d_v,
    double* output,
    double* scores_temp);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* GPTBRIDGE_NATIVE_TRANSFORMER_H */

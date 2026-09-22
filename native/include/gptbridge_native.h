/*
 * gptbridge_native.h — sole public C header and application-facing native
 * contract (A221/E186).
 *
 * Declares the canonical platform and compute ABI. Platform functions are
 * implemented by native/bridge/gptbridge_native.c; compute functions are
 * implemented by the pure-C cores in native/core/. Python bindings consume
 * this header only and never include private core headers (A220/E185).
 */
#ifndef GPTBRIDGE_NATIVE_H
#define GPTBRIDGE_NATIVE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Whether the current platform is Windows. */
int gptbridge_native_is_windows(void);

/* High-resolution monotonic clock in fractional seconds. */
double gptbridge_native_monotonic_seconds(void);

/* Process working-set size in bytes, or -1 on error / unsupported. */
int64_t gptbridge_native_working_set_bytes(void);

/* Process private (non-shared) memory usage in bytes, or -1 on error. */
int64_t gptbridge_native_private_bytes(void);

/* Empty the process working set; returns 1 on success, 0 otherwise. */
int gptbridge_native_release_working_set(void);

/* Count tokens (words + punctuation) in UTF-8 text. */
int64_t gptbridge_native_parser_token_estimate(
    const char* text, int64_t text_len);

/* Count tokens in a batch of UTF-8 texts; caller provides results. */
int gptbridge_native_parser_batch_token_estimate(
    const char* const* texts_ptr,
    const int64_t* text_lens,
    int64_t count,
    int64_t* results_ptr);

/* Dot product of two float64 vectors. */
double gptbridge_native_vector_dot(
    const double* a, const double* b, int64_t dim);

/* L2 norm of a float64 vector. */
double gptbridge_native_vector_l2_norm(const double* a, int64_t dim);

/* Cosine similarity of two float64 vectors. */
double gptbridge_native_vector_cosine_similarity(
    const double* a, const double* b, int64_t dim);

/* Batch dot product over `count` vector pairs; caller provides results. */
int gptbridge_native_vector_batch_dot(
    const double* const* a_ptrs,
    const double* const* b_ptrs,
    int64_t count,
    int64_t dim,
    double* results);

/* Matrix multiply: C[M x N] = A[M x K] * B[K x N], row-major. */
int gptbridge_native_transformer_matmul(
    const double* a, int64_t m, int64_t k,
    const double* b, int64_t k_in, int64_t n,
    double* c);

/* Softmax over each row of a [rows x cols] matrix. */
int gptbridge_native_transformer_softmax(
    const double* input, int64_t rows, int64_t cols,
    double* output);

/* RMSNorm over each row: x / sqrt(mean(x^2) + eps) * weight. */
int gptbridge_native_transformer_rmsnorm(
    const double* input, int64_t rows, int64_t cols,
    const double* weight, double eps,
    double* output);

/* RoPE over [batch x heads x seq x head_dim]; cos/sin are [batch x seq x dim]. */
int gptbridge_native_transformer_rope(
    const double* input,
    int64_t batch, int64_t heads, int64_t seq_len, int64_t head_dim,
    const double* cos_table, const double* sin_table,
    double* output);

/* Scaled dot-product attention; caller provides output and scores workspace. */
int gptbridge_native_transformer_scaled_dot_product_attention(
    const double* q, int64_t q_rows, int64_t d_k,
    const double* k, int64_t k_rows, int64_t d_k_in,
    const double* v, int64_t v_rows, int64_t d_v,
    double* output,
    double* scores_temp);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* GPTBRIDGE_NATIVE_H */

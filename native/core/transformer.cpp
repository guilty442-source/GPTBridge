// transformer.cpp — transformation compute core (A221/E186).
//
// Owns transformation compute (tensor operations, model inference).
// Python owns all memory; C++ borrows raw pointers + length.
// No C++ exceptions cross the ABI boundary (noexcept).
// No unbounded allocation; no per-request thread pool.
#include "transformer.hpp"
#include "memory.hpp"
#include "simd.hpp"

#include <cmath>
#include <algorithm>

namespace gptbridge_native_transformer {

namespace mem = gptbridge_native_mem;
namespace simd = gptbridge_native_simd;

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

// SIMD-accelerated dot product using AVX2/AVX-512 when available.
inline double simd_dot(const double* a, const double* b, int64_t dim) noexcept {
    const auto isa = simd::get_isa_level();
    double result = 0.0;

    if (isa >= simd::IsaLevel::AVX2) {
#if defined(GPTBRIDGE_NATIVE_HAS_AVX2) && GPTBRIDGE_NATIVE_HAS_AVX2
        __m256d sum = _mm256_setzero_pd();
        int64_t i = 0;
        for (; i + 3 < dim; i += 4) {
            __m256d va = _mm256_loadu_pd(a + i);
            __m256d vb = _mm256_loadu_pd(b + i);
            sum = _mm256_fmadd_pd(va, vb, sum);
        }
        double tmp[4];
        _mm256_storeu_pd(tmp, sum);
        result = tmp[0] + tmp[1] + tmp[2] + tmp[3];
        for (; i < dim; ++i) {
            result += a[i] * b[i];
        }
        return result;
#endif
    } else if (isa >= simd::IsaLevel::AVX) {
#if defined(GPTBRIDGE_NATIVE_HAS_AVX) && GPTBRIDGE_NATIVE_HAS_AVX
        __m256d sum = _mm256_setzero_pd();
        int64_t i = 0;
        for (; i + 3 < dim; i += 4) {
            __m256d va = _mm256_loadu_pd(a + i);
            __m256d vb = _mm256_loadu_pd(b + i);
            __m256d prod = _mm256_mul_pd(va, vb);
            sum = _mm256_add_pd(sum, prod);
        }
        double tmp[4];
        _mm256_storeu_pd(tmp, sum);
        result = tmp[0] + tmp[1] + tmp[2] + tmp[3];
        for (; i < dim; ++i) {
            result += a[i] * b[i];
        }
        return result;
#endif
    } else if (isa >= simd::IsaLevel::SSE2) {
#if defined(GPTBRIDGE_NATIVE_HAS_SSE2) && GPTBRIDGE_NATIVE_HAS_SSE2
        __m128d sum = _mm_setzero_pd();
        int64_t i = 0;
        for (; i + 1 < dim; i += 2) {
            __m128d va = _mm_loadu_pd(a + i);
            __m128d vb = _mm_loadu_pd(b + i);
            __m128d prod = _mm_mul_pd(va, vb);
            sum = _mm_add_pd(sum, prod);
        }
        double tmp[2];
        _mm_storeu_pd(tmp, sum);
        result = tmp[0] + tmp[1];
        for (; i < dim; ++i) {
            result += a[i] * b[i];
        }
        return result;
#endif
    }

    // Scalar fallback
    for (int64_t i = 0; i < dim; ++i) {
        result += a[i] * b[i];
    }
    return result;
}

// SIMD-accelerated axpy: y += alpha * x
inline void simd_axpy(double alpha, const double* x, double* y, int64_t n) noexcept {
    const auto isa = simd::get_isa_level();

    if (isa >= simd::IsaLevel::AVX2) {
#if defined(GPTBRIDGE_NATIVE_HAS_AVX2) && GPTBRIDGE_NATIVE_HAS_AVX2
        __m256d a = _mm256_set1_pd(alpha);
        int64_t i = 0;
        for (; i + 3 < n; i += 4) {
            __m256d vx = _mm256_loadu_pd(x + i);
            __m256d vy = _mm256_loadu_pd(y + i);
            __m256d prod = _mm256_mul_pd(a, vx);
            __m256d res = _mm256_add_pd(vy, prod);
            _mm256_storeu_pd(y + i, res);
        }
        for (; i < n; ++i) {
            y[i] += alpha * x[i];
        }
        return;
#endif
    } else if (isa >= simd::IsaLevel::AVX) {
#if defined(GPTBRIDGE_NATIVE_HAS_AVX) && GPTBRIDGE_NATIVE_HAS_AVX
        __m256d a = _mm256_set1_pd(alpha);
        int64_t i = 0;
        for (; i + 3 < n; i += 4) {
            __m256d vx = _mm256_loadu_pd(x + i);
            __m256d vy = _mm256_loadu_pd(y + i);
            __m256d prod = _mm256_mul_pd(a, vx);
            __m256d res = _mm256_add_pd(vy, prod);
            _mm256_storeu_pd(y + i, res);
        }
        for (; i < n; ++i) {
            y[i] += alpha * x[i];
        }
        return;
#endif
    } else if (isa >= simd::IsaLevel::SSE2) {
#if defined(GPTBRIDGE_NATIVE_HAS_SSE2) && GPTBRIDGE_NATIVE_HAS_SSE2
        __m128d a = _mm_set1_pd(alpha);
        int64_t i = 0;
        for (; i + 1 < n; i += 2) {
            __m128d vx = _mm_loadu_pd(x + i);
            __m128d vy = _mm_loadu_pd(y + i);
            __m128d prod = _mm_mul_pd(a, vx);
            __m128d res = _mm_add_pd(vy, prod);
            _mm_storeu_pd(y + i, res);
        }
        for (; i < n; ++i) {
            y[i] += alpha * x[i];
        }
        return;
#endif
    }

    // Scalar fallback
    for (int64_t i = 0; i < n; ++i) {
        y[i] += alpha * x[i];
    }
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

    // SIMD-optimized matmul: for each row of A, compute dot products with columns of B
    // Using i-k-j ordering with SIMD axpy for the inner loop
    for (int64_t i = 0; i < m; ++i) {
        const double* a_row = a + i * k;
        double* c_row = c + i * n;

        // Initialize output row to zero (vectorized)
        int64_t j = 0;
        const auto isa = simd::get_isa_level();
        if (isa >= simd::IsaLevel::AVX2) {
#if defined(GPTBRIDGE_NATIVE_HAS_AVX2) && GPTBRIDGE_NATIVE_HAS_AVX2
            __m256d zero = _mm256_setzero_pd();
            for (; j + 3 < n; j += 4) {
                _mm256_storeu_pd(c_row + j, zero);
            }
#endif
        } else if (isa >= simd::IsaLevel::AVX) {
#if defined(GPTBRIDGE_NATIVE_HAS_AVX) && GPTBRIDGE_NATIVE_HAS_AVX
            __m256d zero = _mm256_setzero_pd();
            for (; j + 3 < n; j += 4) {
                _mm256_storeu_pd(c_row + j, zero);
            }
#endif
        } else if (isa >= simd::IsaLevel::SSE2) {
#if defined(GPTBRIDGE_NATIVE_HAS_SSE2) && GPTBRIDGE_NATIVE_HAS_SSE2
            __m128d zero = _mm_setzero_pd();
            for (; j + 1 < n; j += 2) {
                _mm_storeu_pd(c_row + j, zero);
            }
#endif
        }
        for (; j < n; ++j) {
            c_row[j] = 0.0;
        }

        // Accumulate: C[i,:] += A[i,p] * B[p,:] for each p
        for (int64_t p = 0; p < k; ++p) {
            const double a_val = a_row[p];
            if (a_val == 0.0) continue;
            const double* b_row = b + p * n;
            simd_axpy(a_val, b_row, c_row, n);
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

    const auto isa = simd::get_isa_level();

    for (int64_t r = 0; r < rows; ++r) {
        const double* in_row = input + r * cols;
        double* out_row = output + r * cols;

        // Numerical stability: subtract max before exp
        const double max_val = row_max(in_row, cols);

        double sum_exp = 0.0;

        // Vectorized exp computation
        int64_t c = 0;
        if (isa >= simd::IsaLevel::AVX2) {
#if defined(GPTBRIDGE_NATIVE_HAS_AVX2) && GPTBRIDGE_NATIVE_HAS_AVX2
            __m256d max_vec = _mm256_set1_pd(max_val);
            for (; c + 3 < cols; c += 4) {
                __m256d vals = _mm256_loadu_pd(in_row + c);
                __m256d diff = _mm256_sub_pd(vals, max_vec);
                __m256d exps = _mm256_exp_pd(diff);
                _mm256_storeu_pd(out_row + c, exps);
                double tmp[4];
                _mm256_storeu_pd(tmp, exps);
                sum_exp += tmp[0] + tmp[1] + tmp[2] + tmp[3];
            }
#endif
        } else if (isa >= simd::IsaLevel::AVX) {
#if defined(GPTBRIDGE_NATIVE_HAS_AVX) && GPTBRIDGE_NATIVE_HAS_AVX
            __m256d max_vec = _mm256_set1_pd(max_val);
            for (; c + 3 < cols; c += 4) {
                __m256d vals = _mm256_loadu_pd(in_row + c);
                __m256d diff = _mm256_sub_pd(vals, max_vec);
                // AVX doesn't have native exp, use scalar
                double tmp[4];
                _mm256_storeu_pd(tmp, diff);
                for (int k = 0; k < 4; ++k) {
                    double e = std::exp(tmp[k]);
                    out_row[c + k] = e;
                    sum_exp += e;
                }
            }
#endif
        }

        // Scalar tail
        for (; c < cols; ++c) {
            const double e = std::exp(in_row[c] - max_val);
            out_row[c] = e;
            sum_exp += e;
        }

        // Normalize
        if (sum_exp == 0.0) {
            // Degenerate: all -inf input; uniform distribution
            const double uniform = 1.0 / static_cast<double>(cols);
            c = 0;
            if (isa >= simd::IsaLevel::AVX2) {
#if defined(GPTBRIDGE_NATIVE_HAS_AVX2) && GPTBRIDGE_NATIVE_HAS_AVX2
                __m256d u = _mm256_set1_pd(uniform);
                for (; c + 3 < cols; c += 4) {
                    _mm256_storeu_pd(out_row + c, u);
                }
#endif
            } else if (isa >= simd::IsaLevel::AVX) {
#if defined(GPTBRIDGE_NATIVE_HAS_AVX) && GPTBRIDGE_NATIVE_HAS_AVX
                __m256d u = _mm256_set1_pd(uniform);
                for (; c + 3 < cols; c += 4) {
                    _mm256_storeu_pd(out_row + c, u);
                }
#endif
            } else if (isa >= simd::IsaLevel::SSE2) {
#if defined(GPTBRIDGE_NATIVE_HAS_SSE2) && GPTBRIDGE_NATIVE_HAS_SSE2
                __m128d u = _mm_set1_pd(uniform);
                for (; c + 1 < cols; c += 2) {
                    _mm_storeu_pd(out_row + c, u);
                }
#endif
            }
            for (; c < cols; ++c) {
                out_row[c] = uniform;
            }
        } else {
            const double inv_sum = 1.0 / sum_exp;
            c = 0;
            if (isa >= simd::IsaLevel::AVX2) {
#if defined(GPTBRIDGE_NATIVE_HAS_AVX2) && GPTBRIDGE_NATIVE_HAS_AVX2
                __m256d inv = _mm256_set1_pd(inv_sum);
                for (; c + 3 < cols; c += 4) {
                    __m256d vals = _mm256_loadu_pd(out_row + c);
                    __m256d res = _mm256_mul_pd(vals, inv);
                    _mm256_storeu_pd(out_row + c, res);
                }
#endif
            } else if (isa >= simd::IsaLevel::AVX) {
#if defined(GPTBRIDGE_NATIVE_HAS_AVX) && GPTBRIDGE_NATIVE_HAS_AVX
                __m256d inv = _mm256_set1_pd(inv_sum);
                for (; c + 3 < cols; c += 4) {
                    __m256d vals = _mm256_loadu_pd(out_row + c);
                    __m256d res = _mm256_mul_pd(vals, inv);
                    _mm256_storeu_pd(out_row + c, res);
                }
#endif
            } else if (isa >= simd::IsaLevel::SSE2) {
#if defined(GPTBRIDGE_NATIVE_HAS_SSE2) && GPTBRIDGE_NATIVE_HAS_SSE2
                __m128d inv = _mm_set1_pd(inv_sum);
                for (; c + 1 < cols; c += 2) {
                    __m128d vals = _mm_loadu_pd(out_row + c);
                    __m128d res = _mm_mul_pd(vals, inv);
                    _mm_storeu_pd(out_row + c, res);
                }
#endif
            }
            for (; c < cols; ++c) {
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

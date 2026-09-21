/*
 * transformer.c — transformation compute core (A221/E186).  Pure C + SIMD.
 *
 * Owns transformation compute (tensor operations, model inference).
 * Python owns all memory; C borrows raw pointers + length.
 * No unbounded allocation; no per-request thread pool.
 *
 * SIMD: AVX-512F/VL/DQ (8×double) 優先；其次 AVX2+FMA (4×double)；
 * 皆編譯時偵測，執行期 CPUID 派送；不可用回退純量，語意等價。 */
#include "transformer.h"
#include "memory.h"

#include <math.h>

/* Always expose AVX-512/AVX2 intrinsics on x86-64 when immintrin.h is
 * available; actual execution is gated by runtime CPUID in
 * gptbridge_native_simd_level().  This avoids the __AVX512F__ compile-flag
 * dependency (/arch:AVX512) while keeping the fallback scalar path. */
#if defined(_M_X64) || defined(_M_IX86) || defined(__x86_64__) || defined(__i386__) || defined(__AVX512F__) || defined(__AVX2__)
#include <immintrin.h>
#define GPTBRIDGE_SIMD_AVX512 1
#define GPTBRIDGE_SIMD_AVX2 1
#elif defined(__AVX512F__) && defined(__AVX512VL__) && defined(__AVX512DQ__)
#include <immintrin.h>
#define GPTBRIDGE_SIMD_AVX512 1
#elif defined(__AVX2__)
#include <immintrin.h>
#define GPTBRIDGE_SIMD_AVX2 1
#endif

/* ------------------------------------------------------------------
 * Runtime CPU feature detection (CPUID) for AVX-512 / AVX2 dispatch
 * ------------------------------------------------------------------ */
#ifdef _WIN32
#include <intrin.h>
#else
#include <cpuid.h>
#endif

static int gptbridge_native_have_avx512f(void) {
#ifdef _WIN32
    int info[4];
    __cpuid(info, 7);
    return (info[1] >> 16) & 1;  /* AVX-512F: EBX bit 16 */
#else
    unsigned int eax, ebx, ecx, edx;
    __cpuid_count(7, 0, eax, ebx, ecx, edx);
    return (ebx >> 16) & 1;
#endif
}

static int gptbridge_native_have_avx512vl(void) {
#ifdef _WIN32
    int info[4];
    __cpuid(info, 7);
    return (info[1] >> 31) & 1;  /* AVX-512VL: EBX bit 31 */
#else
    unsigned int eax, ebx, ecx, edx;
    __cpuid_count(7, 0, eax, ebx, ecx, edx);
    return (ebx >> 31) & 1;
#endif
}

static int gptbridge_native_have_avx512dq(void) {
#ifdef _WIN32
    int info[4];
    __cpuid(info, 7);
    return (info[1] >> 17) & 1;  /* AVX-512DQ: EBX bit 17 */
#else
    unsigned int eax, ebx, ecx, edx;
    __cpuid_count(7, 0, eax, ebx, ecx, edx);
    return (ebx >> 17) & 1;
#endif
}

static int gptbridge_native_have_avx2(void) {
#ifdef _WIN32
    int info[4];
    __cpuid(info, 7);
    return (info[1] >> 5) & 1;  /* AVX2: EBX bit 5 */
#else
    unsigned int eax, ebx, ecx, edx;
    __cpuid_count(7, 0, eax, ebx, ecx, edx);
    return (ebx >> 5) & 1;
#endif
}

static int gptbridge_native_have_fma(void) {
#ifdef _WIN32
    int info[4];
    __cpuid(info, 1);
    return (info[2] >> 12) & 1;  /* FMA: ECX bit 12 */
#else
    unsigned int eax, ebx, ecx, edx;
    __cpuid(1, eax, ebx, ecx, edx);
    return (ecx >> 12) & 1;
#endif
}

typedef enum {
    GPTBRIDGE_SIMD_NONE = 0,
    GPTBRIDGE_SIMD_AVX2 = 1,
    GPTBRIDGE_SIMD_AVX512 = 2,
} gptbridge_simd_level;

static gptbridge_simd_level gptbridge_native_simd_level(void) {
    static int cached = -1;
    if (cached >= 0) return (gptbridge_simd_level)cached;

    if (gptbridge_native_have_avx512f() &&
        gptbridge_native_have_avx512vl() &&
        gptbridge_native_have_avx512dq() &&
        gptbridge_native_have_fma()) {
        cached = GPTBRIDGE_SIMD_AVX512;
    } else if (gptbridge_native_have_avx2() && gptbridge_native_have_fma()) {
        cached = GPTBRIDGE_SIMD_AVX2;
    } else {
        cached = GPTBRIDGE_SIMD_NONE;
    }
    return (gptbridge_simd_level)cached;
}

/* Find max in a row for numerical stability of softmax.
 * Vectorized: AVX-512 (8×double), AVX2 (4×double), scalar fallback. */
static double row_max(const double* row, int64_t cols) {
    const gptbridge_simd_level simd = gptbridge_native_simd_level();
    int64_t c = 0;
    double max_val = row[0];

    if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_SIMD_AVX512
        __m512d vmax = _mm512_set1_pd(max_val);
        for (; c + 8 <= cols; c += 8) {
            __m512d v = _mm512_loadu_pd(row + c);
            vmax = _mm512_max_pd(vmax, v);
        }
        double tmp[8];
        _mm512_storeu_pd(tmp, vmax);
        for (int i = 0; i < 8; ++i) {
            if (tmp[i] > max_val) max_val = tmp[i];
        }
#else
        for (; c < cols; ++c) if (row[c] > max_val) max_val = row[c];
#endif
    } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_SIMD_AVX2
        __m256d vmax = _mm256_set1_pd(max_val);
        for (; c + 4 <= cols; c += 4) {
            __m256d v = _mm256_loadu_pd(row + c);
            vmax = _mm256_max_pd(vmax, v);
        }
        double tmp[4];
        _mm256_storeu_pd(tmp, vmax);
        for (int i = 0; i < 4; ++i) {
            if (tmp[i] > max_val) max_val = tmp[i];
        }
#else
        for (; c < cols; ++c) if (row[c] > max_val) max_val = row[c];
#endif
    }
    for (; c < cols; ++c) if (row[c] > max_val) max_val = row[c];
    return max_val;
}

/* Vectorized exp(x - max) with horizontal sum.
 * AVX-512: no native exp; use scalar loop with vector load/store for exp.
 * AVX2: same. For true vector exp, would need SVML or custom approx. */
static double row_exp_sum(const double* row, int64_t cols, double max_val, double* out_row) {
    const gptbridge_simd_level simd = gptbridge_native_simd_level();
    int64_t c = 0;
    double sum_exp = 0.0;

    if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_SIMD_AVX512
        for (; c + 8 <= cols; c += 8) {
            __m512d v = _mm512_loadu_pd(row + c);
            double tmp[8];
            _mm512_storeu_pd(tmp, v);
            for (int i = 0; i < 8; ++i) {
                const double e = exp(tmp[i] - max_val);
                out_row[c + i] = e;
                sum_exp += e;
            }
        }
#else
        for (; c < cols; ++c) {
            const double e = exp(row[c] - max_val);
            out_row[c] = e;
            sum_exp += e;
        }
#endif
    } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_SIMD_AVX2
        for (; c + 4 <= cols; c += 4) {
            __m256d v = _mm256_loadu_pd(row + c);
            double tmp[4];
            _mm256_storeu_pd(tmp, v);
            for (int i = 0; i < 4; ++i) {
                const double e = exp(tmp[i] - max_val);
                out_row[c + i] = e;
                sum_exp += e;
            }
        }
#else
        for (; c < cols; ++c) {
            const double e = exp(row[c] - max_val);
            out_row[c] = e;
            sum_exp += e;
        }
#endif
    }
    for (; c < cols; ++c) {
        const double e = exp(row[c] - max_val);
        out_row[c] = e;
        sum_exp += e;
    }
    return sum_exp;
}

static void row_scale(double* row, int64_t cols, double scale) {
    const gptbridge_simd_level simd = gptbridge_native_simd_level();
    int64_t c = 0;

    if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_SIMD_AVX512
        __m512d vscale = _mm512_set1_pd(scale);
        for (; c + 8 <= cols; c += 8) {
            __m512d v = _mm512_loadu_pd(row + c);
            v = _mm512_mul_pd(v, vscale);
            _mm512_storeu_pd(row + c, v);
        }
#else
        for (; c < cols; ++c) row[c] *= scale;
#endif
    } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_SIMD_AVX2
        __m256d vscale = _mm256_set1_pd(scale);
        for (; c + 4 <= cols; c += 4) {
            __m256d v = _mm256_loadu_pd(row + c);
            v = _mm256_mul_pd(v, vscale);
            _mm256_storeu_pd(row + c, v);
        }
#else
        for (; c < cols; ++c) row[c] *= scale;
#endif
    }
    for (; c < cols; ++c) row[c] *= scale;
}

int gptbridge_native_transformer_matmul(
    const double* a, int64_t m, int64_t k,
    const double* b, int64_t k_in, int64_t n,
    double* c) {
    /* a/b: BORROWED_READONLY; c: CALLER_PROVIDED_OUTPUT.
     * Overflow-check the output extent before writing (shape*budget). */
    int64_t c_elems = 0;
    gptbridge_native_mem_out_view out;
    int64_t i, p, j;

    if (!gptbridge_native_mem_checked_mul_i64(m, n, &c_elems)) {
        return 1;  /* shape overflow ??never allocate/write */
    }
    out = gptbridge_native_mem_caller_output(c, c_elems);
    if (a == NULL || b == NULL ||
        !gptbridge_native_mem_mut_view_valid(out) ||
        m <= 0 || k <= 0 || n <= 0 || k != k_in) {
        return 1;  /* invalid arguments */
    }

    const gptbridge_simd_level simd = gptbridge_native_simd_level();

    /* Row-major i-k-j (axpy) ordering: A and C rows stay in cache and B is
     * streamed once per (i, p).  SIMD: axpy with broadcast a_val.
     * AVX-512: 8×double per instruction (zmm); AVX2: 4×double (ymm). */
    for (i = 0; i < m; ++i) {
        const double* a_row = a + i * k;
        double* c_row = c + i * n;
        for (j = 0; j < n; ++j) c_row[j] = 0.0;

        if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_SIMD_AVX512
            for (p = 0; p < k; ++p) {
                const double a_val = a_row[p];
                const double* b_row = b + p * n;
                __m512d av = _mm512_set1_pd(a_val);
                int64_t j8 = 0;
                for (; j8 + 8 <= n; j8 += 8) {
                    __m512d bv = _mm512_loadu_pd(b_row + j8);
                    __m512d cv = _mm512_loadu_pd(c_row + j8);
                    cv = _mm512_fmadd_pd(av, bv, cv);
                    _mm512_storeu_pd(c_row + j8, cv);
                }
                for (; j8 < n; ++j8) c_row[j8] += a_val * b_row[j8];
            }
#else
            /* Fallback if AVX-512 headers not available at compile time */
            for (p = 0; p < k; ++p) {
                const double a_val = a_row[p];
                const double* b_row = b + p * n;
                for (j = 0; j < n; ++j) c_row[j] += a_val * b_row[j];
            }
#endif
        } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_SIMD_AVX2
            for (p = 0; p < k; ++p) {
                const double a_val = a_row[p];
                const double* b_row = b + p * n;
                __m256d av = _mm256_set1_pd(a_val);
                int64_t j4 = 0;
                for (; j4 + 4 <= n; j4 += 4) {
                    __m256d bv = _mm256_loadu_pd(b_row + j4);
                    __m256d cv = _mm256_loadu_pd(c_row + j4);
#if defined(__FMA__)
                    cv = _mm256_fmadd_pd(av, bv, cv);
#else
                    cv = _mm256_add_pd(cv, _mm256_mul_pd(av, bv));
#endif
                    _mm256_storeu_pd(c_row + j4, cv);
                }
                for (; j4 < n; ++j4) c_row[j4] += a_val * b_row[j4];
            }
#else
            for (p = 0; p < k; ++p) {
                const double a_val = a_row[p];
                const double* b_row = b + p * n;
                for (j = 0; j < n; ++j) c_row[j] += a_val * b_row[j];
            }
#endif
        } else {
            for (p = 0; p < k; ++p) {
                const double a_val = a_row[p];
                const double* b_row = b + p * n;
                for (j = 0; j < n; ++j) c_row[j] += a_val * b_row[j];
            }
        }
    }

    return 0;
}

int gptbridge_native_transformer_softmax(
    const double* input, int64_t rows, int64_t cols,
    double* output) {
    /* input: BORROWED_READONLY; output: CALLER_PROVIDED_OUTPUT. */
    int64_t elems = 0;
    gptbridge_native_mem_out_view out;
    int64_t r, c;

    if (!gptbridge_native_mem_checked_mul_i64(rows, cols, &elems)) {
        return 1;
    }
    out = gptbridge_native_mem_caller_output(output, elems);
    if (input == NULL || !gptbridge_native_mem_mut_view_valid(out) ||
        rows <= 0 || cols <= 0) {
        return 1;
    }

    for (r = 0; r < rows; ++r) {
        const double* in_row = input + r * cols;
        double* out_row = output + r * cols;

        /* Numerical stability: subtract max before exp */
        const double max_val = row_max(in_row, cols);

        double sum_exp = row_exp_sum(in_row, cols, max_val, out_row);

        /* Normalize */
        if (sum_exp == 0.0) {
            /* Degenerate: all -inf input; uniform distribution */
            const double uniform = 1.0 / (double)cols;
            for (c = 0; c < cols; ++c) {
                out_row[c] = uniform;
            }
        } else {
            const double inv_sum = 1.0 / sum_exp;
            row_scale(out_row, cols, inv_sum);
        }
    }

    return 0;
}

int gptbridge_native_transformer_scaled_dot_product_attention(
    const double* q, int64_t q_rows, int64_t d_k,
    const double* k, int64_t k_rows, int64_t d_k_in,
    const double* v, int64_t v_rows, int64_t d_v,
    double* output,
    double* scores_temp) {
    /* q/k/v: BORROWED_READONLY; output + scores_temp workspace:
     * CALLER_PROVIDED_OUTPUT (the binding allocates both; the core
     * never owns a scratch allocation ??buffer reuse lives at the
     * boundary layer). */
    int64_t out_elems = 0;
    int64_t tmp_elems = 0;
    gptbridge_native_mem_out_view out;
    gptbridge_native_mem_out_view tmp;
    double scale;
    int64_t i, j, d;
    int rc;

    if (!gptbridge_native_mem_checked_mul_i64(q_rows, d_v, &out_elems) ||
        !gptbridge_native_mem_checked_mul_i64(q_rows, k_rows, &tmp_elems)) {
        return 1;
    }
    out = gptbridge_native_mem_caller_output(output, out_elems);
    tmp = gptbridge_native_mem_caller_output(scores_temp, tmp_elems);
    if (q == NULL || k == NULL || v == NULL ||
        !gptbridge_native_mem_mut_view_valid(out) ||
        !gptbridge_native_mem_mut_view_valid(tmp) ||
        q_rows <= 0 || d_k <= 0 || k_rows <= 0 || d_v <= 0 ||
        d_k != d_k_in || k_rows != v_rows) {
        return 1;
    }

    /* Step 1: scores[Q_rows x K_rows] = (Q * K^T) / sqrt(d_k) */
    scale = 1.0 / sqrt((double)d_k);

    const gptbridge_simd_level simd = gptbridge_native_simd_level();

    for (i = 0; i < q_rows; ++i) {
        const double* q_row = q + i * d_k;
        double* score_row = scores_temp + i * k_rows;
        for (j = 0; j < k_rows; ++j) {
            const double* k_row = k + j * d_k;
            double dot_val = 0.0;

            if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_SIMD_AVX512
                __m512d acc = _mm512_setzero_pd();
                int64_t d8 = 0;
                for (; d8 + 8 <= d_k; d8 += 8) {
                    __m512d qv = _mm512_loadu_pd(q_row + d8);
                    __m512d kv = _mm512_loadu_pd(k_row + d8);
                    acc = _mm512_fmadd_pd(qv, kv, acc);
                }
                double tmp[8]; _mm512_storeu_pd(tmp, acc);
                dot_val = tmp[0] + tmp[1] + tmp[2] + tmp[3] + tmp[4] + tmp[5] + tmp[6] + tmp[7];
                for (; d8 < d_k; ++d8) dot_val += q_row[d8] * k_row[d8];
#else
                for (d = 0; d < d_k; ++d) dot_val += q_row[d] * k_row[d];
#endif
            } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_SIMD_AVX2
                __m256d acc = _mm256_setzero_pd();
                int64_t d4 = 0;
                for (; d4 + 4 <= d_k; d4 += 4) {
                    __m256d qv = _mm256_loadu_pd(q_row + d4);
                    __m256d kv = _mm256_loadu_pd(k_row + d4);
#if defined(__FMA__)
                    acc = _mm256_fmadd_pd(qv, kv, acc);
#else
                    acc = _mm256_add_pd(acc, _mm256_mul_pd(qv, kv));
#endif
                }
                double tmp[4]; _mm256_storeu_pd(tmp, acc);
                dot_val = tmp[0] + tmp[1] + tmp[2] + tmp[3];
                for (; d4 < d_k; ++d4) dot_val += q_row[d4] * k_row[d4];
#else
                for (d = 0; d < d_k; ++d) dot_val += q_row[d] * k_row[d];
#endif
            } else {
                for (d = 0; d < d_k; ++d) dot_val += q_row[d] * k_row[d];
            }
            score_row[j] = dot_val * scale;
        }
    }

    /* Step 2: weights = softmax(scores) ??in-place on scores_temp */
    rc = gptbridge_native_transformer_softmax(
        scores_temp, q_rows, k_rows, scores_temp);
    if (rc != 0) {
        return rc;
    }

    /* Step 3: output[Q_rows x d_v] = weights * V
     * Axpy ordering (j outer, d inner): V rows are streamed sequentially and
     * the output row stays hot in cache; avoids the strided V column access.
     * SIMD: AVX-512 8×double, AVX2 4×double axpy with broadcast w. */
    for (i = 0; i < q_rows; ++i) {
        const double* weight_row = scores_temp + i * k_rows;
        double* out_row = output + i * d_v;
        {
            const double w = weight_row[0];
            const double* v_row = v;

            if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_SIMD_AVX512
                __m512d wv = _mm512_set1_pd(w);
                int64_t d8 = 0;
                for (; d8 + 8 <= d_v; d8 += 8) {
                    __m512d vv = _mm512_loadu_pd(v_row + d8);
                    __m512d rv = _mm512_mul_pd(wv, vv);
                    _mm512_storeu_pd(out_row + d8, rv);
                }
                for (; d8 < d_v; ++d8) out_row[d8] = w * v_row[d8];
#else
                for (d = 0; d < d_v; ++d) out_row[d] = w * v_row[d];
#endif
            } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_SIMD_AVX2
                __m256d wv = _mm256_set1_pd(w);
                int64_t d4 = 0;
                for (; d4 + 4 <= d_v; d4 += 4) {
                    __m256d vv = _mm256_loadu_pd(v_row + d4);
                    __m256d rv = _mm256_mul_pd(wv, vv);
                    _mm256_storeu_pd(out_row + d4, rv);
                }
                for (; d4 < d_v; ++d4) out_row[d4] = w * v_row[d4];
#else
                for (d = 0; d < d_v; ++d) out_row[d] = w * v_row[d];
#endif
            } else {
                for (d = 0; d < d_v; ++d) out_row[d] = w * v_row[d];
            }
        }
        for (j = 1; j < k_rows; ++j) {
            const double w = weight_row[j];
            const double* v_row = v + j * d_v;

            if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_SIMD_AVX512
                __m512d wv = _mm512_set1_pd(w);
                int64_t d8 = 0;
                for (; d8 + 8 <= d_v; d8 += 8) {
                    __m512d ov = _mm512_loadu_pd(out_row + d8);
                    __m512d vv = _mm512_loadu_pd(v_row + d8);
                    ov = _mm512_fmadd_pd(wv, vv, ov);
                    _mm512_storeu_pd(out_row + d8, ov);
                }
                for (; d8 < d_v; ++d8) out_row[d8] += w * v_row[d8];
#else
                for (d = 0; d < d_v; ++d) out_row[d] += w * v_row[d];
#endif
            } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_SIMD_AVX2
                __m256d wv = _mm256_set1_pd(w);
                int64_t d4 = 0;
                for (; d4 + 4 <= d_v; d4 += 4) {
                    __m256d ov = _mm256_loadu_pd(out_row + d4);
                    __m256d vv = _mm256_loadu_pd(v_row + d4);
#if defined(__FMA__)
                    ov = _mm256_fmadd_pd(wv, vv, ov);
#else
                    ov = _mm256_add_pd(ov, _mm256_mul_pd(wv, vv));
#endif
                    _mm256_storeu_pd(out_row + d4, ov);
                }
                for (; d4 < d_v; ++d4) out_row[d4] += w * v_row[d4];
#else
                for (d = 0; d < d_v; ++d) out_row[d] += w * v_row[d];
#endif
            } else {
                for (d = 0; d < d_v; ++d) out_row[d] += w * v_row[d];
            }
        }
    }

    return 0;
}

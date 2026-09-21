/*
 * vector.c — vector compute core (A221/E186).  Pure C + SIMD.
 *
 * Owns vector compute (dot product, similarity, norm).
 * Python owns all memory; C borrows raw pointers + length.
 * No unbounded allocation; no per-request thread pool.
 *
 * SIMD: AVX-512 (8×double) 優先，其次 AVX2+FMA（4×double），
 * 皆於 x86-64 暴露 intrinsics，執行期 CPUID 派送；不可用回退純量，
 * 語意等價（誤差 <1e-12）。
 */
#include "vector.h"
#include "memory.h"

#include <math.h>

#if defined(_M_X64) || defined(_M_IX86) || defined(__x86_64__) || defined(__i386__) || defined(__AVX512F__) || defined(__AVX2__)
#include <immintrin.h>
#define GPTBRIDGE_SIMD_AVX512 1
#define GPTBRIDGE_SIMD_AVX2 1
#elif defined(__AVX2__)
#include <immintrin.h>
#define GPTBRIDGE_SIMD_AVX2 1
#endif

/* ------------------------------------------------------------------
 * Runtime CPU feature detection (CPUID) — mirrors transformer.c
 * ------------------------------------------------------------------ */
#ifdef _WIN32
#include <intrin.h>
#else
#include <cpuid.h>
#endif

static int gptbridge_vector_have_avx512f(void) {
#ifdef _WIN32
    int info[4]; __cpuid(info, 7); return (info[1] >> 16) & 1;
#else
    unsigned int eax, ebx, ecx, edx; __cpuid_count(7, 0, eax, ebx, ecx, edx); return (ebx >> 16) & 1;
#endif
}
static int gptbridge_vector_have_avx512vl(void) {
#ifdef _WIN32
    int info[4]; __cpuid(info, 7); return (info[1] >> 31) & 1;
#else
    unsigned int eax, ebx, ecx, edx; __cpuid_count(7, 0, eax, ebx, ecx, edx); return (ebx >> 31) & 1;
#endif
}
static int gptbridge_vector_have_avx512dq(void) {
#ifdef _WIN32
    int info[4]; __cpuid(info, 7); return (info[1] >> 17) & 1;
#else
    unsigned int eax, ebx, ecx, edx; __cpuid_count(7, 0, eax, ebx, ecx, edx); return (ebx >> 17) & 1;
#endif
}
static int gptbridge_vector_have_avx2(void) {
#ifdef _WIN32
    int info[4]; __cpuid(info, 7); return (info[1] >> 5) & 1;
#else
    unsigned int eax, ebx, ecx, edx; __cpuid_count(7, 0, eax, ebx, ecx, edx); return (ebx >> 5) & 1;
#endif
}
static int gptbridge_vector_have_fma(void) {
#ifdef _WIN32
    int info[4]; __cpuid(info, 1); return (info[2] >> 12) & 1;
#else
    unsigned int eax, ebx, ecx, edx; __cpuid(1, eax, ebx, ecx, edx); return (ecx >> 12) & 1;
#endif
}
typedef enum { GPTBRIDGE_VEC_NONE = 0, GPTBRIDGE_VEC_AVX2 = 1, GPTBRIDGE_VEC_AVX512 = 2 } gptbridge_vec_level;
static gptbridge_vec_level gptbridge_vector_simd_level(void) {
    static int cached = -1;
    if (cached >= 0) return (gptbridge_vec_level)cached;
    if (gptbridge_vector_have_avx512f() && gptbridge_vector_have_avx512vl() && gptbridge_vector_have_avx512dq() && gptbridge_vector_have_fma()) cached = GPTBRIDGE_VEC_AVX512;
    else if (gptbridge_vector_have_avx2() && gptbridge_vector_have_fma()) cached = GPTBRIDGE_VEC_AVX2;
    else cached = GPTBRIDGE_VEC_NONE;
    return (gptbridge_vec_level)cached;
}

double gptbridge_native_vector_dot(
    const double* a, const double* b, int64_t dim) {
    /* BORROWED_READONLY x2 — Python owns both vectors. */
    const gptbridge_native_mem_const_view va =
        gptbridge_native_mem_borrow_const(a, dim);
    const gptbridge_native_mem_const_view vb =
        gptbridge_native_mem_borrow_const(b, dim);

    if (!gptbridge_native_mem_const_view_valid(va) ||
        !gptbridge_native_mem_const_view_valid(vb) || dim <= 0) {
        return 0.0;
    }

    const gptbridge_vec_level simd = gptbridge_vector_simd_level();
    if (simd == GPTBRIDGE_VEC_AVX512) {
#ifdef GPTBRIDGE_SIMD_AVX512
        __m512d acc = _mm512_setzero_pd();
        int64_t i = 0;
        for (; i + 8 <= dim; i += 8) {
            __m512d va8 = _mm512_loadu_pd(a + i);
            __m512d vb8 = _mm512_loadu_pd(b + i);
            acc = _mm512_fmadd_pd(va8, vb8, acc);
        }
        double tmp[8]; _mm512_storeu_pd(tmp, acc);
        double sum = tmp[0]+tmp[1]+tmp[2]+tmp[3]+tmp[4]+tmp[5]+tmp[6]+tmp[7];
        for (; i < dim; ++i) sum += a[i] * b[i];
        return sum;
#endif
    }
    if (simd == GPTBRIDGE_VEC_AVX2) {
#ifdef GPTBRIDGE_SIMD_AVX2
        __m256d acc = _mm256_setzero_pd();
        int64_t i = 0;
        for (; i + 4 <= dim; i += 4) {
            __m256d va4 = _mm256_loadu_pd(a + i);
            __m256d vb4 = _mm256_loadu_pd(b + i);
#if defined(__FMA__)
            acc = _mm256_fmadd_pd(va4, vb4, acc);
#else
            acc = _mm256_add_pd(acc, _mm256_mul_pd(va4, vb4));
#endif
        }
        double tmp[4]; _mm256_storeu_pd(tmp, acc);
        double sum = tmp[0] + tmp[1] + tmp[2] + tmp[3];
        for (; i < dim; ++i) sum += a[i] * b[i];
        return sum;
#endif
    }
    /* Scalar 4-way unrolled */
    double acc0 = 0.0, acc1 = 0.0, acc2 = 0.0, acc3 = 0.0;
    int64_t i = 0;
    for (; i + 4 <= dim; i += 4) {
        acc0 += a[i] * b[i];
        acc1 += a[i + 1] * b[i + 1];
        acc2 += a[i + 2] * b[i + 2];
        acc3 += a[i + 3] * b[i + 3];
    }
    for (; i < dim; ++i) acc0 += a[i] * b[i];
    return (acc0 + acc1) + (acc2 + acc3);
}

double gptbridge_native_vector_l2_norm(const double* a, int64_t dim) {
    if (a == NULL || dim <= 0) return 0.0;
#ifdef GPTBRIDGE_SIMD_AVX2
    __m256d acc = _mm256_setzero_pd();
    int64_t i = 0;
    for (; i + 4 <= dim; i += 4) {
        __m256d v = _mm256_loadu_pd(a + i);
#if defined(__FMA__)
        acc = _mm256_fmadd_pd(v, v, acc);
#else
        acc = _mm256_add_pd(acc, _mm256_mul_pd(v, v));
#endif
    }
    double tmp[4]; _mm256_storeu_pd(tmp, acc);
    double sum = tmp[0] + tmp[1] + tmp[2] + tmp[3];
    for (; i < dim; ++i) sum += a[i] * a[i];
    return sqrt(sum);
#else
    double acc0 = 0.0, acc1 = 0.0, acc2 = 0.0, acc3 = 0.0;
    int64_t i = 0;
    for (; i + 4 <= dim; i += 4) {
        const double v0 = a[i], v1 = a[i + 1], v2 = a[i + 2], v3 = a[i + 3];
        acc0 += v0 * v0; acc1 += v1 * v1; acc2 += v2 * v2; acc3 += v3 * v3;
    }
    for (; i < dim; ++i) acc0 += a[i] * a[i];
    return sqrt((acc0 + acc1) + (acc2 + acc3));
#endif
}

double gptbridge_native_vector_cosine_similarity(
    const double* a, const double* b, int64_t dim) {
    if (a == NULL || b == NULL || dim <= 0) return 0.0;
#ifdef GPTBRIDGE_SIMD_AVX2
    __m256d dot = _mm256_setzero_pd(), na = _mm256_setzero_pd(), nb = _mm256_setzero_pd();
    int64_t i = 0;
    for (; i + 4 <= dim; i += 4) {
        __m256d va = _mm256_loadu_pd(a + i), vb = _mm256_loadu_pd(b + i);
#if defined(__FMA__)
        dot = _mm256_fmadd_pd(va, vb, dot);
        na = _mm256_fmadd_pd(va, va, na);
        nb = _mm256_fmadd_pd(vb, vb, nb);
#else
        dot = _mm256_add_pd(dot, _mm256_mul_pd(va, vb));
        na = _mm256_add_pd(na, _mm256_mul_pd(va, va));
        nb = _mm256_add_pd(nb, _mm256_mul_pd(vb, vb));
#endif
    }
    double td[4], ta[4], tb[4]; _mm256_storeu_pd(td, dot); _mm256_storeu_pd(ta, na); _mm256_storeu_pd(tb, nb);
    double dot_val = td[0]+td[1]+td[2]+td[3], na_val = ta[0]+ta[1]+ta[2]+ta[3], nb_val = tb[0]+tb[1]+tb[2]+tb[3];
    for (; i < dim; ++i) { double va=a[i], vb=b[i]; dot_val+=va*vb; na_val+=va*va; nb_val+=vb*vb; }
    double norm_a = sqrt(na_val), norm_b = sqrt(nb_val);
#else
    double dot0 = 0.0, dot1 = 0.0, dot2 = 0.0, dot3 = 0.0;
    double na0 = 0.0, na1 = 0.0, na2 = 0.0, na3 = 0.0;
    double nb0 = 0.0, nb1 = 0.0, nb2 = 0.0, nb3 = 0.0;
    double dot_val, norm_a, norm_b;
    int64_t i = 0;
    for (; i + 4 <= dim; i += 4) {
        const double a0 = a[i], a1 = a[i + 1], a2 = a[i + 2], a3 = a[i + 3];
        const double b0 = b[i], b1 = b[i + 1], b2 = b[i + 2], b3 = b[i + 3];
        dot0 += a0 * b0; dot1 += a1 * b1; dot2 += a2 * b2; dot3 += a3 * b3;
        na0 += a0 * a0; na1 += a1 * a1; na2 += a2 * a2; na3 += a3 * a3;
        nb0 += b0 * b0; nb1 += b1 * b1; nb2 += b2 * b2; nb3 += b3 * b3;
    }
    for (; i < dim; ++i) { const double va = a[i], vb = b[i]; dot0 += va * vb; na0 += va * va; nb0 += vb * vb; }
    dot_val = (dot0 + dot1) + (dot2 + dot3);
    norm_a = sqrt((na0 + na1) + (na2 + na3));
    norm_b = sqrt((nb0 + nb1) + (nb2 + nb3));
#endif

    if (norm_a == 0.0 || norm_b == 0.0) {
        return 0.0;
    }

    return dot_val / (norm_a * norm_b);
}

int gptbridge_native_vector_batch_dot(
    const double* const* a_ptrs,
    const double* const* b_ptrs,
    int64_t count,
    int64_t dim,
    double* results) {
    /* a_ptrs/b_ptrs: BORROWED_READONLY pointer arrays;
     * results: CALLER_PROVIDED_OUTPUT (caller allocated, we write). */
    const gptbridge_native_mem_out_view out =
        gptbridge_native_mem_caller_output(results, count);
    int64_t i;

    if (a_ptrs == NULL || b_ptrs == NULL ||
        !gptbridge_native_mem_mut_view_valid(out) ||
        count <= 0 || dim <= 0) {
        return 1;  /* invalid arguments */
    }

    for (i = 0; i < count; ++i) {
        results[i] = gptbridge_native_vector_dot(a_ptrs[i], b_ptrs[i], dim);
    }

    return 0;  /* success */
}

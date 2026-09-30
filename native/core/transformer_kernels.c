/* transformer_kernels.c - B94 fragment of transformer.c.
 * Textually included once by transformer.c (single TU): runtime CPUID
 * SIMD detection + shared row kernels. */

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

    /* Diagnostic/test override (ACC-1 acceptance): GPTBRIDGE_SIMD_LEVEL
     * = none|scalar|avx2|avx512 may only LOWER the dispatched level below
     * the CPUID-detected capability — it never enables instructions the
     * CPU lacks, so non-AVX hosts still take the identical scalar path. */
    {
        const char* forced = getenv("GPTBRIDGE_SIMD_LEVEL");
        gptbridge_simd_level requested = cached;
        if (forced && forced[0]) {
            if (!strcmp(forced, "none") || !strcmp(forced, "scalar") || !strcmp(forced, "0")) {
                requested = GPTBRIDGE_SIMD_NONE;
            } else if (!strcmp(forced, "avx2") || !strcmp(forced, "1")) {
                requested = GPTBRIDGE_SIMD_AVX2;
            } else if (!strcmp(forced, "avx512") || !strcmp(forced, "2")) {
                requested = GPTBRIDGE_SIMD_AVX512;
            }
            if (requested < cached) cached = requested;
        }
    }
    return (gptbridge_simd_level)cached;
}

int gptbridge_native_simd_effective_level(void) {
    return (int)gptbridge_native_simd_level();
}

/* Find max in a row for numerical stability of softmax.
 * Vectorized: AVX-512 (8×double), AVX2 (4×double), scalar fallback. */
static double row_max(const double* row, int64_t cols) {
    const gptbridge_simd_level simd = gptbridge_native_simd_level();
    int64_t c = 0;
    double max_val = row[0];

    if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_HAVE_AVX512
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
#ifdef GPTBRIDGE_HAVE_AVX2
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
#ifdef GPTBRIDGE_HAVE_AVX512
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
#ifdef GPTBRIDGE_HAVE_AVX2
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
#ifdef GPTBRIDGE_HAVE_AVX512
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
#ifdef GPTBRIDGE_HAVE_AVX2
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

/* Row dot product a·b over n doubles.
 * SIMD: AVX-512 8×double FMA, AVX2 4×double (FMA when compiled); scalar
 * tail covers the remainder (and the whole row when the SIMD headers are
 * unavailable at compile time). */
static double dot_row(const double* a, const double* b, int64_t n,
                      gptbridge_simd_level simd) {
    double sum = 0.0;
    int64_t i = 0;

    if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_HAVE_AVX512
        __m512d acc = _mm512_setzero_pd();
        for (; i + 8 <= n; i += 8) {
            acc = _mm512_fmadd_pd(
                _mm512_loadu_pd(a + i), _mm512_loadu_pd(b + i), acc);
        }
        {
            double tmp[8];
            _mm512_storeu_pd(tmp, acc);
            sum = tmp[0] + tmp[1] + tmp[2] + tmp[3]
                + tmp[4] + tmp[5] + tmp[6] + tmp[7];
        }
#endif
    } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_HAVE_AVX2
        __m256d acc = _mm256_setzero_pd();
        for (; i + 4 <= n; i += 4) {
#if defined(__FMA__)
            acc = _mm256_fmadd_pd(
                _mm256_loadu_pd(a + i), _mm256_loadu_pd(b + i), acc);
#else
            acc = _mm256_add_pd(acc, _mm256_mul_pd(
                _mm256_loadu_pd(a + i), _mm256_loadu_pd(b + i)));
#endif
        }
        {
            double tmp[4];
            _mm256_storeu_pd(tmp, acc);
            sum = tmp[0] + tmp[1] + tmp[2] + tmp[3];
        }
#endif
    }
    for (; i < n; ++i) sum += a[i] * b[i];
    return sum;
}

/* y[j] += w * x[j] over n doubles — SIMD axpy with broadcast w. */
static void axpy_row(double* y, double w, const double* x, int64_t n,
                     gptbridge_simd_level simd) {
    int64_t j = 0;

    if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_HAVE_AVX512
        const __m512d wv = _mm512_set1_pd(w);
        for (; j + 8 <= n; j += 8) {
            const __m512d xv = _mm512_loadu_pd(x + j);
            const __m512d yv = _mm512_loadu_pd(y + j);
            _mm512_storeu_pd(y + j, _mm512_fmadd_pd(wv, xv, yv));
        }
#endif
    } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_HAVE_AVX2
        const __m256d wv = _mm256_set1_pd(w);
        for (; j + 4 <= n; j += 4) {
            const __m256d xv = _mm256_loadu_pd(x + j);
            const __m256d yv = _mm256_loadu_pd(y + j);
#if defined(__FMA__)
            _mm256_storeu_pd(y + j, _mm256_fmadd_pd(wv, xv, yv));
#else
            _mm256_storeu_pd(y + j, _mm256_add_pd(yv, _mm256_mul_pd(wv, xv)));
#endif
        }
#endif
    }
    for (; j < n; ++j) y[j] += w * x[j];
}

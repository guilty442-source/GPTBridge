/* transformer_ops_a.c - B94 fragment of transformer.c.
 * Textually included once by transformer.c (single TU): matmul,
 * softmax, rmsnorm entry points. */

/* Above this row length the register-tile column walk strides B too far
 * for hardware prefetch — dispatch keeps the contiguous per-row axpy
 * stream for DRAM-bound wide-n shapes. */
#define MATMUL_TILE_MAX_N ((int64_t)8192)

/* Per-row axpy inner pass over [j0, j1): zero the range, then
 * c_row[j] += a_row[p] * b_row[j] p-ascending.  This is the original
 * kernel body — reused verbatim as the tail path of the register-tiled
 * kernels so tail columns keep the exact same per-element op sequence
 * (8/4-wide fmadd + scalar <8 remainder) as the legacy pass. */
static void transformer_matmul_axpy_range(
    const double* a_row, int64_t k,
    const double* b, int64_t n, double* c_row,
    int64_t j0, int64_t j1,
    gptbridge_simd_level simd) {
    int64_t p, j;
    for (j = j0; j < j1; ++j) c_row[j] = 0.0;

    if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_HAVE_AVX512
        for (p = 0; p < k; ++p) {
            const double a_val = a_row[p];
            const double* b_row = b + p * n;
            __m512d av = _mm512_set1_pd(a_val);
            int64_t j8 = j0;
            for (; j8 + 8 <= j1; j8 += 8) {
                __m512d bv = _mm512_loadu_pd(b_row + j8);
                __m512d cv = _mm512_loadu_pd(c_row + j8);
                cv = _mm512_fmadd_pd(av, bv, cv);
                _mm512_storeu_pd(c_row + j8, cv);
            }
            for (; j8 < j1; ++j8) c_row[j8] += a_val * b_row[j8];
        }
        return;
#endif
    } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_HAVE_AVX2
        for (p = 0; p < k; ++p) {
            const double a_val = a_row[p];
            const double* b_row = b + p * n;
            __m256d av = _mm256_set1_pd(a_val);
            int64_t j4 = j0;
            for (; j4 + 4 <= j1; j4 += 4) {
                __m256d bv = _mm256_loadu_pd(b_row + j4);
                __m256d cv = _mm256_loadu_pd(c_row + j4);
#if defined(__FMA__)
                cv = _mm256_fmadd_pd(av, bv, cv);
#else
                cv = _mm256_add_pd(cv, _mm256_mul_pd(av, bv));
#endif
                _mm256_storeu_pd(c_row + j4, cv);
            }
            for (; j4 < j1; ++j4) c_row[j4] += a_val * b_row[j4];
        }
        return;
#endif
    }
    for (p = 0; p < k; ++p) {
        const double a_val = a_row[p];
        const double* b_row = b + p * n;
        for (j = j0; j < j1; ++j) c_row[j] += a_val * b_row[j];
    }
}

/* Register-tiled pass over four output rows [i..i+3] x stripe [j0,j1):
 * column tiles keep their accumulators in vector registers across the
 * whole k loop, so each C element is written ONCE instead of a
 * load+store round trip per p (the old store-port-bound chain) — and
 * each streamed B tile feeds four rows, cutting B traffic by 4x.
 * Tail columns (<tile) fall back to the per-row axpy pass.  Every output
 * element still accumulates p-ascending — bit-identical per element. */
static void transformer_matmul_rows4_range(
    const double* a, int64_t k,
    const double* b, int64_t n, double* c,
    int64_t j0, int64_t j1,
    gptbridge_simd_level simd) {
    const double* a0 = a;
    const double* a1 = a + k;
    const double* a2 = a1 + k;
    const double* a3 = a2 + k;
    double* c0 = c;
    double* c1 = c + n;
    double* c2 = c1 + n;
    double* c3 = c2 + n;
    int64_t p;
    int64_t jb = j0;

    /* Wide-n guard: when a B row exceeds ~64KB the tile's 256B slice
     * strides too far for hardware prefetch — the contiguous per-row
     * stream wins on DRAM-bound shapes (measured crossover n>~8k). */
    if (n > MATMUL_TILE_MAX_N) {
        transformer_matmul_axpy_range(a0, k, b, n, c0, j0, j1, simd);
        transformer_matmul_axpy_range(a1, k, b, n, c1, j0, j1, simd);
        transformer_matmul_axpy_range(a2, k, b, n, c2, j0, j1, simd);
        transformer_matmul_axpy_range(a3, k, b, n, c3, j0, j1, simd);
        return;
    }

    if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_HAVE_AVX512
        /* 32-column tile: 4 rows x 4 zmm accumulators = 16 regs. */
        for (; jb + 32 <= j1; jb += 32) {
            __m512d acc00 = _mm512_setzero_pd(), acc01 = _mm512_setzero_pd(),
                    acc02 = _mm512_setzero_pd(), acc03 = _mm512_setzero_pd();
            __m512d acc10 = _mm512_setzero_pd(), acc11 = _mm512_setzero_pd(),
                    acc12 = _mm512_setzero_pd(), acc13 = _mm512_setzero_pd();
            __m512d acc20 = _mm512_setzero_pd(), acc21 = _mm512_setzero_pd(),
                    acc22 = _mm512_setzero_pd(), acc23 = _mm512_setzero_pd();
            __m512d acc30 = _mm512_setzero_pd(), acc31 = _mm512_setzero_pd(),
                    acc32 = _mm512_setzero_pd(), acc33 = _mm512_setzero_pd();
            for (p = 0; p < k; ++p) {
                const double* b_row = b + p * n + jb;
                const __m512d bv0 = _mm512_loadu_pd(b_row);
                const __m512d bv1 = _mm512_loadu_pd(b_row + 8);
                const __m512d bv2 = _mm512_loadu_pd(b_row + 16);
                const __m512d bv3 = _mm512_loadu_pd(b_row + 24);
                const __m512d av0 = _mm512_set1_pd(a0[p]);
                const __m512d av1 = _mm512_set1_pd(a1[p]);
                const __m512d av2 = _mm512_set1_pd(a2[p]);
                const __m512d av3 = _mm512_set1_pd(a3[p]);
                acc00 = _mm512_fmadd_pd(av0, bv0, acc00);
                acc01 = _mm512_fmadd_pd(av0, bv1, acc01);
                acc02 = _mm512_fmadd_pd(av0, bv2, acc02);
                acc03 = _mm512_fmadd_pd(av0, bv3, acc03);
                acc10 = _mm512_fmadd_pd(av1, bv0, acc10);
                acc11 = _mm512_fmadd_pd(av1, bv1, acc11);
                acc12 = _mm512_fmadd_pd(av1, bv2, acc12);
                acc13 = _mm512_fmadd_pd(av1, bv3, acc13);
                acc20 = _mm512_fmadd_pd(av2, bv0, acc20);
                acc21 = _mm512_fmadd_pd(av2, bv1, acc21);
                acc22 = _mm512_fmadd_pd(av2, bv2, acc22);
                acc23 = _mm512_fmadd_pd(av2, bv3, acc23);
                acc30 = _mm512_fmadd_pd(av3, bv0, acc30);
                acc31 = _mm512_fmadd_pd(av3, bv1, acc31);
                acc32 = _mm512_fmadd_pd(av3, bv2, acc32);
                acc33 = _mm512_fmadd_pd(av3, bv3, acc33);
            }
            _mm512_storeu_pd(c0 + jb, acc00);
            _mm512_storeu_pd(c0 + jb + 8, acc01);
            _mm512_storeu_pd(c0 + jb + 16, acc02);
            _mm512_storeu_pd(c0 + jb + 24, acc03);
            _mm512_storeu_pd(c1 + jb, acc10);
            _mm512_storeu_pd(c1 + jb + 8, acc11);
            _mm512_storeu_pd(c1 + jb + 16, acc12);
            _mm512_storeu_pd(c1 + jb + 24, acc13);
            _mm512_storeu_pd(c2 + jb, acc20);
            _mm512_storeu_pd(c2 + jb + 8, acc21);
            _mm512_storeu_pd(c2 + jb + 16, acc22);
            _mm512_storeu_pd(c2 + jb + 24, acc23);
            _mm512_storeu_pd(c3 + jb, acc30);
            _mm512_storeu_pd(c3 + jb + 8, acc31);
            _mm512_storeu_pd(c3 + jb + 16, acc32);
            _mm512_storeu_pd(c3 + jb + 24, acc33);
        }
#endif
    } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_HAVE_AVX2
        /* 8-column tile: 4 rows x 2 ymm accumulators = 8 regs. */
        for (; jb + 8 <= j1; jb += 8) {
            __m256d acc00 = _mm256_setzero_pd(), acc01 = _mm256_setzero_pd();
            __m256d acc10 = _mm256_setzero_pd(), acc11 = _mm256_setzero_pd();
            __m256d acc20 = _mm256_setzero_pd(), acc21 = _mm256_setzero_pd();
            __m256d acc30 = _mm256_setzero_pd(), acc31 = _mm256_setzero_pd();
            for (p = 0; p < k; ++p) {
                const double* b_row = b + p * n + jb;
                const __m256d bv0 = _mm256_loadu_pd(b_row);
                const __m256d bv1 = _mm256_loadu_pd(b_row + 4);
                const __m256d av0 = _mm256_set1_pd(a0[p]);
                const __m256d av1 = _mm256_set1_pd(a1[p]);
                const __m256d av2 = _mm256_set1_pd(a2[p]);
                const __m256d av3 = _mm256_set1_pd(a3[p]);
#if defined(__FMA__)
                acc00 = _mm256_fmadd_pd(av0, bv0, acc00);
                acc01 = _mm256_fmadd_pd(av0, bv1, acc01);
                acc10 = _mm256_fmadd_pd(av1, bv0, acc10);
                acc11 = _mm256_fmadd_pd(av1, bv1, acc11);
                acc20 = _mm256_fmadd_pd(av2, bv0, acc20);
                acc21 = _mm256_fmadd_pd(av2, bv1, acc21);
                acc30 = _mm256_fmadd_pd(av3, bv0, acc30);
                acc31 = _mm256_fmadd_pd(av3, bv1, acc31);
#else
                acc00 = _mm256_add_pd(acc00, _mm256_mul_pd(av0, bv0));
                acc01 = _mm256_add_pd(acc01, _mm256_mul_pd(av0, bv1));
                acc10 = _mm256_add_pd(acc10, _mm256_mul_pd(av1, bv0));
                acc11 = _mm256_add_pd(acc11, _mm256_mul_pd(av1, bv1));
                acc20 = _mm256_add_pd(acc20, _mm256_mul_pd(av2, bv0));
                acc21 = _mm256_add_pd(acc21, _mm256_mul_pd(av2, bv1));
                acc30 = _mm256_add_pd(acc30, _mm256_mul_pd(av3, bv0));
                acc31 = _mm256_add_pd(acc31, _mm256_mul_pd(av3, bv1));
#endif
            }
            _mm256_storeu_pd(c0 + jb, acc00);
            _mm256_storeu_pd(c0 + jb + 4, acc01);
            _mm256_storeu_pd(c1 + jb, acc10);
            _mm256_storeu_pd(c1 + jb + 4, acc11);
            _mm256_storeu_pd(c2 + jb, acc20);
            _mm256_storeu_pd(c2 + jb + 4, acc21);
            _mm256_storeu_pd(c3 + jb, acc30);
            _mm256_storeu_pd(c3 + jb + 4, acc31);
        }
#endif
    }
    /* Tail columns (< tile width) and the scalar level keep the
     * per-row axpy pass — same per-element op sequence as the legacy
     * kernel. */
    if (jb < j1) {
        transformer_matmul_axpy_range(a0, k, b, n, c0, jb, j1, simd);
        transformer_matmul_axpy_range(a1, k, b, n, c1, jb, j1, simd);
        transformer_matmul_axpy_range(a2, k, b, n, c2, jb, j1, simd);
        transformer_matmul_axpy_range(a3, k, b, n, c3, jb, j1, simd);
    }
}

/* Register-tiled single row (m<4 remainder / decode m=1): 64-column
 * tiles keep 8 zmm accumulators live across k so each C element is
 * written once.  Tail columns use the plain per-row axpy pass. */
static void transformer_matmul_tile_row(
    const double* a_row, int64_t k,
    const double* b, int64_t n, double* c_row,
    int64_t j0, int64_t j1,
    gptbridge_simd_level simd) {
    int64_t p;
    int64_t jb = j0;

    if (n > MATMUL_TILE_MAX_N) {
        transformer_matmul_axpy_range(a_row, k, b, n, c_row, j0, j1, simd);
        return;
    }

    if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_HAVE_AVX512
        for (; jb + 64 <= j1; jb += 64) {
            __m512d acc0 = _mm512_setzero_pd(), acc1 = _mm512_setzero_pd(),
                    acc2 = _mm512_setzero_pd(), acc3 = _mm512_setzero_pd(),
                    acc4 = _mm512_setzero_pd(), acc5 = _mm512_setzero_pd(),
                    acc6 = _mm512_setzero_pd(), acc7 = _mm512_setzero_pd();
            for (p = 0; p < k; ++p) {
                const double* b_row = b + p * n + jb;
                const __m512d av = _mm512_set1_pd(a_row[p]);
                acc0 = _mm512_fmadd_pd(av, _mm512_loadu_pd(b_row), acc0);
                acc1 = _mm512_fmadd_pd(av, _mm512_loadu_pd(b_row + 8), acc1);
                acc2 = _mm512_fmadd_pd(av, _mm512_loadu_pd(b_row + 16), acc2);
                acc3 = _mm512_fmadd_pd(av, _mm512_loadu_pd(b_row + 24), acc3);
                acc4 = _mm512_fmadd_pd(av, _mm512_loadu_pd(b_row + 32), acc4);
                acc5 = _mm512_fmadd_pd(av, _mm512_loadu_pd(b_row + 40), acc5);
                acc6 = _mm512_fmadd_pd(av, _mm512_loadu_pd(b_row + 48), acc6);
                acc7 = _mm512_fmadd_pd(av, _mm512_loadu_pd(b_row + 56), acc7);
            }
            _mm512_storeu_pd(c_row + jb, acc0);
            _mm512_storeu_pd(c_row + jb + 8, acc1);
            _mm512_storeu_pd(c_row + jb + 16, acc2);
            _mm512_storeu_pd(c_row + jb + 24, acc3);
            _mm512_storeu_pd(c_row + jb + 32, acc4);
            _mm512_storeu_pd(c_row + jb + 40, acc5);
            _mm512_storeu_pd(c_row + jb + 48, acc6);
            _mm512_storeu_pd(c_row + jb + 56, acc7);
        }
#endif
    } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_HAVE_AVX2
        for (; jb + 16 <= j1; jb += 16) {
            __m256d acc0 = _mm256_setzero_pd(), acc1 = _mm256_setzero_pd(),
                    acc2 = _mm256_setzero_pd(), acc3 = _mm256_setzero_pd();
            for (p = 0; p < k; ++p) {
                const double* b_row = b + p * n + jb;
                const __m256d av = _mm256_set1_pd(a_row[p]);
#if defined(__FMA__)
                acc0 = _mm256_fmadd_pd(av, _mm256_loadu_pd(b_row), acc0);
                acc1 = _mm256_fmadd_pd(av, _mm256_loadu_pd(b_row + 4), acc1);
                acc2 = _mm256_fmadd_pd(av, _mm256_loadu_pd(b_row + 8), acc2);
                acc3 = _mm256_fmadd_pd(av, _mm256_loadu_pd(b_row + 12), acc3);
#else
                acc0 = _mm256_add_pd(acc0,
                    _mm256_mul_pd(av, _mm256_loadu_pd(b_row)));
                acc1 = _mm256_add_pd(acc1,
                    _mm256_mul_pd(av, _mm256_loadu_pd(b_row + 4)));
                acc2 = _mm256_add_pd(acc2,
                    _mm256_mul_pd(av, _mm256_loadu_pd(b_row + 8)));
                acc3 = _mm256_add_pd(acc3,
                    _mm256_mul_pd(av, _mm256_loadu_pd(b_row + 12)));
#endif
            }
            _mm256_storeu_pd(c_row + jb, acc0);
            _mm256_storeu_pd(c_row + jb + 4, acc1);
            _mm256_storeu_pd(c_row + jb + 8, acc2);
            _mm256_storeu_pd(c_row + jb + 12, acc3);
        }
#endif
    }
    if (jb < j1) {
        transformer_matmul_axpy_range(a_row, k, b, n, c_row, jb, j1, simd);
    }
}

/* Shared row-major i-k-j (axpy) kernel: C[M x N] = A[M x K] * B[K x N],
 * restricted to the output column stripe [j0, j1).  Callers validate
 * arguments and the output extent; this kernel only runs the loop.
 * Full 4-row blocks take the register-tiled pass; the <4 remainder
 * rows use the register-tiled single row. */
static void transformer_matmul_rows_range(
    const double* a, int64_t m, int64_t k,
    const double* b, int64_t n, double* c,
    int64_t j0, int64_t j1,
    gptbridge_simd_level simd) {
    int64_t i = 0;
    for (; i + 4 <= m; i += 4) {
        transformer_matmul_rows4_range(
            a + i * k, k, b, n, c + i * n, j0, j1, simd);
    }
    for (; i < m; ++i) {
        transformer_matmul_tile_row(
            a + i * k, k, b, n, c + i * n, j0, j1, simd);
    }
}

/* Parallel column-striped GEMM.  The output column range [0, n) is split
 * into T stripes; each stripe runs the unchanged i-k-j kernel over its
 * own [j0, j1) slice of every row, so each output element keeps its exact
 * accumulation order — results are bit-identical to the sequential pass;
 * only the wall-clock differs.
 *
 * Threads come from the OS *default process* thread pool
 * (CreateThreadpoolWork/Submit/Wait): this core neither creates nor owns
 * a pool and no work item outlives the call — honoring the
 * "no per-request thread pool" bound.  GPTBRIDGE_MATMUL_THREADS is read
 * once at first use: unset/0/<=1 = min(8, logical cores) stripes; 1 =
 * serial; N = N stripes (cap 64).  Shapes below MATMUL_PAR_MIN_ELEMS
 * stay sequential — the dispatch overhead would dominate. */
#define MATMUL_PAR_MIN_ELEMS ((int64_t)512 * 1024)
#define MATMUL_PAR_MIN_STRIPE 16
#define MATMUL_PAR_MAX_STRIPES 64

#ifdef _WIN32
static int transformer_matmul_threads(void) {
    static volatile long cached = -1;
    long threads = cached;
    if (threads < 0) {
        const char* value = getenv("GPTBRIDGE_MATMUL_THREADS");
        if (value != NULL && *value != '\0') {
            long parsed = strtol(value, NULL, 10);
            threads = parsed <= 1 ? 1
                    : parsed > MATMUL_PAR_MAX_STRIPES ? MATMUL_PAR_MAX_STRIPES
                    : parsed;
        } else {
            SYSTEM_INFO info;
            GetSystemInfo(&info);
            threads = info.dwNumberOfProcessors <= 1 ? 1
                    : info.dwNumberOfProcessors > 8 ? 8
                    : (long)info.dwNumberOfProcessors;
        }
        cached = threads;
    }
    return (int)threads;
}

typedef struct {
    const double* a;
    int64_t m;
    int64_t k;
    const double* b;
    int64_t n;
    double* c;
    int64_t j0;
    int64_t j1;
    gptbridge_simd_level simd;
} matmul_stripe_ctx;

static void CALLBACK transformer_matmul_stripe_cb(
    PTP_CALLBACK_INSTANCE instance, PVOID context, PTP_WORK work) {
    const matmul_stripe_ctx* stripe = (const matmul_stripe_ctx*)context;
    (void)instance;
    (void)work;
    transformer_matmul_rows_range(
        stripe->a, stripe->m, stripe->k, stripe->b, stripe->n, stripe->c,
        stripe->j0, stripe->j1, stripe->simd);
}
#else
static int transformer_matmul_threads(void) { return 1; }
#endif

static void transformer_matmul_dispatch(
    const double* a, int64_t m, int64_t k,
    const double* b, int64_t n, double* c,
    gptbridge_simd_level simd) {
    int64_t work = 0;
    if (!gptbridge_native_mem_checked_mul_i64(k, n, &work)) {
        work = 0;
    }
#ifdef _WIN32
    if (work >= MATMUL_PAR_MIN_ELEMS && n >= 2 * MATMUL_PAR_MIN_STRIPE) {
        const int threads = transformer_matmul_threads();
        int64_t stripes = threads;
        int64_t s, t, j0;
        matmul_stripe_ctx ctxs[MATMUL_PAR_MAX_STRIPES];
        PTP_WORK works[MATMUL_PAR_MAX_STRIPES];
        if (stripes > MATMUL_PAR_MAX_STRIPES) stripes = MATMUL_PAR_MAX_STRIPES;
        if (n / stripes < MATMUL_PAR_MIN_STRIPE) {
            stripes = n / MATMUL_PAR_MIN_STRIPE;
        }
        if (stripes >= 2) {
            const int64_t width = (n + stripes - 1) / stripes;
            s = 0;
            j0 = 0;
            while (j0 < n && s < stripes) {
                ctxs[s].a = a;
                ctxs[s].m = m;
                ctxs[s].k = k;
                ctxs[s].b = b;
                ctxs[s].n = n;
                ctxs[s].c = c;
                ctxs[s].j0 = j0;
                ctxs[s].j1 = (j0 + width < n) ? j0 + width : n;
                ctxs[s].simd = simd;
                works[s] = NULL;
                ++s;
                j0 += width;
            }
            for (t = 1; t < s; ++t) {
                works[t] = CreateThreadpoolWork(
                    transformer_matmul_stripe_cb, &ctxs[t], NULL);
                if (works[t] != NULL) {
                    SubmitThreadpoolWork(works[t]);
                } else {
                    /* Pool object creation failed — degrade to inline
                     * execution; semantics unchanged. */
                    transformer_matmul_rows_range(
                        ctxs[t].a, ctxs[t].m, ctxs[t].k, ctxs[t].b,
                        ctxs[t].n, ctxs[t].c, ctxs[t].j0, ctxs[t].j1,
                        ctxs[t].simd);
                }
            }
            transformer_matmul_rows_range(
                ctxs[0].a, ctxs[0].m, ctxs[0].k, ctxs[0].b, ctxs[0].n,
                ctxs[0].c, ctxs[0].j0, ctxs[0].j1, ctxs[0].simd);
            for (t = 1; t < s; ++t) {
                if (works[t] != NULL) {
                    WaitForThreadpoolWorkCallbacks(works[t], FALSE);
                    CloseThreadpoolWork(works[t]);
                }
            }
            return;
        }
    }
#endif
    transformer_matmul_rows_range(a, m, k, b, n, c, 0, n, simd);
}

static void transformer_matmul_rows(
    const double* a, int64_t m, int64_t k,
    const double* b, int64_t n, double* c,
    gptbridge_simd_level simd) {
    transformer_matmul_dispatch(a, m, k, b, n, c, simd);
}

int gptbridge_native_transformer_matmul(
    const double* a, int64_t m, int64_t k,
    const double* b, int64_t k_in, int64_t n,
    double* c) {
    /* a/b: BORROWED_READONLY; c: CALLER_PROVIDED_OUTPUT.
     * Overflow-check the output extent before writing (shape*budget). */
    int64_t c_elems = 0;
    gptbridge_native_mem_out_view out;

    if (!gptbridge_native_mem_checked_mul_i64(m, n, &c_elems)) {
        return 1;  /* shape overflow ??never allocate/write */
    }
    out = gptbridge_native_mem_caller_output(c, c_elems);
    if (a == NULL || b == NULL ||
        !gptbridge_native_mem_mut_view_valid(out) ||
        m <= 0 || k <= 0 || n <= 0 || k != k_in) {
        return 1;  /* invalid arguments */
    }

    transformer_matmul_rows(
        a, m, k, b, n, c, gptbridge_native_simd_level());
    return 0;
}

int gptbridge_native_transformer_matmul_grouped(
    const double* a, const int64_t* group_rows, int64_t groups,
    const double* const* b_list, int64_t k, int64_t n,
    double* c) {
    /* a/b_list/c: BORROWED (c CALLER_PROVIDED_OUTPUT).  a holds the groups'
     * row-blocks concatenated ([sum(group_rows) x k]); b_list[g] is that
     * group's [k x n] weight; c receives the concatenated
     * [sum(group_rows) x n] outputs in the same order.  One dispatch for
     * the whole group loop (R5 grouped GEMM). */
    int64_t g;
    int64_t total_rows = 0;
    int64_t c_elems = 0;
    gptbridge_native_mem_out_view out;
    int64_t a_off = 0, c_off = 0;
    gptbridge_simd_level simd;

    if (a == NULL || group_rows == NULL || b_list == NULL || c == NULL ||
        groups <= 0 || k <= 0 || n <= 0) {
        return 1;
    }
    for (g = 0; g < groups; ++g) {
        if (group_rows[g] < 0 || b_list[g] == NULL) {
            return 1;
        }
        if (group_rows[g] > INT64_MAX - total_rows) {
            return 1;  /* row-count overflow ??never allocate/write */
        }
        total_rows += group_rows[g];
    }
    if (total_rows <= 0 ||
        !gptbridge_native_mem_checked_mul_i64(total_rows, n, &c_elems)) {
        return 1;
    }
    out = gptbridge_native_mem_caller_output(c, c_elems);
    if (!gptbridge_native_mem_mut_view_valid(out)) {
        return 1;
    }

    simd = gptbridge_native_simd_level();
    for (g = 0; g < groups; ++g) {
        const int64_t m_g = group_rows[g];
        if (m_g == 0) {
            continue;
        }
        transformer_matmul_rows(
            a + a_off, m_g, k, b_list[g], n, c + c_off, simd);
        a_off += m_g * k;
        c_off += m_g * n;
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

int gptbridge_native_transformer_rmsnorm(
    const double* input, int64_t rows, int64_t cols,
    const double* weight, double eps,
    double* output) {
    /* input/weight: BORROWED_READONLY; output: CALLER_PROVIDED_OUTPUT. */
    int64_t elems = 0;
    gptbridge_native_mem_out_view out;
    const gptbridge_simd_level simd = gptbridge_native_simd_level();
    int64_t r, c;

    if (!gptbridge_native_mem_checked_mul_i64(rows, cols, &elems)) {
        return 1;
    }
    out = gptbridge_native_mem_caller_output(output, elems);
    if (input == NULL || weight == NULL ||
        !gptbridge_native_mem_mut_view_valid(out) ||
        rows <= 0 || cols <= 0 || eps < 0.0) {
        return 1;
    }

    for (r = 0; r < rows; ++r) {
        const double* in_row = input + r * cols;
        double* out_row = output + r * cols;
        double sum_sq = 0.0;
        double inv_rms;
        c = 0;

        if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_HAVE_AVX512
            __m512d acc = _mm512_setzero_pd();
            for (; c + 8 <= cols; c += 8) {
                __m512d v = _mm512_loadu_pd(in_row + c);
                acc = _mm512_fmadd_pd(v, v, acc);
            }
            {
                double tmp[8];
                _mm512_storeu_pd(tmp, acc);
                sum_sq = tmp[0]+tmp[1]+tmp[2]+tmp[3]+tmp[4]+tmp[5]+tmp[6]+tmp[7];
            }
#endif
        } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_HAVE_AVX2
            __m256d acc = _mm256_setzero_pd();
            for (; c + 4 <= cols; c += 4) {
                __m256d v = _mm256_loadu_pd(in_row + c);
#if defined(__FMA__)
                acc = _mm256_fmadd_pd(v, v, acc);
#else
                acc = _mm256_add_pd(acc, _mm256_mul_pd(v, v));
#endif
            }
            {
                double tmp[4];
                _mm256_storeu_pd(tmp, acc);
                sum_sq = tmp[0] + tmp[1] + tmp[2] + tmp[3];
            }
#endif
        }
        for (; c < cols; ++c) sum_sq += in_row[c] * in_row[c];

        inv_rms = 1.0 / sqrt((sum_sq / (double)cols) + eps);
        c = 0;
        if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_HAVE_AVX512
            __m512d scale = _mm512_set1_pd(inv_rms);
            for (; c + 8 <= cols; c += 8) {
                __m512d x = _mm512_loadu_pd(in_row + c);
                __m512d w = _mm512_loadu_pd(weight + c);
                _mm512_storeu_pd(out_row + c, _mm512_mul_pd(_mm512_mul_pd(x, scale), w));
            }
#endif
        } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_HAVE_AVX2
            __m256d scale = _mm256_set1_pd(inv_rms);
            for (; c + 4 <= cols; c += 4) {
                __m256d x = _mm256_loadu_pd(in_row + c);
                __m256d w = _mm256_loadu_pd(weight + c);
                _mm256_storeu_pd(out_row + c, _mm256_mul_pd(_mm256_mul_pd(x, scale), w));
            }
#endif
        }
        for (; c < cols; ++c) out_row[c] = in_row[c] * inv_rms * weight[c];
    }

    return 0;
}

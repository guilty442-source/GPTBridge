/* transformer_ops_a.c - B94 fragment of transformer.c.
 * Textually included once by transformer.c (single TU): matmul,
 * softmax, rmsnorm entry points. */

/* Shared row-major i-k-j (axpy) kernel: C[M x N] = A[M x K] * B[K x N],
 * restricted to the output column stripe [j0, j1).  Callers validate
 * arguments and the output extent; this kernel only runs the loop.
 * SIMD: axpy with broadcast a_val.  AVX-512: 8×double per instruction
 * (zmm); AVX2: 4×double (ymm). */
static void transformer_matmul_rows_range(
    const double* a, int64_t m, int64_t k,
    const double* b, int64_t n, double* c,
    int64_t j0, int64_t j1,
    gptbridge_simd_level simd) {
    int64_t i, p, j;
    for (i = 0; i < m; ++i) {
        const double* a_row = a + i * k;
        double* c_row = c + i * n;
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
#else
            /* Fallback if AVX-512 headers not available at compile time */
            for (p = 0; p < k; ++p) {
                const double a_val = a_row[p];
                const double* b_row = b + p * n;
                for (j = j0; j < j1; ++j) c_row[j] += a_val * b_row[j];
            }
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
#else
            for (p = 0; p < k; ++p) {
                const double a_val = a_row[p];
                const double* b_row = b + p * n;
                for (j = j0; j < j1; ++j) c_row[j] += a_val * b_row[j];
            }
#endif
        } else {
            for (p = 0; p < k; ++p) {
                const double a_val = a_row[p];
                const double* b_row = b + p * n;
                for (j = j0; j < j1; ++j) c_row[j] += a_val * b_row[j];
            }
        }
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

/* transformer_ops_b.c - B94 fragment of transformer.c.
 * Textually included once by transformer.c (single TU): rope,
 * scaled-dot-product attention, online attention. */

int gptbridge_native_transformer_rope(
    const double* input,
    int64_t batch, int64_t heads, int64_t seq_len, int64_t head_dim,
    const double* cos_table, const double* sin_table,
    double* output) {
    /* input/cos/sin: BORROWED_READONLY; output: CALLER_PROVIDED_OUTPUT. */
    int64_t elems = 0;
    int64_t table_elems = 0;
    int64_t batch_heads = 0;
    int64_t seq_dim = 0;
    int64_t batch_seq = 0;
    gptbridge_native_mem_out_view out;
    const gptbridge_simd_level simd = gptbridge_native_simd_level();
    int64_t b, h, s, i;
    const int64_t half = head_dim / 2;

    if (!gptbridge_native_mem_checked_mul_i64(batch, heads, &batch_heads) ||
        !gptbridge_native_mem_checked_mul_i64(seq_len, head_dim, &seq_dim) ||
        !gptbridge_native_mem_checked_mul_i64(batch_heads, seq_dim, &elems) ||
        !gptbridge_native_mem_checked_mul_i64(batch, seq_len, &batch_seq) ||
        !gptbridge_native_mem_checked_mul_i64(batch_seq, head_dim, &table_elems)) {
        return 1;
    }
    out = gptbridge_native_mem_caller_output(output, elems);
    if (input == NULL || cos_table == NULL || sin_table == NULL ||
        !gptbridge_native_mem_mut_view_valid(out) ||
        batch <= 0 || heads <= 0 || seq_len <= 0 ||
        head_dim <= 0 || (head_dim % 2) != 0) {
        return 1;
    }

    for (b = 0; b < batch; ++b) {
        for (h = 0; h < heads; ++h) {
            for (s = 0; s < seq_len; ++s) {
                const int64_t x_base = ((b * heads + h) * seq_len + s) * head_dim;
                const int64_t t_base = (b * seq_len + s) * head_dim;
                const double* x1 = input + x_base;
                const double* x2 = input + x_base + half;
                const double* c1 = cos_table + t_base;
                const double* s1 = sin_table + t_base;
                double* o1 = output + x_base;
                double* o2 = output + x_base + half;
                i = 0;

                if (simd == GPTBRIDGE_SIMD_AVX512) {
#ifdef GPTBRIDGE_HAVE_AVX512
                    for (; i + 8 <= half; i += 8) {
                        __m512d a = _mm512_loadu_pd(x1 + i);
                        __m512d bvec = _mm512_loadu_pd(x2 + i);
                        __m512d cv = _mm512_loadu_pd(c1 + i);
                        __m512d sv = _mm512_loadu_pd(s1 + i);
                        _mm512_storeu_pd(o1 + i, _mm512_fmsub_pd(a, cv, _mm512_mul_pd(bvec, sv)));
                        _mm512_storeu_pd(o2 + i, _mm512_fmadd_pd(a, sv, _mm512_mul_pd(bvec, cv)));
                    }
#endif
                } else if (simd == GPTBRIDGE_SIMD_AVX2) {
#ifdef GPTBRIDGE_HAVE_AVX2
                    for (; i + 4 <= half; i += 4) {
                        __m256d a = _mm256_loadu_pd(x1 + i);
                        __m256d bvec = _mm256_loadu_pd(x2 + i);
                        __m256d cv = _mm256_loadu_pd(c1 + i);
                        __m256d sv = _mm256_loadu_pd(s1 + i);
#if defined(__FMA__)
                        _mm256_storeu_pd(o1 + i, _mm256_fmsub_pd(a, cv, _mm256_mul_pd(bvec, sv)));
                        _mm256_storeu_pd(o2 + i, _mm256_fmadd_pd(a, sv, _mm256_mul_pd(bvec, cv)));
#else
                        _mm256_storeu_pd(o1 + i, _mm256_sub_pd(_mm256_mul_pd(a, cv), _mm256_mul_pd(bvec, sv)));
                        _mm256_storeu_pd(o2 + i, _mm256_add_pd(_mm256_mul_pd(a, sv), _mm256_mul_pd(bvec, cv)));
#endif
                    }
#endif
                }
                for (; i < half; ++i) {
                    const double a = x1[i];
                    const double bval = x2[i];
                    const double cval = c1[i];
                    const double sval = s1[i];
                    o1[i] = a * cval - bval * sval;
                    o2[i] = a * sval + bval * cval;
                }
            }
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
            score_row[j] = dot_row(q_row, k + j * d_k, d_k, simd) * scale;
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
#ifdef GPTBRIDGE_HAVE_AVX512
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
#ifdef GPTBRIDGE_HAVE_AVX2
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
            axpy_row(out_row, weight_row[j], v + j * d_v, d_v, simd);
        }
    }

    return 0;
}

int gptbridge_native_transformer_attention_online(
    const double* q, int64_t q_rows, int64_t d_k,
    const double* k, int64_t k_rows, int64_t d_k_in,
    const double* v, int64_t v_rows, int64_t d_v,
    int64_t block_k,
    double* output,
    double* block_scores) {
    /* q/k/v: BORROWED_READONLY; output + block_scores workspace:
     * CALLER_PROVIDED_OUTPUT.  block_scores holds ONE K-block
     * (min(block_k, k_rows) doubles) instead of the full
     * q_rows x k_rows scores matrix — W2 online/blocked attention
     * (FlashAttention-style running-max softmax, bounded workspace). */
    int64_t out_elems = 0;
    int64_t blk;
    gptbridge_native_mem_out_view out;
    gptbridge_native_mem_out_view ws;
    gptbridge_simd_level simd;
    double scale;
    int64_t i, j, j0, d;

    if (q == NULL || k == NULL || v == NULL ||
        q_rows <= 0 || d_k <= 0 || k_rows <= 0 || d_v <= 0 ||
        block_k <= 0 || d_k != d_k_in || k_rows != v_rows) {
        return 1;
    }
    blk = block_k < k_rows ? block_k : k_rows;
    if (!gptbridge_native_mem_checked_mul_i64(q_rows, d_v, &out_elems)) {
        return 1;  /* shape overflow → never write */
    }
    out = gptbridge_native_mem_caller_output(output, out_elems);
    ws = gptbridge_native_mem_caller_output(block_scores, blk);
    if (!gptbridge_native_mem_mut_view_valid(out) ||
        !gptbridge_native_mem_mut_view_valid(ws)) {
        return 1;
    }

    scale = 1.0 / sqrt((double)d_k);
    simd = gptbridge_native_simd_level();

    for (i = 0; i < q_rows; ++i) {
        const double* q_row = q + i * d_k;
        double* out_row = output + i * d_v;
        double m_run = 0.0;  /* running max of all scores seen so far */
        double l_run = 0.0;  /* running unnormalized exp-sum */
        int seen = 0;

        for (d = 0; d < d_v; ++d) out_row[d] = 0.0;

        for (j0 = 0; j0 < k_rows; j0 += blk) {
            const int64_t bs = (k_rows - j0 < blk) ? (k_rows - j0) : blk;
            const double* k_blk = k + j0 * d_k;
            const double* v_blk = v + j0 * d_v;
            double bmax, m_new, rescale;

            for (j = 0; j < bs; ++j) {
                block_scores[j] =
                    dot_row(q_row, k_blk + j * d_k, d_k, simd) * scale;
            }

            /* Online softmax merge: m_new = max(m_run, bmax); rescale the
             * accumulator by exp(m_run - m_new) then add
             * exp(s_j - m_new) * V_j for this block. */
            bmax = row_max(block_scores, bs);
            m_new = (seen && m_run > bmax) ? m_run : bmax;
            rescale = seen ? exp(m_run - m_new) : 0.0;
            row_scale(out_row, d_v, rescale);
            l_run = rescale * l_run +
                row_exp_sum(block_scores, bs, m_new, block_scores);
            for (j = 0; j < bs; ++j) {
                axpy_row(out_row, block_scores[j],
                         v_blk + j * d_v, d_v, simd);
            }
            m_run = m_new;
            seen = 1;
        }

        /* l_run >= 1 after the first block (the block-max element
         * contributes exp(0) = 1), so the division is safe. */
        row_scale(out_row, d_v, 1.0 / l_run);
    }

    return 0;
}

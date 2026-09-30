// engine_kernels.h — B94 fragment of engine.cpp (anonymous-namespace compute kernels).
// Included once by engine.cpp inside namespace xingcheng::inference.
#pragma once

namespace {
std::atomic<bool> g_cuda_requested{false};
std::atomic<bool> g_cuda_bf16_requested{false};
std::atomic<bool> g_cuda_fp8_requested{false};
std::atomic<bool> g_cuda_kv_requested{false};

bool env_flag(const char* name) {
    const char* value = std::getenv(name);
    if (value == nullptr) return false;
    const std::string v(value);
    return v == "1" || v == "true" || v == "TRUE" || v == "yes";
}
}  // namespace

std::vector<double> matmul(
    const double* a,
    int64_t m,
    int64_t k,
    const double* b,
    int64_t n) {
    std::vector<double> out(static_cast<size_t>(m * n));
#if defined(XINGCHENG_CUDA)
    if (g_cuda_requested.load()) {
        if (g_cuda_bf16_requested.load()) {
            if (xcuda_matmul_bf16(a, m, k, b, n, out.data()) != 0) {
                throw InferenceError("CUDA_BF16_MATMUL_FAILED");
            }
            return out;
        }
        if (g_cuda_fp8_requested.load()) {
            if (xcuda_matmul_fp8(a, m, k, b, n, out.data()) != 0) {
                throw InferenceError("CUDA_FP8_MATMUL_FAILED");
            }
            return out;
        }
        if (xcuda_matmul_f64(a, m, k, b, n, out.data()) != 0) {
            throw InferenceError("CUDA_MATMUL_FAILED");
        }
        return out;
    }
#else
    if (g_cuda_requested.load()) {
        throw InferenceError("CUDA_UNAVAILABLE");
    }
#endif
    checked_c_call(
        gptbridge_native_transformer_matmul(a, m, k, b, k, n, out.data()),
        "matmul");
    return out;
}

std::vector<double> linear(
    const std::vector<double>& input,
    int64_t rows,
    int64_t in_features,
    const std::vector<double>& transposed_weight,
    int64_t out_features) {
    return matmul(input.data(), rows, in_features, transposed_weight.data(), out_features);
}

// R5 grouped GEMM: a holds the groups' row-blocks concatenated
// ([sum(group_rows) x k]); b_list[g] is group g's [k x n] weight; the
// concatenated [sum x n] outputs come back in group order. One C dispatch
// for the whole group loop; under a requested-CUDA build there is no
// grouped device entry, so the group loop dispatches per group instead.
std::vector<double> matmul_grouped(
    const std::vector<double>& a,
    const std::vector<int64_t>& group_rows,
    const std::vector<const double*>& b_list,
    int64_t k,
    int64_t n) {
    int64_t total_rows = 0;
    for (const int64_t rows : group_rows) total_rows += rows;
    std::vector<double> out(static_cast<size_t>(total_rows * n));
    if (g_cuda_requested.load()) {
#if defined(XINGCHENG_CUDA)
        int64_t a_off = 0;
        int64_t c_off = 0;
        for (size_t g = 0; g < group_rows.size(); ++g) {
            const int64_t m_g = group_rows[g];
            if (m_g == 0) continue;
            const int rc = g_cuda_bf16_requested.load()
                ? xcuda_matmul_bf16(
                      a.data() + a_off, m_g, k, b_list[g], n,
                      out.data() + c_off)
                : g_cuda_fp8_requested.load()
                ? xcuda_matmul_fp8(
                      a.data() + a_off, m_g, k, b_list[g], n,
                      out.data() + c_off)
                : xcuda_matmul_f64(
                      a.data() + a_off, m_g, k, b_list[g], n,
                      out.data() + c_off);
            if (rc != 0) {
                throw InferenceError("CUDA_MATMUL_FAILED");
            }
            a_off += m_g * k;
            c_off += m_g * n;
        }
        return out;
#else
        throw InferenceError("CUDA_UNAVAILABLE");
#endif
    }
    checked_c_call(
        gptbridge_native_transformer_matmul_grouped(
            a.data(), group_rows.data(),
            static_cast<int64_t>(group_rows.size()), b_list.data(), k, n,
            out.data()),
        "matmul-grouped");
    return out;
}

// ── W1 attention kernels ───────────────────────────────────────────────
// Streaming Q·K dot and score·V accumulate used by the attention loop.
// AVX is used when the CPU supports it (runtime-detected once); scalar
// fallback keeps identical semantics on machines without AVX.

bool cpu_has_avx() {
#if XINGCHENG_W1_X64
    static const bool supported = [] {
#if defined(_MSC_VER)
        int regs[4] = {0, 0, 0, 0};
        __cpuidex(regs, 1, 0);
        const bool osxsave = (regs[2] & (1 << 27)) != 0;
        const bool avx = (regs[2] & (1 << 28)) != 0;
        if (!osxsave || !avx) return false;
        return (_xgetbv(0) & 0x6) == 0x6;
#else
        __builtin_cpu_init();
        return __builtin_cpu_supports("avx");
#endif
    }();
    return supported;
#else
    return false;
#endif
}

double dot_f64(const double* a, const double* b, int64_t n) {
#if XINGCHENG_W1_X64
    if (cpu_has_avx()) {
        __m256d acc0 = _mm256_setzero_pd();
        __m256d acc1 = _mm256_setzero_pd();
        int64_t i = 0;
        for (; i + 8 <= n; i += 8) {
            acc0 = _mm256_add_pd(acc0, _mm256_mul_pd(
                _mm256_loadu_pd(a + i), _mm256_loadu_pd(b + i)));
            acc1 = _mm256_add_pd(acc1, _mm256_mul_pd(
                _mm256_loadu_pd(a + i + 4), _mm256_loadu_pd(b + i + 4)));
        }
        acc0 = _mm256_add_pd(acc0, acc1);
        const __m128d pair = _mm_add_pd(
            _mm256_castpd256_pd128(acc0), _mm256_extractf128_pd(acc0, 1));
        double sum = _mm_cvtsd_f64(pair)
            + _mm_cvtsd_f64(_mm_unpackhi_pd(pair, pair));
        for (; i < n; ++i) sum += a[i] * b[i];
        return sum;
    }
#endif
    double sum = 0.0;
    for (int64_t i = 0; i < n; ++i) sum += a[i] * b[i];
    return sum;
}

void axpy_f64(double* out, double weight, const double* v, int64_t n) {
#if XINGCHENG_W1_X64
    if (cpu_has_avx()) {
        const __m256d wv = _mm256_set1_pd(weight);
        int64_t i = 0;
        for (; i + 4 <= n; i += 4) {
            _mm256_storeu_pd(out + i, _mm256_add_pd(
                _mm256_loadu_pd(out + i),
                _mm256_mul_pd(wv, _mm256_loadu_pd(v + i))));
        }
        for (; i < n; ++i) out[i] += weight * v[i];
        return;
    }
#endif
    for (int64_t i = 0; i < n; ++i) out[i] += weight * v[i];
}

// KV INT8 helpers: fp64 operand against packed int8 storage; the caller
// folds the per-token/per-head scale into the weight or the score.
// AVX2 mirrors the fp64 kernels — sign-extend 8 int8 lanes per step.
double dot_int8(const double* a, const int8_t* q, int64_t n) {
#if XINGCHENG_W1_X64
    if (cpu_has_avx()) {
        __m256d acc0 = _mm256_setzero_pd();
        __m256d acc1 = _mm256_setzero_pd();
        int64_t i = 0;
        for (; i + 8 <= n; i += 8) {
            const __m128i v8 = _mm_loadl_epi64(
                reinterpret_cast<const __m128i*>(q + i));
            const __m128i v32 = _mm_cvtepi8_epi32(v8);
            acc0 = _mm256_add_pd(acc0, _mm256_mul_pd(
                _mm256_loadu_pd(a + i), _mm256_cvtepi32_pd(v32)));
            acc1 = _mm256_add_pd(acc1, _mm256_mul_pd(
                _mm256_loadu_pd(a + i + 4),
                _mm256_cvtepi32_pd(_mm_srli_si128(v32, 8))));
        }
        acc0 = _mm256_add_pd(acc0, acc1);
        const __m128d pair = _mm_add_pd(
            _mm256_castpd256_pd128(acc0), _mm256_extractf128_pd(acc0, 1));
        double sum = _mm_cvtsd_f64(pair)
            + _mm_cvtsd_f64(_mm_unpackhi_pd(pair, pair));
        for (; i < n; ++i) sum += a[i] * static_cast<double>(q[i]);
        return sum;
    }
#endif
    double sum = 0.0;
    for (int64_t i = 0; i < n; ++i) sum += a[i] * static_cast<double>(q[i]);
    return sum;
}

void axpy_int8(double* out, double weight, const int8_t* q, int64_t n) {
#if XINGCHENG_W1_X64
    if (cpu_has_avx()) {
        const __m256d wv = _mm256_set1_pd(weight);
        int64_t i = 0;
        for (; i + 8 <= n; i += 8) {
            const __m128i v8 = _mm_loadl_epi64(
                reinterpret_cast<const __m128i*>(q + i));
            const __m128i v32 = _mm_cvtepi8_epi32(v8);
            _mm256_storeu_pd(out + i, _mm256_add_pd(
                _mm256_loadu_pd(out + i),
                _mm256_mul_pd(wv, _mm256_cvtepi32_pd(v32))));
            _mm256_storeu_pd(out + i + 4, _mm256_add_pd(
                _mm256_loadu_pd(out + i + 4),
                _mm256_mul_pd(wv, _mm256_cvtepi32_pd(_mm_srli_si128(v32, 8)))));
        }
        for (; i < n; ++i) out[i] += weight * static_cast<double>(q[i]);
        return;
    }
#endif
    for (int64_t i = 0; i < n; ++i) out[i] += weight * static_cast<double>(q[i]);
}

std::vector<double> rmsnorm(
    const std::vector<double>& input,
    int64_t rows,
    int64_t cols,
    const TensorView& weight,
    double eps) {
    std::vector<double> out(input.size());
    checked_c_call(
        gptbridge_native_transformer_rmsnorm(
            input.data(), rows, cols, weight.data, eps, out.data()),
        "rmsnorm");
    return out;
}

void rope_tables(
    int64_t seq_len,
    int64_t offset,
    int64_t dim,
    double theta,
    std::vector<double>& cos_out,
    std::vector<double>& sin_out) {
    cos_out.assign(static_cast<size_t>(seq_len * dim), 0.0);
    sin_out.assign(static_cast<size_t>(seq_len * dim), 0.0);
    const int64_t half = dim / 2;
    for (int64_t s = 0; s < seq_len; ++s) {
        const double position = static_cast<double>(offset + s);
        for (int64_t i = 0; i < half; ++i) {
            const double exponent = -2.0 * static_cast<double>(i) / static_cast<double>(dim);
            const double angle = position * std::pow(theta, exponent);
            cos_out[static_cast<size_t>(s * dim + i)] = std::cos(angle);
            cos_out[static_cast<size_t>(s * dim + i + half)] = std::cos(angle);
            sin_out[static_cast<size_t>(s * dim + i)] = std::sin(angle);
            sin_out[static_cast<size_t>(s * dim + i + half)] = std::sin(angle);
        }
    }
}

std::string json_escape(const std::string& text) {
    std::string out;
    for (const unsigned char ch : text) {
        switch (ch) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (ch < 0x20) {
                    constexpr char digits[] = "0123456789abcdef";
                    out += "\\u00";
                    out.push_back(digits[ch >> 4]);
                    out.push_back(digits[ch & 0x0F]);
                } else {
                    out.push_back(static_cast<char>(ch));
                }
        }
    }
    return out;
}

// final_logit_softcapping: logits = cap * tanh(logits / cap); the cap
// is a config field (Gemma4 uses 30.0) so a non-positive value is a
// no-op rather than a hard gate.
void logit_softcap(std::vector<double>& logits, double cap) {
    if (cap <= 0.0) return;
    for (double& value : logits) {
        value = cap * std::tanh(value / cap);
    }
}

}  // namespace

// ── Weight blob memory mapping (P3b) ──────────────────────────────────

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

// ── Hybrid CPU+GPU fp64 lane (opt-in: XINGCHENG_HYBRID_MATMUL) ─────────
// Splits one matmul into a CPU row block plus an async GPU row block.
// The split is a deterministic function of (m, env pct): for a fixed
// environment every call maps the same rows to the same lane, and each
// lane keeps its own certified per-element accumulation order — the CPU
// rows are bit-identical to the pure-CPU path, the GPU rows are
// bit-identical to the pure-GPU path.  Read once; the pct env is latched
// so a mid-run env edit can never change the mapping.
int g_hybrid_cpu_pct = -1;  // -1 = not latched yet
bool g_hybrid_matmul_on = false;

bool hybrid_matmul_enabled() {
    if (g_hybrid_cpu_pct < 0) {
        g_hybrid_matmul_on = env_flag("XINGCHENG_HYBRID_MATMUL");
        const char* v = std::getenv("XINGCHENG_HYBRID_CPU_PCT");
        int pct = v != nullptr ? std::atoi(v) : 25;
        if (pct < 0) pct = 0;
        if (pct > 50) pct = 50;
        g_hybrid_cpu_pct = pct;
    }
    return g_hybrid_matmul_on && g_hybrid_cpu_pct > 0;
}
}  // namespace

#if defined(XINGCHENG_CUDA)
// Returns true when `out` is fully written by the hybrid lane.  GPU rows
// [cpu_rows, m) run asynchronously while the CPU register-tiled kernel
// computes rows [0, cpu_rows).  A mid-flight device failure recomputes
// the GPU rows on the CPU — fail-closed, never a partial result.
//
// The split is quantized to the 64-row wide-tile block boundary: the
// GPU GEMM grid is ceil(m_gpu/64) block-rows, so a split that leaves
// m_gpu inside the same block count gains nothing — the CPU share only
// pays when it removes a whole GPU block-row.  gpu_rows therefore floors
// to a multiple of 64 and the CPU absorbs the remainder.
bool matmul_hybrid_f64(
    const double* a, int64_t m, int64_t k,
    const double* b, int64_t n, double* out) {
    int64_t gpu_rows =
        (m >= 8) ? ((m - m * g_hybrid_cpu_pct / 100) / 64) * 64 : 0;
    if (gpu_rows == 0 && m > 64) gpu_rows = 64;  // shed all but one block
    const int64_t cpu_rows = m - gpu_rows;
    if (gpu_rows <= 0 || cpu_rows <= 0) return false;
    if (xcuda_matmul_f64_begin(
            a + cpu_rows * k, gpu_rows, k, b, n) != 0)
        return false;
    checked_c_call(
        gptbridge_native_transformer_matmul(
            a, cpu_rows, k, b, k, n, out),
        "matmul-hybrid-cpu");
    if (xcuda_matmul_f64_wait(out + cpu_rows * n, gpu_rows, n) != 0) {
        checked_c_call(
            gptbridge_native_transformer_matmul(
                a + cpu_rows * k, gpu_rows, k, b, k, n,
                out + cpu_rows * n),
            "matmul-hybrid-repair");
    }
    return true;
}
#endif

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
        if (hybrid_matmul_enabled() &&
            matmul_hybrid_f64(a, m, k, b, n, out.data())) {
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

// Caller-buffered variants: identical computation writing into reusable
// scratch storage — the forward loop keeps one arena alive across layers
// and decode steps instead of a fresh heap block per projection.
void matmul_into(
    const double* a,
    int64_t m,
    int64_t k,
    const double* b,
    int64_t n,
    double* out) {
#if defined(XINGCHENG_CUDA)
    if (g_cuda_requested.load()) {
        if (g_cuda_bf16_requested.load()) {
            if (xcuda_matmul_bf16(a, m, k, b, n, out) != 0) {
                throw InferenceError("CUDA_BF16_MATMUL_FAILED");
            }
            return;
        }
        if (g_cuda_fp8_requested.load()) {
            if (xcuda_matmul_fp8(a, m, k, b, n, out) != 0) {
                throw InferenceError("CUDA_FP8_MATMUL_FAILED");
            }
            return;
        }
        if (hybrid_matmul_enabled() &&
            matmul_hybrid_f64(a, m, k, b, n, out)) {
            return;
        }
        if (xcuda_matmul_f64(a, m, k, b, n, out) != 0) {
            throw InferenceError("CUDA_MATMUL_FAILED");
        }
        return;
    }
#else
    if (g_cuda_requested.load()) {
        throw InferenceError("CUDA_UNAVAILABLE");
    }
#endif
    checked_c_call(
        gptbridge_native_transformer_matmul(a, m, k, b, k, n, out),
        "matmul");
}

void linear_into(
    const std::vector<double>& input,
    int64_t rows,
    int64_t in_features,
    const std::vector<double>& transposed_weight,
    int64_t out_features,
    std::vector<double>& out) {
    out.resize(static_cast<size_t>(rows * out_features));
    matmul_into(
        input.data(), rows, in_features,
        transposed_weight.data(), out_features, out.data());
}

// R5 grouped GEMM: a holds the groups' row-blocks concatenated
// ([sum(group_rows) x k]); b_list[g] is group g's [k x n] weight; the
// concatenated [sum x n] outputs come back in group order. One C dispatch
// for the whole group loop; under CUDA the bf16/fp8 lanes dispatch per
// group (NVRTC entry points take a single weight), while the fp64 lane
// uses xcuda_matmul_f64_grouped — one H2D + per-group Dgemm + one D2H.
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
        if (!g_cuda_bf16_requested.load() &&
            !g_cuda_fp8_requested.load()) {
            if (xcuda_matmul_f64_grouped(
                    a.data(), group_rows.data(),
                    static_cast<long long>(group_rows.size()),
                    b_list.data(), k, n, out.data()) != 0) {
                throw InferenceError("CUDA_MATMUL_FAILED");
            }
            return out;
        }
        int64_t a_off = 0;
        int64_t c_off = 0;
        for (size_t g = 0; g < group_rows.size(); ++g) {
            const int64_t m_g = group_rows[g];
            if (m_g == 0) continue;
            const int rc = g_cuda_bf16_requested.load()
                ? xcuda_matmul_bf16(
                      a.data() + a_off, m_g, k, b_list[g], n,
                      out.data() + c_off)
                : xcuda_matmul_fp8(
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

// Caller-buffered grouped GEMM — same dispatch (grouped fp64 entry /
// per-group bf16+fp8 fallback), writing into reusable scratch instead of
// a fresh vector.
void matmul_grouped_into(
    const std::vector<double>& a,
    const std::vector<int64_t>& group_rows,
    const std::vector<const double*>& b_list,
    int64_t k,
    int64_t n,
    std::vector<double>& out) {
    int64_t total_rows = 0;
    for (const int64_t rows : group_rows) total_rows += rows;
    out.resize(static_cast<size_t>(total_rows * n));
    if (g_cuda_requested.load()) {
#if defined(XINGCHENG_CUDA)
        if (!g_cuda_bf16_requested.load() &&
            !g_cuda_fp8_requested.load()) {
            if (xcuda_matmul_f64_grouped(
                    a.data(), group_rows.data(),
                    static_cast<long long>(group_rows.size()),
                    b_list.data(), k, n, out.data()) != 0) {
                throw InferenceError("CUDA_MATMUL_FAILED");
            }
            return;
        }
        int64_t a_off = 0;
        int64_t c_off = 0;
        for (size_t g = 0; g < group_rows.size(); ++g) {
            const int64_t m_g = group_rows[g];
            if (m_g == 0) continue;
            const int rc = g_cuda_bf16_requested.load()
                ? xcuda_matmul_bf16(
                      a.data() + a_off, m_g, k, b_list[g], n,
                      out.data() + c_off)
                : xcuda_matmul_fp8(
                      a.data() + a_off, m_g, k, b_list[g], n,
                      out.data() + c_off);
            if (rc != 0) {
                throw InferenceError("CUDA_MATMUL_FAILED");
            }
            a_off += m_g * k;
            c_off += m_g * n;
        }
        return;
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

void rmsnorm_into(
    const std::vector<double>& input,
    int64_t rows,
    int64_t cols,
    const TensorView& weight,
    double eps,
    std::vector<double>& out) {
    out.resize(input.size());
    checked_c_call(
        gptbridge_native_transformer_rmsnorm(
            input.data(), rows, cols, weight.data, eps, out.data()),
        "rmsnorm");
}

// RoPE frequency bases: base[i] = pow(theta, -2i/dim) depends only on
// (dim, theta), so it is computed once per model instead of per token —
// every produced angle (position * base[i]) keeps identical operands and
// is bit-identical to the per-call pow() form.
const std::vector<double>& rope_bases(
    int64_t dim, double theta,
    std::vector<double>& cache,
    int64_t& cache_dim,
    double& cache_theta) {
    if (cache_dim != dim || cache_theta != theta ||
        static_cast<int64_t>(cache.size()) != dim / 2) {
        cache.assign(static_cast<size_t>(dim / 2), 0.0);
        for (int64_t i = 0; i < dim / 2; ++i) {
            const double exponent =
                -2.0 * static_cast<double>(i) / static_cast<double>(dim);
            cache[static_cast<size_t>(i)] = std::pow(theta, exponent);
        }
        cache_dim = dim;
        cache_theta = theta;
    }
    return cache;
}

void rope_tables(
    int64_t seq_len,
    int64_t offset,
    int64_t dim,
    const std::vector<double>& bases,
    std::vector<double>& cos_out,
    std::vector<double>& sin_out) {
    cos_out.assign(static_cast<size_t>(seq_len * dim), 0.0);
    sin_out.assign(static_cast<size_t>(seq_len * dim), 0.0);
    const int64_t half = dim / 2;
    for (int64_t s = 0; s < seq_len; ++s) {
        const double position = static_cast<double>(offset + s);
        for (int64_t i = 0; i < half; ++i) {
            const double angle = position * bases[static_cast<size_t>(i)];
            cos_out[static_cast<size_t>(s * dim + i)] = std::cos(angle);
            cos_out[static_cast<size_t>(s * dim + i + half)] = std::cos(angle);
            sin_out[static_cast<size_t>(s * dim + i)] = std::sin(angle);
            sin_out[static_cast<size_t>(s * dim + i + half)] = std::sin(angle);
        }
    }
}

// v27 fused-hybrid rope — trainer contract (xct_math.h rope_hf_partial /
// rope).  Two pairings coexist: partial rotary rotates channel i against
// i + rd/2 within the first rd channels (channels >= rd pass through);
// full rotary pairs (2i, 2i+1) interleaved.  Both differ from the legacy
// C-ABI rotate-half kernel, so fused bundles apply them here in the
// engine layer — same math, same operand order as the trainer.
// v29 Qwen3-Coder YaRN — per-channel blend of raw and
// factor-interpolated inv-freqs (fp64 twin of the trainer's
// xct_math.h yarn_blend_i). Returns a yarn-adjusted copy of `bases`.
inline std::vector<double> yarn_adjust_bases(
    const std::vector<double>& bases, int64_t dim, double theta,
    double factor, int64_t orig_pos,
    double beta_fast, double beta_slow) {
    const int64_t half = dim / 2;
    const double logb = std::log(theta);
    auto corr = [&](double beta) {
        return static_cast<double>(dim) *
               std::log(static_cast<double>(orig_pos) /
                        (beta * 6.283185307179586)) /
               (2.0 * logb);
    };
    const double lo = std::max(0.0, std::floor(corr(beta_fast)));
    double hi =
        std::min(static_cast<double>(half - 1), std::ceil(corr(beta_slow)));
    if (hi == lo) hi = lo + 1e-3;
    std::vector<double> out = bases;
    for (int64_t i = 0; i < half; ++i) {
        const double ext =
            1.0 - std::min(1.0,
                           std::max(0.0, (static_cast<double>(i) - lo) /
                                             (hi - lo)));
        out[static_cast<size_t>(i)] *= ext + (1.0 - ext) / factor;
    }
    return out;
}

// YaRN attention-factor mscale (fp64 twin of xct_math.h yarn_mscale);
// applied to the cos/sin operands so the rotated channels scale.
inline double yarn_mscale_d(double factor, double attn_factor) {
    return attn_factor > 0.0 ? attn_factor
                             : 0.1 * std::log(factor) + 1.0;
}

void rope_partial_rows(
    double* v, int64_t heads, int64_t seq, int64_t head_dim, int64_t rd,
    int64_t offset, const std::vector<double>& bases,
    double mscale = 1.0) {
    const int64_t half = rd / 2;
    for (int64_t s = 0; s < seq; ++s) {
        const double position = static_cast<double>(offset + s);
        for (int64_t h = 0; h < heads; ++h) {
            double* row = v + static_cast<size_t>((h * seq + s) * head_dim);
            for (int64_t i = 0; i < half; ++i) {
                const double angle = position * bases[static_cast<size_t>(i)];
                const double c = std::cos(angle) * mscale;
                const double sn = std::sin(angle) * mscale;
                const double a = row[i];
                const double b = row[i + half];
                row[i] = a * c - b * sn;
                row[i + half] = a * sn + b * c;
            }
        }
    }
}

void rope_interleaved_rows(
    double* v, int64_t heads, int64_t seq, int64_t head_dim,
    int64_t offset, const std::vector<double>& bases,
    double mscale = 1.0) {
    const int64_t half = head_dim / 2;
    for (int64_t s = 0; s < seq; ++s) {
        const double position = static_cast<double>(offset + s);
        for (int64_t h = 0; h < heads; ++h) {
            double* row = v + static_cast<size_t>((h * seq + s) * head_dim);
            for (int64_t i = 0; i < half; ++i) {
                const double angle = position * bases[static_cast<size_t>(i)];
                const double c = std::cos(angle) * mscale;
                const double sn = std::sin(angle) * mscale;
                const double a = row[2 * i];
                const double b = row[2 * i + 1];
                row[2 * i] = a * c - b * sn;
                row[2 * i + 1] = a * sn + b * c;
            }
        }
    }
}

// v27 scalar helpers — fp64 twins of the trainer's fp32 primitives
// (identical formulas; lane order unchanged).
double sigmoid_d(double x) { return 1.0 / (1.0 + std::exp(-x)); }
double silu_d(double x) { return x / (1.0 + std::exp(-x)); }
double softplus_d(double x) {
    return x > 20.0 ? x : std::log1p(std::exp(x));
}

// Span-scoped variant for the packed [T, vh, dim] deltanet buffers:
// normalizes the seq*vh rows belonging to one span (rows are indexed
// (row * vh + h) with row relative to the packed batch).
void l2norm_span_rows(std::vector<double>& v, int64_t base_row,
                      int64_t seq, int64_t heads_per_row, int64_t dim,
                      double eps) {
    const int64_t row0 = base_row * heads_per_row;
    for (int64_t r = 0; r < seq * heads_per_row; ++r) {
        double* row = v.data() + static_cast<size_t>((row0 + r) * dim);
        double ss = 0.0;
        for (int64_t i = 0; i < dim; ++i) ss += row[i] * row[i];
        const double inv = 1.0 / std::sqrt(ss + eps);
        for (int64_t i = 0; i < dim; ++i) row[i] *= inv;
    }
}

// In-place L2 row normalization on a [rows x dim] flat buffer
// (deltanet q/k, eps folded into the length like the trainer).
void l2norm_rows(std::vector<double>& v, int64_t rows, int64_t dim,
                 double eps) {
    for (int64_t r = 0; r < rows; ++r) {
        double* row = v.data() + static_cast<size_t>(r * dim);
        double ss = 0.0;
        for (int64_t i = 0; i < dim; ++i) ss += row[i] * row[i];
        const double inv = 1.0 / std::sqrt(ss + eps);
        for (int64_t i = 0; i < dim; ++i) row[i] *= inv;
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

// Micro-benchmark: current per-element tpu_linear vs register-tiled variant.
// tile4 keeps per-row dual accumulators matching dot_ref's exact split and
// reduction order, so each output element accumulates identically (bitwise).
#include <immintrin.h>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <random>
#include <algorithm>

static float dot_ref(const float* a, const float* b, int64_t n) {
    __m256 s0 = _mm256_setzero_ps(), s1 = _mm256_setzero_ps();
    int64_t i = 0;
    for (; i + 16 <= n; i += 16) {
        s0 = _mm256_fmadd_ps(_mm256_loadu_ps(a + i), _mm256_loadu_ps(b + i), s0);
        s1 = _mm256_fmadd_ps(_mm256_loadu_ps(a + i + 8), _mm256_loadu_ps(b + i + 8), s1);
    }
    for (; i + 8 <= n; i += 8)
        s0 = _mm256_fmadd_ps(_mm256_loadu_ps(a + i), _mm256_loadu_ps(b + i), s0);
    s0 = _mm256_add_ps(s0, s1);
    __m128 lo = _mm256_castps256_ps128(s0);
    __m128 hi = _mm256_extractf128_ps(s0, 1);
    lo = _mm_add_ps(lo, hi);
    lo = _mm_add_ps(lo, _mm_movehl_ps(lo, lo));
    lo = _mm_add_ss(lo, _mm_shuffle_ps(lo, lo, 1));
    float sum = _mm_cvtss_f32(lo);
    for (; i < n; ++i) sum += a[i] * b[i];
    return sum;
}

static float reduce8(__m256 v) {
    __m128 lo = _mm256_castps256_ps128(v);
    __m128 hi = _mm256_extractf128_ps(v, 1);
    lo = _mm_add_ps(lo, hi);
    lo = _mm_add_ps(lo, _mm_movehl_ps(lo, lo));
    lo = _mm_add_ss(lo, _mm_shuffle_ps(lo, lo, 1));
    return _mm_cvtss_f32(lo);
}

// baseline: one output element per dot, W re-streamed per token row
static void lin_ref(const float* x, const float* w, float* y, int T, int I, int O) {
    for (int t = 0; t < T; ++t)
        for (int o = 0; o < O; ++o)
            y[(size_t)t * O + o] = dot_ref(x + (size_t)t * I, w + (size_t)o * I, I);
}

// tile4: 4 token rows share one W stream. Per output element the i-loop
// mirrors dot_ref exactly (s0 on i, s1 on i+8, same tails) -> bitwise equal.
static void lin_tile4(const float* x, const float* w, float* y, int T, int I, int O) {
    for (int t0 = 0; t0 < T; t0 += 4) {
        int tb = std::min(4, T - t0);
        for (int o = 0; o < O; ++o) {
            const float* wr = w + (size_t)o * I;
            __m256 a0 = _mm256_setzero_ps(), a1 = _mm256_setzero_ps(),
                   b0 = _mm256_setzero_ps(), b1 = _mm256_setzero_ps(),
                   c0 = _mm256_setzero_ps(), c1 = _mm256_setzero_ps(),
                   d0 = _mm256_setzero_ps(), d1 = _mm256_setzero_ps();
            const float* x0 = x + (size_t)t0 * I;
            const float* x1 = tb > 1 ? x0 + I : x0;
            const float* x2 = tb > 2 ? x0 + 2 * I : x0;
            const float* x3 = tb > 3 ? x0 + 3 * I : x0;
            int64_t i = 0;
            for (; i + 16 <= I; i += 16) {
                __m256 wv0 = _mm256_loadu_ps(wr + i);
                __m256 wv1 = _mm256_loadu_ps(wr + i + 8);
                a0 = _mm256_fmadd_ps(_mm256_loadu_ps(x0 + i), wv0, a0);
                a1 = _mm256_fmadd_ps(_mm256_loadu_ps(x0 + i + 8), wv1, a1);
                b0 = _mm256_fmadd_ps(_mm256_loadu_ps(x1 + i), wv0, b0);
                b1 = _mm256_fmadd_ps(_mm256_loadu_ps(x1 + i + 8), wv1, b1);
                c0 = _mm256_fmadd_ps(_mm256_loadu_ps(x2 + i), wv0, c0);
                c1 = _mm256_fmadd_ps(_mm256_loadu_ps(x2 + i + 8), wv1, c1);
                d0 = _mm256_fmadd_ps(_mm256_loadu_ps(x3 + i), wv0, d0);
                d1 = _mm256_fmadd_ps(_mm256_loadu_ps(x3 + i + 8), wv1, d1);
            }
            for (; i + 8 <= I; i += 8) {
                __m256 wv = _mm256_loadu_ps(wr + i);
                a0 = _mm256_fmadd_ps(_mm256_loadu_ps(x0 + i), wv, a0);
                b0 = _mm256_fmadd_ps(_mm256_loadu_ps(x1 + i), wv, b0);
                c0 = _mm256_fmadd_ps(_mm256_loadu_ps(x2 + i), wv, c0);
                d0 = _mm256_fmadd_ps(_mm256_loadu_ps(x3 + i), wv, d0);
            }
            float acc[4] = {reduce8(_mm256_add_ps(a0, a1)),
                            reduce8(_mm256_add_ps(b0, b1)),
                            reduce8(_mm256_add_ps(c0, c1)),
                            reduce8(_mm256_add_ps(d0, d1))};
            for (; i < I; ++i) {
                acc[0] += x0[i] * wr[i]; acc[1] += x1[i] * wr[i];
                acc[2] += x2[i] * wr[i]; acc[3] += x3[i] * wr[i];
            }
            for (int k = 0; k < tb; ++k)
                y[(size_t)(t0 + k) * O + o] = acc[k];
        }
    }
}

template <typename F>
static double bench(F&& f, int reps) {
    auto t0 = std::chrono::steady_clock::now();
    for (int r = 0; r < reps; ++r) f();
    auto t1 = std::chrono::steady_clock::now();
    return std::chrono::duration<double>(t1 - t0).count() / reps;
}

int main() {
    const struct { int T, I, O; } shapes[] = {
        {64, 768, 768}, {64, 768, 2048}, {64, 2048, 768},
        {32, 768, 1024}, {8, 768, 1024}, {512, 768, 8192},
    };
    std::mt19937 rng(42);
    std::normal_distribution<float> nd(0.f, 0.02f);
    for (auto& s : shapes) {
        std::vector<float> x((size_t)s.T * s.I), w((size_t)s.O * s.I),
                           y0((size_t)s.T * s.O), y1((size_t)s.T * s.O);
        for (auto& v : x) v = nd(rng);
        for (auto& v : w) v = nd(rng);
        int reps = std::max(1, (int)(3e8 / ((double)s.T * s.I * s.O * 2)));
        double tref = bench([&] { lin_ref(x.data(), w.data(), y0.data(), s.T, s.I, s.O); }, reps);
        double tt4  = bench([&] { lin_tile4(x.data(), w.data(), y1.data(), s.T, s.I, s.O); }, reps);
        double gf = (double)s.T * s.I * s.O * 2 / 1e9;
        bool same = std::memcmp(y0.data(), y1.data(), y0.size() * 4) == 0;
        std::printf("T=%d I=%d O=%d  ref %.3fms %.1fGF/s  tile4 %.3fms %.1fGF/s  speedup %.2fx  bitwise=%s\n",
                    s.T, s.I, s.O, tref * 1e3, gf / tref, tt4 * 1e3, gf / tt4,
                    tref / tt4, same ? "YES" : "NO");
    }
    return 0;
}

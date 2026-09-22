// Suite: ACC-1 CPU SIMD acceptance — pure-C transformer/vector cores.
//
// For every dispatched kernel an independent scalar reference (in this file)
// defines the semantic contract; the dispatched result must match within
// tolerance.  Run under GPTBRIDGE_SIMD_LEVEL=none|avx2|avx512 to build the
// cross-level equivalence matrix — the env cap may only LOWER the level, so
// a "none" run is also the no-AVX fallback proof.
//
// `--bench <out.json>` mode times each kernel at fixed shapes and writes
// per-kernel ns totals for reproducible speedup evidence.
#include "harness.hpp"

extern "C" {
#include "transformer.h"
#include "vector.h"
}

#include <cmath>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace {

const char* SUITE = "SIMD_KERNELS_SUITE";
const double TOL = 1e-9;  // fp64 reassociation headroom; contract gate is 1e-3

uint64_t rng_state = 0x9e3779b97f4a7c15ull;

double rng_next() {
    rng_state = rng_state * 6364136223846793005ull + 1442695040888963407ull;
    return ((double)(rng_state >> 11) / (double)(1ull << 53)) * 2.0 - 1.0;
}

void fill(std::vector<double>& v) {
    for (double& x : v) x = rng_next();
}

bool near(double a, double b) {
    return std::fabs(a - b) <= TOL + TOL * std::fabs(b);
}

double max_abs_diff(const std::vector<double>& x, const std::vector<double>& y) {
    double worst = 0.0;
    for (std::size_t i = 0; i < x.size() && i < y.size(); ++i) {
        const double d = std::fabs(x[i] - y[i]);
        if (d > worst) worst = d;
    }
    return worst;
}

// ---------- independent scalar references ----------

void ref_matmul(const double* a, int64_t m, int64_t k,
                const double* b, int64_t n, double* c) {
    for (int64_t i = 0; i < m; ++i)
        for (int64_t j = 0; j < n; ++j) {
            double s = 0.0;
            for (int64_t p = 0; p < k; ++p) s += a[i * k + p] * b[p * n + j];
            c[i * n + j] = s;
        }
}

void ref_softmax(const double* in, int64_t rows, int64_t cols, double* out) {
    for (int64_t r = 0; r < rows; ++r) {
        const double* row = in + r * cols;
        double* orow = out + r * cols;
        double mx = row[0];
        for (int64_t c = 1; c < cols; ++c) if (row[c] > mx) mx = row[c];
        double sum = 0.0;
        for (int64_t c = 0; c < cols; ++c) {
            orow[c] = std::exp(row[c] - mx);
            sum += orow[c];
        }
        const double inv = 1.0 / sum;
        for (int64_t c = 0; c < cols; ++c) orow[c] *= inv;
    }
}

void ref_rmsnorm(const double* in, int64_t rows, int64_t cols,
                 const double* w, double eps, double* out) {
    for (int64_t r = 0; r < rows; ++r) {
        double sq = 0.0;
        for (int64_t c = 0; c < cols; ++c) sq += in[r * cols + c] * in[r * cols + c];
        const double inv = 1.0 / std::sqrt(sq / (double)cols + eps);
        for (int64_t c = 0; c < cols; ++c) out[r * cols + c] = in[r * cols + c] * inv * w[c];
    }
}

void ref_rope(const double* in, int64_t batch, int64_t heads, int64_t seq,
              int64_t hd, const double* ct, const double* st, double* out) {
    const int64_t half = hd / 2;
    for (int64_t b = 0; b < batch; ++b)
        for (int64_t h = 0; h < heads; ++h)
            for (int64_t s = 0; s < seq; ++s) {
                const int64_t xb = ((b * heads + h) * seq + s) * hd;
                const int64_t tb = (b * seq + s) * hd;
                for (int64_t i = 0; i < half; ++i) {
                    const double a = in[xb + i], bv = in[xb + half + i];
                    const double cv = ct[tb + i], sv = st[tb + i];
                    out[xb + i] = a * cv - bv * sv;
                    out[xb + half + i] = a * sv + bv * cv;
                }
            }
}

void ref_sdpa(const double* q, int64_t qr, int64_t dk,
              const double* k, int64_t kr,
              const double* v, int64_t dv,
              double* out) {
    std::vector<double> scores((std::size_t)qr * kr);
    const double scale = 1.0 / std::sqrt((double)dk);
    for (int64_t i = 0; i < qr; ++i)
        for (int64_t j = 0; j < kr; ++j) {
            double s = 0.0;
            for (int64_t d = 0; d < dk; ++d) s += q[i * dk + d] * k[j * dk + d];
            scores[i * kr + j] = s * scale;
        }
    ref_softmax(scores.data(), qr, kr, scores.data());
    for (int64_t i = 0; i < qr; ++i)
        for (int64_t d = 0; d < dv; ++d) {
            double s = 0.0;
            for (int64_t j = 0; j < kr; ++j) s += scores[i * kr + j] * v[j * dv + d];
            out[i * dv + d] = s;
        }
}

double ref_dot(const double* a, const double* b, int64_t n) {
    double s = 0.0;
    for (int64_t i = 0; i < n; ++i) s += a[i] * b[i];
    return s;
}

const char* level_name(int level) {
    switch (level) {
        case 2: return "avx512";
        case 1: return "avx2";
        default: return "none";
    }
}

// ---------- benchmark mode ----------

volatile double bench_sink = 0.0;

template <typename F>
double bench_ms(F&& fn, int reps) {
    const auto t0 = std::chrono::steady_clock::now();
    for (int i = 0; i < reps; ++i) fn();
    const auto t1 = std::chrono::steady_clock::now();
    return std::chrono::duration<double, std::milli>(t1 - t0).count();
}

int run_bench(const char* out_path) {
    std::vector<double> a(64 * 192), b(64 * 96), bT(192 * 96), c(64 * 96);
    fill(a); fill(bT);
    // matmul wants b[k x n] row-major = transpose of bT
    for (int64_t p = 0; p < 192; ++p)
        for (int64_t j = 0; j < 96; ++j) b[p * 96 + j] = bT[j * 192 + p];

    std::vector<double> sm_in(64 * 257), sm_out(64 * 257);
    fill(sm_in);
    std::vector<double> rn_in(64 * 255), rn_w(255), rn_out(64 * 255);
    fill(rn_in); fill(rn_w);
    std::vector<double> rp_in(2 * 8 * 64 * 64), rp_ct(2 * 64 * 64), rp_st(2 * 64 * 64), rp_out(2 * 8 * 64 * 64);
    fill(rp_in); fill(rp_ct); fill(rp_st);
    std::vector<double> q(32 * 64), kk(48 * 64), v(48 * 64), sdpa_out(32 * 64), scores(32 * 48);
    fill(q); fill(kk); fill(v);
    std::vector<double> va(8191), vb(8191);
    fill(va); fill(vb);
    const int64_t bd_n = 16, bd_dim = 1025;
    std::vector<std::vector<double>> ba(bd_n, std::vector<double>(bd_dim)),
        bb(bd_n, std::vector<double>(bd_dim));
    std::vector<const double*> ba_p(bd_n), bb_p(bd_n);
    std::vector<double> bd_out(bd_n);
    for (int64_t i = 0; i < bd_n; ++i) {
        fill(ba[i]); fill(bb[i]); ba_p[i] = ba[i].data(); bb_p[i] = bb[i].data();
    }

    struct Entry { const char* name; int reps; double ms; };
    std::vector<Entry> entries;
    entries.push_back({"matmul_64x192x96", 200,
        bench_ms([&]() { gptbridge_native_transformer_matmul(a.data(), 64, 192, b.data(), 192, 96, c.data()); bench_sink += c[0]; }, 200)});
    entries.push_back({"softmax_64x257", 2000,
        bench_ms([&]() { gptbridge_native_transformer_softmax(sm_in.data(), 64, 257, sm_out.data()); bench_sink += sm_out[0]; }, 2000)});
    entries.push_back({"rmsnorm_64x255", 2000,
        bench_ms([&]() { gptbridge_native_transformer_rmsnorm(rn_in.data(), 64, 255, rn_w.data(), 1e-5, rn_out.data()); bench_sink += rn_out[0]; }, 2000)});
    entries.push_back({"rope_2x8x64x64", 500,
        bench_ms([&]() { gptbridge_native_transformer_rope(rp_in.data(), 2, 8, 64, 64, rp_ct.data(), rp_st.data(), rp_out.data()); bench_sink += rp_out[0]; }, 500)});
    entries.push_back({"sdpa_32x48x64x64", 300,
        bench_ms([&]() { gptbridge_native_transformer_scaled_dot_product_attention(q.data(), 32, 64, kk.data(), 48, 64, v.data(), 48, 64, sdpa_out.data(), scores.data()); bench_sink += sdpa_out[0]; }, 300)});
    entries.push_back({"vector_dot_8191", 2000,
        bench_ms([&]() { bench_sink += gptbridge_native_vector_dot(va.data(), vb.data(), 8191); }, 2000)});
    entries.push_back({"vector_batch_dot_16x1025", 500,
        bench_ms([&]() { gptbridge_native_vector_batch_dot(ba_p.data(), bb_p.data(), bd_n, bd_dim, bd_out.data()); bench_sink += bd_out[0]; }, 500)});

    const int level = gptbridge_native_simd_effective_level();
    FILE* out = std::fopen(out_path, "w");
    if (!out) out = stdout;
    std::fprintf(out, "{\"schema\":\"acc1-simd-bench/v1\",\"level\":\"%s\",\"level_code\":%d,\"kernels\":[",
                 level_name(level), level);
    for (std::size_t i = 0; i < entries.size(); ++i) {
        const Entry& e = entries[i];
        std::fprintf(out, "%s{\"name\":\"%s\",\"reps\":%d,\"ms_total\":%.3f,\"us_per_op\":%.3f}",
                     i ? "," : "", e.name, e.reps, e.ms, e.ms * 1000.0 / e.reps);
    }
    std::fprintf(out, "],\"sink\":%.6f}", bench_sink);
    if (out != stdout) std::fclose(out);
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    for (int i = 1; i < argc; ++i) {
        if (!std::strcmp(argv[i], "--bench")) {
            const char* path = (i + 1 < argc) ? argv[i + 1] : "simd_bench.json";
            return run_bench(path);
        }
    }

    NT_SUITE(SUITE);
    rng_state = 0x9e3779b97f4a7c15ull;

    NT_TEST(SUITE, "effective_level_respects_env_cap") {
        const int level = gptbridge_native_simd_effective_level();
        const char* forced = std::getenv("GPTBRIDGE_SIMD_LEVEL");
        NT_CHECK(level >= 0 && level <= 2, "level out of range");
        if (forced && !std::strcmp(forced, "none")) {
            NT_CHECK(level == 0, "none cap must force scalar");
        } else if (forced && !std::strcmp(forced, "avx2")) {
            NT_CHECK(level <= 1, "avx2 cap must not exceed AVX2");
        }
    }
    NT_END_TEST(SUITE, "effective_level_respects_env_cap");

    NT_TEST(SUITE, "matmul_matches_scalar_reference") {
        const int64_t shapes[][3] = {{4, 8, 6}, {13, 64, 37}, {32, 96, 48}};
        for (const auto& s : shapes) {
            std::vector<double> a(s[0] * s[1]), b(s[1] * s[2]);
            fill(a); fill(b);
            std::vector<double> got(s[0] * s[2], 0.0), want(s[0] * s[2], 0.0);
            NT_CHECK(gptbridge_native_transformer_matmul(a.data(), s[0], s[1], b.data(), s[1], s[2], got.data()) == 0,
                     "matmul rc");
            ref_matmul(a.data(), s[0], s[1], b.data(), s[2], want.data());
            NT_CHECK_NEAR(0.0, max_abs_diff(got, want), 1e-8, "matmul parity");
        }
    }
    NT_END_TEST(SUITE, "matmul_matches_scalar_reference");

    NT_TEST(SUITE, "softmax_matches_scalar_reference") {
        const int64_t shapes[][2] = {{3, 17}, {16, 64}, {8, 255}};
        for (const auto& s : shapes) {
            std::vector<double> in(s[0] * s[1]);
            fill(in);
            std::vector<double> got(s[0] * s[1], 0.0), want(s[0] * s[1], 0.0);
            NT_CHECK(gptbridge_native_transformer_softmax(in.data(), s[0], s[1], got.data()) == 0, "softmax rc");
            ref_softmax(in.data(), s[0], s[1], want.data());
            NT_CHECK_NEAR(0.0, max_abs_diff(got, want), 1e-12, "softmax parity");
            for (int64_t r = 0; r < s[0]; ++r) {
                double sum = 0.0;
                for (int64_t cix = 0; cix < s[1]; ++cix) sum += got[r * s[1] + cix];
                NT_CHECK_NEAR(sum, 1.0, 1e-12, "softmax row sum");
            }
        }
    }
    NT_END_TEST(SUITE, "softmax_matches_scalar_reference");

    NT_TEST(SUITE, "rmsnorm_matches_scalar_reference") {
        const int64_t shapes[][2] = {{4, 32}, {9, 129}};
        for (const auto& s : shapes) {
            std::vector<double> in(s[0] * s[1]), w(s[1]);
            fill(in); fill(w);
            std::vector<double> got(s[0] * s[1], 0.0), want(s[0] * s[1], 0.0);
            NT_CHECK(gptbridge_native_transformer_rmsnorm(in.data(), s[0], s[1], w.data(), 1e-5, got.data()) == 0,
                     "rmsnorm rc");
            ref_rmsnorm(in.data(), s[0], s[1], w.data(), 1e-5, want.data());
            NT_CHECK_NEAR(0.0, max_abs_diff(got, want), 1e-9, "rmsnorm parity");
        }
    }
    NT_END_TEST(SUITE, "rmsnorm_matches_scalar_reference");

    NT_TEST(SUITE, "rope_matches_scalar_reference") {
        const int64_t cfgs[][4] = {{1, 2, 9, 16}, {2, 3, 5, 34}};
        for (const auto& s : cfgs) {
            const int64_t elems = s[0] * s[1] * s[2] * s[3];
            const int64_t telems = s[0] * s[2] * s[3];
            std::vector<double> in(elems), ct(telems), st(telems);
            fill(in); fill(ct); fill(st);
            std::vector<double> got(elems, 0.0), want(elems, 0.0);
            NT_CHECK(gptbridge_native_transformer_rope(in.data(), s[0], s[1], s[2], s[3], ct.data(), st.data(), got.data()) == 0,
                     "rope rc");
            ref_rope(in.data(), s[0], s[1], s[2], s[3], ct.data(), st.data(), want.data());
            NT_CHECK_NEAR(0.0, max_abs_diff(got, want), 1e-12, "rope parity");
        }
    }
    NT_END_TEST(SUITE, "rope_matches_scalar_reference");

    NT_TEST(SUITE, "sdpa_matches_scalar_reference") {
        const int64_t cfgs[][4] = {{4, 16, 11, 24}, {9, 37, 13, 29}};
        for (const auto& s : cfgs) {
            std::vector<double> q(s[0] * s[1]), k(s[2] * s[1]), v(s[2] * s[3]);
            fill(q); fill(k); fill(v);
            std::vector<double> got(s[0] * s[3], 0.0), want(s[0] * s[3], 0.0), scratch(s[0] * s[2]);
            NT_CHECK(gptbridge_native_transformer_scaled_dot_product_attention(
                         q.data(), s[0], s[1], k.data(), s[2], s[1], v.data(), s[2], s[3],
                         got.data(), scratch.data()) == 0,
                     "sdpa rc");
            ref_sdpa(q.data(), s[0], s[1], k.data(), s[2], v.data(), s[3], want.data());
            NT_CHECK_NEAR(0.0, max_abs_diff(got, want), 1e-9, "sdpa parity");
        }
    }
    NT_END_TEST(SUITE, "sdpa_matches_scalar_reference");

    NT_TEST(SUITE, "vector_ops_match_scalar_reference") {
        const int64_t dims[] = {5, 64, 1027};
        for (int64_t dim : dims) {
            std::vector<double> a(dim), b(dim);
            fill(a); fill(b);
            NT_CHECK_NEAR(gptbridge_native_vector_dot(a.data(), b.data(), dim),
                          ref_dot(a.data(), b.data(), dim), 1e-9 * dim, "dot parity");
            double ref_l2 = std::sqrt(ref_dot(a.data(), a.data(), dim));
            NT_CHECK_NEAR(gptbridge_native_vector_l2_norm(a.data(), dim), ref_l2, 1e-9 * dim, "l2 parity");
            const double ref_cos = ref_dot(a.data(), b.data(), dim) /
                (std::sqrt(ref_dot(a.data(), a.data(), dim)) * std::sqrt(ref_dot(b.data(), b.data(), dim)));
            NT_CHECK_NEAR(gptbridge_native_vector_cosine_similarity(a.data(), b.data(), dim), ref_cos, 1e-9, "cosine parity");
        }
        const int64_t n = 7, dim = 33;
        std::vector<std::vector<double>> ba(n, std::vector<double>(dim)), bb(n, std::vector<double>(dim));
        std::vector<const double*> ap(n), bp(n);
        std::vector<double> results(n, 0.0);
        for (int64_t i = 0; i < n; ++i) { fill(ba[i]); fill(bb[i]); ap[i] = ba[i].data(); bp[i] = bb[i].data(); }
        NT_CHECK(gptbridge_native_vector_batch_dot(ap.data(), bp.data(), n, dim, results.data()) == 0, "batch_dot rc");
        for (int64_t i = 0; i < n; ++i)
            NT_CHECK_NEAR(results[i], ref_dot(ba[i].data(), bb[i].data(), dim), 1e-9, "batch_dot parity");
    }
    NT_END_TEST(SUITE, "vector_ops_match_scalar_reference");

    NT_TEST(SUITE, "invalid_arguments_fail_closed") {
        double buf[4] = {0, 0, 0, 0};
        NT_CHECK(gptbridge_native_transformer_matmul(nullptr, 2, 2, buf, 2, 2, buf) != 0, "matmul null a");
        NT_CHECK(gptbridge_native_transformer_matmul(buf, 2, 3, buf, 2, 2, buf) != 0, "matmul k mismatch");
        NT_CHECK(gptbridge_native_transformer_softmax(buf, 0, 2, buf) != 0, "softmax zero rows");
        NT_CHECK(gptbridge_native_transformer_rmsnorm(buf, 1, 2, nullptr, 1e-5, buf) != 0, "rmsnorm null weight");
        NT_CHECK(gptbridge_native_transformer_rope(buf, 1, 1, 1, 3, buf, buf, buf) != 0, "rope odd head_dim");
        NT_CHECK(gptbridge_native_transformer_scaled_dot_product_attention(
                     buf, 1, 4, buf, 2, 8, buf, 2, 4, buf, buf) != 0, "sdpa dk mismatch");
        NT_CHECK(gptbridge_native_vector_batch_dot(nullptr, nullptr, 1, 4, buf) != 0, "batch_dot null");
    }
    NT_END_TEST(SUITE, "invalid_arguments_fail_closed");

    return native_tests::report("simd_kernels_suite.json");
}

// xcm_computeplane.h — unified measured compute-plane report.
// Included once by xc_modeltool.cpp after xcm_rtgates.h (reuses its
// extern "C" CUDA decls and the cpup_* helpers).
//
//   compute-plane   measured throughput of every governed lane on one
//                   shape grid: CPU register-tiled fp64 GEMM + GEMV, RAM
//                   bandwidth, device GEMM (nvcuda Driver API), and the
//                   hybrid CPU+GPU split lane.  Pure measurement — a
//                   dispatch or precision profile is never enabled by
//                   probing (§27 last rule); the report is evidence for
//                   the governed admission/dispatch policy, not a gate.
#pragma once

extern "C" int xcuda_matmul_f64_begin(const double*, long long, long long,
                                      const double*, long long);
extern "C" int xcuda_matmul_f64_wait(double*, long long, long long);
extern "C" int xcuda_sm_count();
extern "C" int gptbridge_native_transformer_matmul(
    const double*, int64_t, int64_t, const double*, int64_t, int64_t,
    double*);
extern "C" int gptbridge_native_simd_effective_level();

namespace {

// Best-of-reps wall time for a callable producing no side effects we
// need; the lanes themselves are the measured artifact.
template <typename Fn>
double cp_best_s(Fn&& fn, int reps) {
    double best = 1e30;
    for (int r = 0; r < reps; ++r) {
        const auto t0 = std::chrono::steady_clock::now();
        fn();
        const auto t1 = std::chrono::steady_clock::now();
        best = std::min(best, std::chrono::duration<double>(t1 - t0).count());
    }
    return best;
}

}  // namespace

// compute-plane [--m rows] [--k k] [--n n] [--reps r] [--cpu-pct p]
// Emits star-compute-plane/v1.  Defaults mirror a governed prefill
// shape (m=64,k=768,n=3072); the decode grid is always m=1.
int mode_compute_plane(const Args& a) {
    const long long m = std::max<long long>(1, a.num_arg("m", 64));
    const long long k = std::max<long long>(1, a.num_arg("k", 768));
    const long long n = std::max<long long>(1, a.num_arg("n", 3072));
    const int reps = (int)std::max<long long>(1, a.num_arg("reps", 4));
    long long cpu_pct = a.num_arg("cpu-pct", 25);
    if (cpu_pct < 0) cpu_pct = 0;
    if (cpu_pct > 50) cpu_pct = 50;
    // Allocation guard: keep every operand under ~2 GiB.
    const double bytes = 8.0 * (double)(m * k + k * n + m * n);
    if (bytes > 2.0e9) fail("COMPUTE_PLANE_SHAPE_TOO_LARGE");

    std::vector<double> A((size_t)(m * k)), B((size_t)(k * n));
    {
        std::mt19937_64 rng(4);
        std::uniform_real_distribution<double> u(-0.5, 0.5);
        for (auto& v : A) v = u(rng);
        for (auto& v : B) v = u(rng);
    }
    std::vector<double> C((size_t)(m * n), 0.0);

    // ── CPU lane: register-tiled GEMM via the C ABI ───────────────────
    const auto cpu_gemm = [&] {
        return gptbridge_native_transformer_matmul(
            A.data(), m, k, B.data(), k, n, C.data());
    };
    if (cpu_gemm() != 0) fail("COMPUTE_PLANE_CPU_GEMM_FAILED");
    const double cpu_s = cp_best_s(cpu_gemm, reps);
    const double cpu_gflops =
        cpu_s > 0 ? 2.0 * (double)m * k * n / cpu_s / 1e9 : 0.0;

    // Decode GEMV (m==1) — the latency-critical lane.
    std::vector<double> C1((size_t)n, 0.0);
    const auto cpu_gemv = [&] {
        return gptbridge_native_transformer_matmul(
            A.data(), 1, k, B.data(), k, n, C1.data());
    };
    cpu_gemv();
    const double gemv_s = cp_best_s(cpu_gemv, reps);
    const double gemv_gflops =
        gemv_s > 0 ? 2.0 * (double)k * n / gemv_s / 1e9 : 0.0;

    // ── RAM bandwidth: streaming read + write over 128 MiB ───────────
    const size_t ram_words = (size_t)16 * 1024 * 1024;
    std::vector<double> rambuf(ram_words, 1.0);
    volatile double sink = 0.0;
    const double read_s = cp_best_s(
        [&] {
            double s = 0.0;
            for (size_t i = 0; i < ram_words; ++i) s += rambuf[i];
            sink = s;
        },
        3);
    const double write_s = cp_best_s(
        [&] { std::memset(rambuf.data(), 0x5a, rambuf.size() * 8); },
        3);
    (void)sink;
    const double ram_gbs =
        (double)(ram_words * 8);
    const double ram_read_gbs = read_s > 0 ? ram_gbs / read_s / 1e9 : 0.0;
    const double ram_write_gbs = write_s > 0 ? ram_gbs / write_s / 1e9 : 0.0;
    rambuf.clear();
    rambuf.shrink_to_fit();

    // ── Device lane: Driver-API fp64 GEMM incl. pinned H2D/D2H ───────
    long long fb = 0, tb = 0;
    int ccm = 0, ccn = 0;
    const bool cuda = xcuda_probe(&fb, &tb, &ccm, &ccn) != 0;
    const int sm_count = cuda ? xcuda_sm_count() : 0;
    double gpu_gflops = 0.0;
    if (cuda && xcuda_available()) {
        const auto gpu_gemm = [&] {
            return xcuda_matmul_f64(A.data(), m, k, B.data(), n, C.data());
        };
        if (gpu_gemm() == 0) {
            const double gpu_s = cp_best_s(gpu_gemm, reps);
            gpu_gflops =
                gpu_s > 0 ? 2.0 * (double)m * k * n / gpu_s / 1e9 : 0.0;
        }
    }

    // ── Hybrid CPU+GPU lane: deterministic row split, measured ────────
    // Same begin/cpu/wait sequence the engine runs under
    // XINGCHENG_HYBRID_MATMUL: GPU takes rows [cpu_rows, m) async while
    // the CPU computes [0, cpu_rows).  max_abs_diff is checked against
    // the pure-CPU result — each row is bit-identical to whichever lane
    // owns it, so the diff bounds only the CPU-vs-GPU lane gap.
    // Same tile-quantized split as matmul_hybrid_f64 in the engine: the
    // GPU wide kernel's grid is ceil(m_gpu/64) block-rows, so gpu_rows
    // floors to a multiple of 64 and the CPU absorbs the remainder.
    long long gpu_rows =
        (m >= 8) ? ((m - m * cpu_pct / 100) / 64) * 64 : 0;
    if (gpu_rows == 0 && m > 64) gpu_rows = 64;  // shed all but one block
    const long long cpu_rows = m - gpu_rows;
    double hyb_gflops = 0.0, hyb_diff = -1.0, hyb_speedup = 0.0;
    bool hyb_ok = false;
    if (cuda && xcuda_available() && gpu_rows > 0 && cpu_rows > 0) {
        std::vector<double> H((size_t)(m * n), 0.0);
        std::vector<double> R((size_t)(m * n), 0.0);
        const auto hybrid_call = [&] {
            if (xcuda_matmul_f64_begin(A.data() + cpu_rows * k,
                                       gpu_rows, k, B.data(), n) != 0)
                return 3;
            gptbridge_native_transformer_matmul(
                A.data(), cpu_rows, k, B.data(), k, n, H.data());
            if (xcuda_matmul_f64_wait(H.data() + cpu_rows * n,
                                      gpu_rows, n) != 0) {
                gptbridge_native_transformer_matmul(
                    A.data() + cpu_rows * k, gpu_rows, k,
                    B.data(), k, n, H.data() + cpu_rows * n);
            }
            return 0;
        };
        if (hybrid_call() == 0) {
            gptbridge_native_transformer_matmul(
                A.data(), m, k, B.data(), k, n, R.data());
            hyb_diff = cpup_maxdiff(H, R);
            const double hyb_s = cp_best_s(hybrid_call, reps);
            hyb_gflops =
                hyb_s > 0 ? 2.0 * (double)m * k * n / hyb_s / 1e9 : 0.0;
            const double gpu_only_s = gpu_gflops > 0
                ? 2.0 * (double)m * k * n / gpu_gflops / 1e9 : 0.0;
            hyb_speedup =
                gpu_only_s > 0 && hyb_s > 0 ? gpu_only_s / hyb_s : 0.0;
            hyb_ok = true;
        }
    }

    SYSTEM_INFO si;
    GetSystemInfo(&si);
    MEMORYSTATUSEX ms{};
    ms.dwLength = sizeof(ms);
    long long ram_mb = 0;
    if (GlobalMemoryStatusEx(&ms)) ram_mb = (long long)(ms.ullTotalPhys >> 20);
    const int simd_level = gptbridge_native_simd_effective_level();

    char cc_buf[16] = "null", gpu_buf[24] = "null";
    char sm_buf[16] = "null", vfb_buf[24] = "null", vtb_buf[24] = "null";
    char hyb_gf[24] = "null", hyb_df[32] = "null", hyb_sp[24] = "null";
    char hrows_buf[24] = "null", grows_buf[24] = "null";
    if (cuda) {
        std::snprintf(cc_buf, sizeof(cc_buf), "\"%d.%d\"", ccm, ccn);
        std::snprintf(gpu_buf, sizeof(gpu_buf), "%.2f", gpu_gflops);
        std::snprintf(sm_buf, sizeof(sm_buf), "%d", sm_count);
        std::snprintf(vfb_buf, sizeof(vfb_buf), "%lld",
                      fb / (1024 * 1024));
        std::snprintf(vtb_buf, sizeof(vtb_buf), "%lld",
                      tb / (1024 * 1024));
    }
    if (hyb_ok) {
        std::snprintf(hyb_gf, sizeof(hyb_gf), "%.2f", hyb_gflops);
        std::snprintf(hyb_df, sizeof(hyb_df), "%.3e", hyb_diff);
        std::snprintf(hyb_sp, sizeof(hyb_sp), "%.3f", hyb_speedup);
        std::snprintf(hrows_buf, sizeof(hrows_buf), "%lld", cpu_rows);
        std::snprintf(grows_buf, sizeof(grows_buf), "%lld", gpu_rows);
    }
    std::printf(
        "{\"ok\":true,\"format\":\"star-compute-plane/v1\","
        "\"shape\":{\"m\":%lld,\"k\":%lld,\"n\":%lld,\"reps\":%d},"
        "\"cpu\":{\"simd_level\":%d,\"logical_cores\":%u,"
        "\"ram_mb\":%lld,\"gemm_f64_gflops\":%.2f,"
        "\"gemv_f64_gflops\":%.2f,\"ram_read_gbs\":%.2f,"
        "\"ram_write_gbs\":%.2f},"
        "\"cuda\":{\"available\":%s,\"cc\":%s,\"sm_count\":%s,"
        "\"vram_free_mb\":%s,\"vram_total_mb\":%s,"
        "\"gemm_f64_gflops\":%s},"
        "\"hybrid\":{\"available\":%s,\"cpu_pct\":%lld,"
        "\"cpu_rows\":%s,\"gpu_rows\":%s,\"gflops\":%s,"
        "\"max_abs_diff_vs_cpu\":%s,\"speedup_vs_gpu_only\":%s},"
        "\"note\":\"measured only — dispatch and certification state "
        "unchanged (see hw-caps for detection, parity suites for "
        "correctness)\"}\n",
        m, k, n, reps,
        simd_level, (unsigned)si.dwNumberOfProcessors, ram_mb,
        cpu_gflops, gemv_gflops, ram_read_gbs, ram_write_gbs,
        cuda ? "true" : "false", cc_buf, sm_buf, vfb_buf, vtb_buf,
        gpu_buf,
        hyb_ok ? "true" : "false", cpu_pct, hrows_buf, grows_buf,
        hyb_gf, hyb_df, hyb_sp);
    return 0;
}

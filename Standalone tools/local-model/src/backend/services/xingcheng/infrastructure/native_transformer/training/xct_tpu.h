// xct_tpu.h — B94 fragment of xingcheng_trainer.cpp (TPU-cluster-class lanes).
// Included once by xingcheng_trainer.cpp inside namespace xct, after
// xct_util.h and before xct_math.h / xct_backward.h / xct_job.h.
//
// Native TPU-cluster-class compute substrate for the fused hybrid model:
// systolic-style tiled GEMM lanes, runtime SIMD dispatch (AVX2/FMA fp32 with
// scalar fallback) and a bounded multicore worker pool — the on-CPU
// analogue of TPU pod data lanes. This is NOT an XLA/libtpu backend: B167
// keeps C++23 the sole training executor and JAX/XLA stays retired.
//
// Determinism contract: every parallel lane partitions disjoint output
// ranges only, so results are identical for any worker count. SIMD keeps a
// single fixed accumulation order per build (AVX2+FMA or scalar), so runs
// are reproducible per binary/config and only differ from the plain scalar
// order by lane-limited reassociation.
#pragma once

// ----------------------------------------------------------- lane config --

struct TpuLaneCfg {
    int threads = 0;    // 0 = auto (min(8, hw)); 1 = serial lanes; cap 16
    bool simd = true;   // runtime AVX2/FMA dispatch; false forces scalar
    bool tile4 = true;  // register-tiled GEMM at T>=16; false = legacy (A/B only)
};

static TpuLaneCfg g_tpu;

static int tpu_threads() {
    int n = g_tpu.threads;
    if (n <= 0) {
        unsigned hw = std::thread::hardware_concurrency();
        n = (int)std::min<unsigned>(8, hw > 0 ? hw : 1);
    }
    return std::max(1, std::min(16, n));
}

// -------------------------------------------------------- SIMD dispatch ---

#if defined(_M_X64) || defined(__x86_64__)
#define XCT_TPU_X64 1
#else
#define XCT_TPU_X64 0
#endif

static bool tpu_has_avx2_fma() {
#if XCT_TPU_X64 && defined(_MSC_VER)
    static const bool ok = [] {
        if (!g_tpu.simd) return false;
        int r[4] = {0, 0, 0, 0};
        __cpuidex(r, 1, 0);
        const bool osx = (r[2] & (1 << 27)) != 0;
        const bool avx = (r[2] & (1 << 28)) != 0;
        const bool fma = (r[2] & (1 << 12)) != 0;
        if (!osx || !avx || !fma) return false;
        if ((_xgetbv(0) & 0x6) != 0x6) return false;
        int r7[4] = {0, 0, 0, 0};
        __cpuidex(r7, 7, 0);
        return (r7[1] & (1 << 5)) != 0;   // AVX2
    }();
    return ok;
#else
    return false;
#endif
}

static const char* tpu_simd_name() {
    return tpu_has_avx2_fma() ? "avx2+fma" : "scalar";
}

#if XCT_TPU_X64 && defined(_MSC_VER)
// Horizontal reduction for the dual-accumulator dot: a fixed reduction
// tree shared by the scalar-shape path and the tile4 kernel so both
// produce bitwise-identical per-element results.
static float tpu_hsum2(__m256 s0, __m256 s1) {
    s0 = _mm256_add_ps(s0, s1);
    __m128 lo = _mm256_castps256_ps128(s0);
    __m128 hi = _mm256_extractf128_ps(s0, 1);
    lo = _mm_add_ps(lo, hi);
    lo = _mm_add_ps(lo, _mm_movehl_ps(lo, lo));
    lo = _mm_add_ss(lo, _mm_shuffle_ps(lo, lo, 1));
    return _mm_cvtss_f32(lo);
}
#endif

// fp32 dot: AVX2+FMA dual accumulators when supported, scalar otherwise.
static float tpu_dot(const float* a, const float* b, int64_t n) {
#if XCT_TPU_X64 && defined(_MSC_VER)
    if (tpu_has_avx2_fma()) {
        __m256 s0 = _mm256_setzero_ps(), s1 = _mm256_setzero_ps();
        int64_t i = 0;
        for (; i + 16 <= n; i += 16) {
            s0 = _mm256_fmadd_ps(_mm256_loadu_ps(a + i),
                                 _mm256_loadu_ps(b + i), s0);
            s1 = _mm256_fmadd_ps(_mm256_loadu_ps(a + i + 8),
                                 _mm256_loadu_ps(b + i + 8), s1);
        }
        for (; i + 8 <= n; i += 8)
            s0 = _mm256_fmadd_ps(_mm256_loadu_ps(a + i),
                                 _mm256_loadu_ps(b + i), s0);
        float sum = tpu_hsum2(s0, s1);
        for (; i < n; ++i) sum += a[i] * b[i];
        return sum;
    }
#endif
    float sum = 0.0f;
    for (int64_t i = 0; i < n; ++i) sum += a[i] * b[i];
    return sum;
}

// y += a * x (fp32 axpy).
static void tpu_axpy(float* y, float a, const float* x, int64_t n) {
#if XCT_TPU_X64 && defined(_MSC_VER)
    if (tpu_has_avx2_fma()) {
        const __m256 av = _mm256_set1_ps(a);
        int64_t i = 0;
        for (; i + 8 <= n; i += 8)
            _mm256_storeu_ps(y + i, _mm256_fmadd_ps(
                av, _mm256_loadu_ps(x + i), _mm256_loadu_ps(y + i)));
        for (; i < n; ++i) y[i] += a * x[i];
        return;
    }
#endif
    for (int64_t i = 0; i < n; ++i) y[i] += a * x[i];
}

// dst = src * a (scale-copy; src/dst may not alias).
static void tpu_scale_copy(float* dst, const float* src, float a, int64_t n) {
#if XCT_TPU_X64 && defined(_MSC_VER)
    if (tpu_has_avx2_fma()) {
        const __m256 av = _mm256_set1_ps(a);
        int64_t i = 0;
        for (; i + 8 <= n; i += 8)
            _mm256_storeu_ps(dst + i,
                             _mm256_mul_ps(av, _mm256_loadu_ps(src + i)));
        for (; i < n; ++i) dst[i] = src[i] * a;
        return;
    }
#endif
    for (int64_t i = 0; i < n; ++i) dst[i] = src[i] * a;
}

// dst *= a in place.
static void tpu_scale(float* dst, float a, int64_t n) {
    tpu_scale_copy(dst, dst, a, n);
}

// ---------------------------------------------------------- worker pool ---

// Bounded persistent lane pool: N-1 workers + caller lane. Each run is cut
// into `lanes` contiguous disjoint blocks of [0, n); a lane function is
// fn(begin, end). Generation tags stop stale lanes from claiming blocks of
// a newer run; lane exceptions are captured and rethrown on the caller
// lane so a throw can never wedge the pool (A178). Nested calls from
// inside a lane degrade to inline execution.
class TpuPool {
public:
    static TpuPool& inst() {
        static TpuPool p;
        return p;
    }
    void run(int64_t n, const std::function<void(int64_t, int64_t)>& fn) {
        size_t g;
        {
            std::unique_lock<std::mutex> lk(mu_);
            if (workers_.empty()) start();
            fn_ = &fn; n_ = n; next_ = 0;
            lanes_ = (int64_t)workers_.size() + 1;
            pending_ = lanes_;
            ep_ = nullptr;
            g = ++gen_;
        }
        cv_.notify_all();
        in_lane_ = true;                    // caller lane: nested -> inline
        take_work(g, n, fn);
        in_lane_ = false;
        {
            std::unique_lock<std::mutex> dk(mu_);
            done_cv_.wait(dk, [&] { return pending_ == 0; });
        }
        if (ep_) std::rethrow_exception(ep_);
    }
    ~TpuPool() {
        {
            std::lock_guard<std::mutex> lk(mu_);
            stop_ = true;
            ++gen_;
        }
        cv_.notify_all();
        for (auto& w : workers_) if (w.joinable()) w.join();
    }
private:
    TpuPool() = default;
    void start() {
        const int nw = tpu_threads() - 1;
        for (int i = 0; i < nw; ++i)
            workers_.emplace_back([this] { loop(); });
    }
    void take_work(size_t g, int64_t n,
                   const std::function<void(int64_t, int64_t)>& fn) {
        for (;;) {
            int64_t i;
            {
                std::lock_guard<std::mutex> lk(mu_);
                if (stop_ || gen_ != g || next_ >= lanes_) return;
                i = next_++;
            }
            const int64_t b = i * n / lanes_;
            const int64_t e = (i + 1) * n / lanes_;
            try {
                fn(b, e);
            } catch (...) {
                std::lock_guard<std::mutex> lk(mu_);
                if (gen_ == g && !ep_) ep_ = std::current_exception();
            }
            {
                std::lock_guard<std::mutex> lk(mu_);
                if (gen_ != g) return;
                if (--pending_ == 0) done_cv_.notify_all();
            }
        }
    }
    void loop() {
        in_lane_ = true;
        size_t seen = 0;
        for (;;) {
            const std::function<void(int64_t, int64_t)>* fn;
            size_t g;
            int64_t n;
            {
                std::unique_lock<std::mutex> lk(mu_);
                cv_.wait(lk, [&] { return gen_ != seen; });
                seen = gen_;
                if (stop_) return;
                fn = fn_; g = gen_; n = n_;
            }
            take_work(g, n, *fn);
        }
    }
    std::vector<std::thread> workers_;
    std::mutex mu_;
    std::condition_variable cv_, done_cv_;
    const std::function<void(int64_t, int64_t)>* fn_ = nullptr;
    int64_t n_ = 0, next_ = 0, lanes_ = 1;
    int64_t pending_ = 0;
    std::exception_ptr ep_;
    size_t gen_ = 0;
    bool stop_ = false;
public:
    static thread_local bool in_lane_;      // lane reentrancy guard
};
thread_local bool TpuPool::in_lane_ = false;

// parallel_for(n, fn): partition [0,n) into lane ranges; serial when few
// lanes, tiny n, or already inside a lane.
static void parallel_for(int64_t n,
                         const std::function<void(int64_t, int64_t)>& fn) {
    if (n <= 0) return;
    if (tpu_threads() <= 1 || n < 64 || TpuPool::in_lane_) {
        fn(0, n);
        return;
    }
    TpuPool::inst().run(n, fn);
}

// ------------------------------------------------------------ GEMM lanes --

// Row-shared dot block: y[j] = x_j . w for up to 4 rows against the same
// weight row. The old elementwise lane re-streamed the whole W row per
// output element — the dominant DRAM traffic in every GEMM (the lm_head
// alone moved ~51 GB/step). Four rows share one W read, quartering that
// traffic. Each element keeps tpu_dot's exact accumulation order (dual
// accumulators over 16-float chunks, one 8-chunk, scalar tail), so the
// results are bitwise identical to per-row tpu_dot calls.
static void tpu_dot4(const float* x0, const float* x1, const float* x2,
                     const float* x3, const float* w, float* y4,
                     int64_t n) {
#if XCT_TPU_X64 && defined(_MSC_VER)
    if (tpu_has_avx2_fma()) {
        const __m256 z = _mm256_setzero_ps();
        __m256 a00 = z, a01 = z, a10 = z, a11 = z;
        __m256 a20 = z, a21 = z, a30 = z, a31 = z;
        int64_t i = 0;
        for (; i + 16 <= n; i += 16) {
            const __m256 w0 = _mm256_loadu_ps(w + i);
            const __m256 w1 = _mm256_loadu_ps(w + i + 8);
            a00 = _mm256_fmadd_ps(_mm256_loadu_ps(x0 + i), w0, a00);
            a01 = _mm256_fmadd_ps(_mm256_loadu_ps(x0 + i + 8), w1, a01);
            a10 = _mm256_fmadd_ps(_mm256_loadu_ps(x1 + i), w0, a10);
            a11 = _mm256_fmadd_ps(_mm256_loadu_ps(x1 + i + 8), w1, a11);
            a20 = _mm256_fmadd_ps(_mm256_loadu_ps(x2 + i), w0, a20);
            a21 = _mm256_fmadd_ps(_mm256_loadu_ps(x2 + i + 8), w1, a21);
            a30 = _mm256_fmadd_ps(_mm256_loadu_ps(x3 + i), w0, a30);
            a31 = _mm256_fmadd_ps(_mm256_loadu_ps(x3 + i + 8), w1, a31);
        }
        if (i + 8 <= n) {
            const __m256 w0 = _mm256_loadu_ps(w + i);
            a00 = _mm256_fmadd_ps(_mm256_loadu_ps(x0 + i), w0, a00);
            a10 = _mm256_fmadd_ps(_mm256_loadu_ps(x1 + i), w0, a10);
            a20 = _mm256_fmadd_ps(_mm256_loadu_ps(x2 + i), w0, a20);
            a30 = _mm256_fmadd_ps(_mm256_loadu_ps(x3 + i), w0, a30);
            i += 8;
        }
        y4[0] = tpu_hsum2(a00, a01); y4[1] = tpu_hsum2(a10, a11);
        y4[2] = tpu_hsum2(a20, a21); y4[3] = tpu_hsum2(a30, a31);
        for (; i < n; ++i) {
            y4[0] += x0[i] * w[i]; y4[1] += x1[i] * w[i];
            y4[2] += x2[i] * w[i]; y4[3] += x3[i] * w[i];
        }
        return;
    }
#endif
    const float* xs[4] = {x0, x1, x2, x3};
    for (int j = 0; j < 4; ++j) y4[j] = tpu_dot(xs[j], w, n);
}

// Two-row variant: 4 accumulators instead of 8 — measured at the
// canonical lm_head shape (T=2048, O=8192, y = 64 MB) tile4 spills its
// accumulator set and collapses to 0.26x while tile2 still wins 1.15x;
// the strided-row write streams stay under two per direction. Same
// dual-accumulator association, bitwise identical to tpu_dot rows.
static void tpu_dot2(const float* x0, const float* x1, const float* w,
                     float* y2, int64_t n) {
#if XCT_TPU_X64 && defined(_MSC_VER)
    if (tpu_has_avx2_fma()) {
        const __m256 z = _mm256_setzero_ps();
        __m256 a00 = z, a01 = z, a10 = z, a11 = z;
        int64_t i = 0;
        for (; i + 16 <= n; i += 16) {
            const __m256 w0 = _mm256_loadu_ps(w + i);
            const __m256 w1 = _mm256_loadu_ps(w + i + 8);
            a00 = _mm256_fmadd_ps(_mm256_loadu_ps(x0 + i), w0, a00);
            a01 = _mm256_fmadd_ps(_mm256_loadu_ps(x0 + i + 8), w1, a01);
            a10 = _mm256_fmadd_ps(_mm256_loadu_ps(x1 + i), w0, a10);
            a11 = _mm256_fmadd_ps(_mm256_loadu_ps(x1 + i + 8), w1, a11);
        }
        if (i + 8 <= n) {
            const __m256 w0 = _mm256_loadu_ps(w + i);
            a00 = _mm256_fmadd_ps(_mm256_loadu_ps(x0 + i), w0, a00);
            a10 = _mm256_fmadd_ps(_mm256_loadu_ps(x1 + i), w0, a10);
            i += 8;
        }
        y2[0] = tpu_hsum2(a00, a01); y2[1] = tpu_hsum2(a10, a11);
        for (; i < n; ++i) { y2[0] += x0[i] * w[i]; y2[1] += x1[i] * w[i]; }
        return;
    }
#endif
    y2[0] = tpu_dot(x0, w, n); y2[1] = tpu_dot(x1, w, n);
}

// Legacy linear lane: y[t,o] = x[t,:] . w[o,:] over the flat output
// space — one full W-row stream per output element. Kept verbatim as
// the small-T path (tile4 is a measured 0.31x loss at T=8) and as the
// bitwise-parity oracle for the benchmark artifact.
static void tpu_linear_legacy(const float* x, const float* w, float* y,
                              int T, int I, int O) {
    parallel_for((int64_t)T * O, [&](int64_t b, int64_t e) {
        int64_t t = b / O, o = b % O;
        for (int64_t p = b; p < e; ++p) {
            y[p] = tpu_dot(x + (size_t)t * I, w + (size_t)o * I, I);
            if (++o == O) { o = 0; ++t; }
        }
    });
}

// Tiled linear lane (tile4): y[t,o] = x[t,:] . w[o,:]. Lanes own
// t-blocks of 4 rows and sweep all outputs serially, so each weight row
// is fetched once per 4 x-rows instead of once per element. Per-element
// results are lane-count independent and bitwise identical to tpu_dot
// rows.
static void tpu_linear_tile4(const float* x, const float* w, float* y,
                             int T, int I, int O) {
    const int64_t nb = (T + 3) / 4;
    parallel_for(nb, [&](int64_t b, int64_t e) {
        for (int64_t tb = b; tb < e; ++tb) {
            const int64_t t0 = tb * 4;
            const int nr = (int)std::min<int64_t>(4, T - t0);
            const float* xb = x + (size_t)t0 * I;
            float* yb = y + (size_t)t0 * O;
            for (int64_t o = 0; o < O; ++o) {
                const float* wr = w + (size_t)o * I;
                if (nr == 4) {
                    // The four rows produce column-o outputs strided by O
                    // — dot4 results go to a scratch quad and scatter;
                    // writing them contiguously would corrupt the layout.
                    float q4[4];
                    tpu_dot4(xb, xb + I, xb + 2 * I, xb + 3 * I,
                             wr, q4, I);
                    yb[o] = q4[0];
                    yb[(size_t)O + o] = q4[1];
                    yb[(size_t)2 * O + o] = q4[2];
                    yb[(size_t)3 * O + o] = q4[3];
                } else {
                    for (int j = 0; j < nr; ++j)
                        yb[(size_t)j * O + o] =
                            tpu_dot(xb + (size_t)j * I, wr, I);
                }
            }
        }
    });
}

// Legacy backward lane: dW over output rows (t-ascending axpy), dx over
// input rows (o-ascending axpy) — the pre-tile4 reference order, kept as
// the small-T path and the bitwise-parity oracle.
static void tpu_linear_bwd_legacy(const float* dy, const float* x,
                                  const float* w, float* dx, float* dW,
                                  int T, int I, int O) {
    if (dW) parallel_for(O, [&](int64_t b, int64_t e) {
        for (int64_t o = b; o < e; ++o) {
            float* dw = dW + (size_t)o * I;
            for (int64_t t = 0; t < T; ++t)
                tpu_axpy(dw, dy[(size_t)t * O + o], x + (size_t)t * I, I);
        }
    });
    if (!dx) return;
    parallel_for(T, [&](int64_t b, int64_t e) {
        for (int64_t t = b; t < e; ++t) {
            float* dxr = dx + (size_t)t * I;
            const float* dyr = dy + (size_t)t * O;
            for (int64_t o = 0; o < O; ++o)
                tpu_axpy(dxr, dyr[o], w + (size_t)o * I, I);
        }
    });
}

// Tiled backward (tile4): two disjoint partitions — dW over output-row
// blocks, dx over input-row blocks — matching the reference accumulation
// order per element (t-ascending for dW, o-ascending for dx; the row-
// block interleave only changes which row each FMA lands in, never the
// add order within an element). Row blocks share the streamed operand:
// x rows are read once per 4 dW rows, W rows once per 4 dx rows —
// quartering the L3/DRAM traffic of the per-row lanes.
static void tpu_linear_bwd_tile4(const float* dy, const float* x,
                                 const float* w, float* dx, float* dW,
                                 int T, int I, int O) {
    if (dW) {
        const int64_t nob = (O + 3) / 4;
        parallel_for(nob, [&](int64_t b, int64_t e) {
            for (int64_t ob = b; ob < e; ++ob) {
                const int64_t o0 = ob * 4;
                const int no = (int)std::min<int64_t>(4, O - o0);
                for (int64_t t = 0; t < T; ++t) {
                    const float* xr = x + (size_t)t * I;
                    const float* dyr = dy + (size_t)t * O + o0;
                    for (int j = 0; j < no; ++j)
                        tpu_axpy(dW + (size_t)(o0 + j) * I, dyr[j], xr, I);
                }
            }
        });
    }
    if (!dx) return;
    const int64_t ntb = (T + 3) / 4;
    parallel_for(ntb, [&](int64_t b, int64_t e) {
        for (int64_t tb = b; tb < e; ++tb) {
            const int64_t t0 = tb * 4;
            const int nt = (int)std::min<int64_t>(4, T - t0);
            for (int64_t o = 0; o < O; ++o) {
                const float* wr = w + (size_t)o * I;
                for (int j = 0; j < nt; ++j) {
                    const int64_t t = t0 + j;
                    tpu_axpy(dx + (size_t)t * I, dy[(size_t)t * O + o],
                             wr, I);
                }
            }
        }
    });
}

// Tiled linear lane (tile2): identical structure to tile4 but two x
// rows per block — for very large outputs (T*O past ~16M elements,
// e.g. the lm_head at T=2048/O=8192) tile4's eight-accumulator set
// spills and measures 0.26x of legacy while tile2 still wins.
static void tpu_linear_tile2(const float* x, const float* w, float* y,
                             int T, int I, int O) {
    const int64_t nb = (T + 1) / 2;
    parallel_for(nb, [&](int64_t b, int64_t e) {
        for (int64_t tb = b; tb < e; ++tb) {
            const int64_t t0 = tb * 2;
            const int nr = (int)std::min<int64_t>(2, T - t0);
            const float* xb = x + (size_t)t0 * I;
            float* yb = y + (size_t)t0 * O;
            for (int64_t o = 0; o < O; ++o) {
                const float* wr = w + (size_t)o * I;
                if (nr == 2) {
                    float q2[2];
                    tpu_dot2(xb, xb + I, wr, q2, I);
                    yb[o] = q2[0]; yb[(size_t)O + o] = q2[1];
                } else {
                    yb[o] = tpu_dot(xb, wr, I);
                }
            }
        }
    });
}

// Shape dispatcher (fixed rules): T >= 16 -> tiled; T < 16 -> legacy.
// Measured on the production microbenchmark: tile4 is a strict loss at
// T=8 (0.31x — setup overhead beats the stream saving), so the small-T
// cutoff is a hard rule, not a heuristic. Huge outputs (T*O >= 16M,
// the lm_head shape) use tile2 — tile4 spills its eight accumulators
// there and measured 0.26x. The same rules cover grouped expert linear
// (dispatch sees Te as T). XCT_TPU_TILE4=0 forces legacy for A/B
// measurement only — it is not a production switch.
static constexpr int kTpuTile4MinT = 16;
static constexpr int64_t kTpuTile4MaxOut = 16LL * 1024 * 1024;

static void tpu_linear(const float* x, const float* w, float* y,
                       int T, int I, int O) {
    if (g_tpu.tile4 && T >= kTpuTile4MinT) {
        if ((int64_t)T * O >= kTpuTile4MaxOut) {
            tpu_linear_tile2(x, w, y, T, I, O);
            return;
        }
        tpu_linear_tile4(x, w, y, T, I, O);
        return;
    }
    tpu_linear_legacy(x, w, y, T, I, O);
}

static void tpu_linear_bwd(const float* dy, const float* x, const float* w,
                           float* dx, float* dW, int T, int I, int O) {
    // Measured on the production microbenchmark: tile4's row-blocked
    // backward is a systematic loss at the mid-T shapes that dominate
    // real steps (768x768 T=64: 0.54x, T=128: 0.48x — the 4-row dx block
    // thrashes store buffers harder than the stream saving pays), while
    // the legacy split is already near its bandwidth roof. Keep backward
    // on the legacy partitions; forward keeps the tile4/tile2 wins.
    (void)g_tpu; (void)kTpuTile4MinT;
    tpu_linear_bwd_legacy(dy, x, w, dx, dW, T, I, O);
}

// Elementwise lane: dst[i] = f(i) over a flat range (AdamW / gate loops).
template <typename F>
static void tpu_elementwise(int64_t n, F&& f) {
    parallel_for(n, [&](int64_t b, int64_t e) {
        for (int64_t i = b; i < e; ++i) f(i);
    });
}

// Fixed-shape parallel sum of squares: every tensor is cut into the same
// 64 chunks regardless of worker count; lanes sum whole chunks serially
// and the caller combines partials in chunk order — deterministic and
// lane-count independent. Chunk-boundary reassociation can differ from
// a flat serial scan by ~1ulp; used for the global grad-norm where the
// value feeds only the clip threshold, not stored state.
static double tpu_sumsq(const float* x, int64_t n) {
    constexpr int64_t NC = 64;
    double part[NC] = {};
    parallel_for(NC, [&](int64_t b, int64_t e) {
        for (int64_t c = b; c < e; ++c) {
            const int64_t lo = c * n / NC, hi = (c + 1) * n / NC;
            double s = 0.0;
            for (int64_t i = lo; i < hi; ++i)
                s += (double)x[(size_t)i] * x[(size_t)i];
            part[c] = s;
        }
    });
    double s = 0.0;
    for (int64_t c = 0; c < NC; ++c) s += part[c];
    return s;
}

// ---------------------------------------------------------- gemm bench ---

// star-cpu-gemm-benchmark/v1: legacy vs tile4 across the production
// training shapes (300M xc-fused-1 GEMM dims) and the T sweep. Each
// cell reports GFLOP/s for forward and backward plus a bitwise-parity
// flag — the tile4 lane MUST be bitwise identical to the legacy lane
// per element, so a parity failure fails the mode (exit 2).
// Threads come from XCT_TPU_THREADS (g_tpu.threads) — run the mode once
// per thread count; the artifact records the count verbatim.
static int gemmbench() {
    if (const char* e = std::getenv("XCT_TPU_THREADS"))
        g_tpu.threads = std::atoi(e);
    if (const char* e = std::getenv("XCT_TPU_SIMD"))
        g_tpu.simd = !(e[0] == '0' && e[1] == '\0');

    const int threads = tpu_threads();
    const int dims[][2] = {
        {768, 768}, {768, 2048}, {2048, 768}, {768, 1024}, {768, 8192}};
    const int Ts[] = {8, 16, 32, 64, 128, 512};

    std::mt19937 rng(0x5eedu);
    std::uniform_real_distribution<float> uf(-1.0f, 1.0f);
    auto fill = [&](std::vector<float>& v) {
        for (auto& x : v) x = uf(rng);
    };
    auto clock_ms = [](auto&& fn, int reps) {
        fn();   // warmup: pool start + page-in
        const auto t0 = std::chrono::steady_clock::now();
        for (int i = 0; i < reps; ++i) fn();
        const auto t1 = std::chrono::steady_clock::now();
        return std::chrono::duration<double, std::milli>(t1 - t0)
                   .count() / reps;
    };

    bool all_parity = true;
    std::string entries;
    for (const auto& dm : dims) {
        const int I = dm[0], O = dm[1];
        std::vector<float> w((size_t)I * O);
        fill(w);
        for (const int T : Ts) {
            std::vector<float> x((size_t)T * I), dy((size_t)T * O);
            std::vector<float> y0((size_t)T * O), y1((size_t)T * O);
            std::vector<float> dx0((size_t)T * I), dx1((size_t)T * I);
            std::vector<float> dW0((size_t)O * I), dW1((size_t)O * I);
            fill(x); fill(dy);

            const double fwd_flops = 2.0 * T * I * O;
            const double bwd_flops = 2.0 * fwd_flops;
            // >=3 reps every cell (single-shot timings on large shapes
            // read scheduler noise, not bandwidth); small shapes scale
            // to ~0.3 GFLOP of work per measurement.
            const int reps = (int)std::max<int64_t>(
                3, std::min<int64_t>(50,
                    (int64_t)(3.0e8 / fwd_flops)));

            const double lf_ms = clock_ms([&] {
                tpu_linear_legacy(x.data(), w.data(), y0.data(),
                                  T, I, O);
            }, reps);
            const double tf_ms = clock_ms([&] {
                tpu_linear_tile4(x.data(), w.data(), y1.data(),
                                 T, I, O);
            }, reps);
            const double lb_ms = clock_ms([&] {
                tpu_linear_bwd_legacy(dy.data(), x.data(), w.data(),
                                      dx0.data(), dW0.data(), T, I, O);
            }, reps);
            const double tb_ms = clock_ms([&] {
                tpu_linear_bwd_tile4(dy.data(), x.data(), w.data(),
                                     dx1.data(), dW1.data(), T, I, O);
            }, reps);

            const bool par =
                std::memcmp(y0.data(), y1.data(),
                            y0.size() * sizeof(float)) == 0 &&
                std::memcmp(dx0.data(), dx1.data(),
                            dx0.size() * sizeof(float)) == 0 &&
                std::memcmp(dW0.data(), dW1.data(),
                            dW0.size() * sizeof(float)) == 0;
            if (!par) all_parity = false;
            if (!entries.empty()) entries += ',';
            char buf[768];
            std::snprintf(buf, sizeof buf,
                "{\"shape\":\"%dx%d\",\"T\":%d,\"reps\":%d,"
                "\"legacy_fwd_ms\":%.4f,\"tile4_fwd_ms\":%.4f,"
                "\"legacy_bwd_ms\":%.4f,\"tile4_bwd_ms\":%.4f,"
                "\"legacy_fwd_gflops\":%.2f,\"tile4_fwd_gflops\":%.2f,"
                "\"fwd_speedup\":%.3f,"
                "\"legacy_bwd_gflops\":%.2f,\"tile4_bwd_gflops\":%.2f,"
                "\"bwd_speedup\":%.3f,\"bitwise_parity\":%s}",
                I, O, T, reps, lf_ms, tf_ms, lb_ms, tb_ms,
                fwd_flops / (lf_ms * 1e6),
                fwd_flops / (tf_ms * 1e6), lf_ms / tf_ms,
                bwd_flops / (lb_ms * 1e6),
                bwd_flops / (tb_ms * 1e6), lb_ms / tb_ms,
                par ? "true" : "false");
            entries += buf;
        }
    }
    std::printf(
        "{\"format\":\"star-cpu-gemm-benchmark/v1\",\"threads\":%d,"
        "\"simd\":\"%s\",\"bitwise_parity\":%s,\"entries\":[%s]}\n",
        threads, tpu_simd_name(), all_parity ? "true" : "false",
        entries.c_str());
    return all_parity ? 0 : 2;
}

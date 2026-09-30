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

// Tiled linear lane: y[t,o] = x[t,:] . w[o,:] over the flat output space.
// Lane-granular contiguous p-ranges keep one fixed accumulation order per
// output element, identical for any lane count.
static void tpu_linear(const float* x, const float* w, float* y,
                       int T, int I, int O) {
    parallel_for((int64_t)T * O, [&](int64_t b, int64_t e) {
        int64_t t = b / O, o = b % O;
        for (int64_t p = b; p < e; ++p) {
            y[p] = tpu_dot(x + (size_t)t * I, w + (size_t)o * I, I);
            if (++o == O) { o = 0; ++t; }
        }
    });
}

// Split backward GEMM into two disjoint partitions — dW over output rows,
// dx over input rows — matching the reference accumulation order per
// element (t-ascending for dW, o-ascending for dx).
static void tpu_linear_bwd(const float* dy, const float* x, const float* w,
                           float* dx, float* dW, int T, int I, int O) {
    parallel_for(O, [&](int64_t b, int64_t e) {
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

// Elementwise lane: dst[i] = f(i) over a flat range (AdamW / gate loops).
template <typename F>
static void tpu_elementwise(int64_t n, F&& f) {
    parallel_for(n, [&](int64_t b, int64_t e) {
        for (int64_t i = b; i < e; ++i) f(i);
    });
}

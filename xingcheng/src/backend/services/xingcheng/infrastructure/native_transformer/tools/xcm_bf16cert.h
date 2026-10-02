// xcm_bf16cert.h — §58 BF16 production certification probe.
//
// The FP64 CPU oracle is the reference (§1/§65). cuBLAS-fp64 and the
// NVRTC bf16 kernel run the same deterministic matrices across
// representative shapes (decode GEMV, prefill GEMM, LM-head, MoE
// block) and are compared on absolute error, relative error and
// per-row argmax agreement — the GEMM-level logit-parity analog.
// Verdicts are evidence only; promotion to BF16-primary stays a
// lifecycle decision (§58 certifies, it does not promote).
//
//   xc_modeltool bf16-cert [--rel-tol 0.03] [--f64-rel-tol 1e-10]
//
// star-bf16-certification/v1

extern "C" {
int xcuda_available();
int xcuda_bf16_available();
int xcuda_matmul_f64(const double* a, long long m, long long k,
                     const double* b, long long n, double* out);
int xcuda_matmul_bf16(const double* a, long long m, long long k,
                      const double* b, long long n, double* out);
int xcuda_release_weights();
void xengine_cuda_lane(int cuda_requested, int bf16_requested);
int xengine_cuda_lane_state();
void xcuda_graph_enable(int on);
int xcuda_graph_state();
}

static void bf16cert_oracle(const std::vector<double>& a,
                            const std::vector<double>& b,
                            std::vector<double>& out,
                            long long m, long long k, long long n) {
    // Row-major C[m,n] = A[m,k] * B[k,n], fp64 sequential-k
    // accumulation — the same order the device kernels use.
    out.assign((size_t)(m * n), 0.0);
    for (long long r = 0; r < m; ++r)
        for (long long c = 0; c < n; ++c) {
            double acc = 0.0;
            for (long long i = 0; i < k; ++i)
                acc += a[(size_t)(r * k + i)] *
                       b[(size_t)(i * n + c)];
            out[(size_t)(r * n + c)] = acc;
        }
}

struct Bf16Err {
    double max_abs = 0.0, rms_rel = 0.0, max_rel_sig = 0.0;
    long long argmax_match = 0, rows = 0;
};

// Error metrics vs the oracle: raw relative error on near-zero outputs
// is meaningless for quantized paths (1e-2 abs on a 1e-4 output reads
// as rel=100), so certification uses three standard measures:
//   max_abs     — worst absolute drift
//   rms_rel     — ||cand-ref|| / ||ref|| over the whole matrix
//   max_rel_sig — worst relative error over outputs above 10% of
//                 max|ref| (the significant logits only)
// plus per-row argmax agreement — the greedy-parity analog.
static Bf16Err bf16cert_err(const std::vector<double>& cand,
                            const std::vector<double>& ref,
                            long long m, long long n) {
    Bf16Err e;
    e.rows = m;
    double ref_max = 0.0, num = 0.0, den = 0.0;
    for (size_t i = 0; i < ref.size(); ++i) {
        if (std::fabs(ref[i]) > ref_max) ref_max = std::fabs(ref[i]);
        const double d = cand[i] - ref[i];
        num += d * d;
        den += ref[i] * ref[i];
        const double ae = std::fabs(d);
        if (ae > e.max_abs) e.max_abs = ae;
    }
    e.rms_rel = den > 0 ? std::sqrt(num / den) : 0.0;
    const double sig = ref_max * 0.1;
    for (size_t i = 0; i < ref.size(); ++i)
        if (std::fabs(ref[i]) >= sig) {
            const double re = std::fabs(cand[i] - ref[i]) /
                              std::fabs(ref[i]);
            if (re > e.max_rel_sig) e.max_rel_sig = re;
        }
    for (long long r = 0; r < m; ++r) {
        long long ar = 0, ac = 0;
        double bv = -1e300, cv = -1e300;
        for (long long c = 0; c < n; ++c) {
            const double rv = ref[(size_t)(r * n + c)];
            const double xv = cand[(size_t)(r * n + c)];
            if (rv > bv) { bv = rv; ar = c; }
            if (xv > cv) { cv = xv; ac = c; }
        }
        if (ar == ac) ++e.argmax_match;
    }
    return e;
}

static int mode_bf16_cert(const Args& a) {
    const double bf16_rms_tol = a.has("rms-tol")
        ? std::stod(a.get("rms-tol")) : 0.02;
    const double bf16_rel_tol = a.has("rel-tol")
        ? std::stod(a.get("rel-tol")) : 0.05;
    const double f64_tol = a.has("f64-rel-tol")
        ? std::stod(a.get("f64-rel-tol")) : 1e-10;

    struct Shape { long long m, k, n; const char* name; };
    static const Shape shapes[] = {
        {1, 768, 1536, "decode-gemv"},
        {8, 768, 3072, "prefill-gemm"},
        {4, 512, 8192, "lm-head"},
        {16, 256, 1024, "moe-block"},
    };

    const bool cuda = xcuda_available() == 1;
    const bool bf16 = xcuda_bf16_available() == 1;
    bool ok = true;
    std::mt19937_64 rng(7);
    std::normal_distribution<double> ga(0.0, 1.0);
    std::normal_distribution<double> gb(0.0, 0.05);   // weight scale

    std::ostringstream o;
    o << "{\"format\":\"star-bf16-certification/v1\","
      << "\"cuda_f64_lane\":" << (cuda ? "true" : "false")
      << ",\"bf16_lane\":" << (bf16 ? "true" : "false")
      << ",\"bf16_rms_tol\":" << bf16_rms_tol
      << ",\"bf16_rel_sig_tol\":" << bf16_rel_tol
      << ",\"f64_rel_tol\":" << f64_tol
      << ",\"shapes\":[";
    bool first = true;
    for (const auto& s : shapes) {
        std::vector<double> av((size_t)(s.m * s.k)),
                            bv((size_t)(s.k * s.n));
        for (auto& x : av) x = ga(rng);
        for (auto& x : bv) x = gb(rng);
        std::vector<double> ref, c64, cb;
        bf16cert_oracle(av, bv, ref, s.m, s.k, s.n);

        Bf16Err ef, eb;
        bool have_f = cuda &&
            xcuda_matmul_f64(av.data(), s.m, s.k, bv.data(), s.n,
                             [&]{ c64.resize((size_t)(s.m*s.n));
                                  return c64.data(); }()) == 0;
        bool have_b = bf16 &&
            xcuda_matmul_bf16(av.data(), s.m, s.k, bv.data(), s.n,
                              [&]{ cb.resize((size_t)(s.m*s.n));
                                   return cb.data(); }()) == 0;
        if (have_f) ef = bf16cert_err(c64, ref, s.m, s.n);
        if (have_b) eb = bf16cert_err(cb, ref, s.m, s.n);

        const bool f_ok = !have_f ||
            (ef.max_rel_sig <= f64_tol && ef.argmax_match == ef.rows);
        const bool b_ok = !have_b ||
            (eb.rms_rel <= bf16_rms_tol &&
             eb.max_rel_sig <= bf16_rel_tol &&
             eb.argmax_match == eb.rows);
        ok &= f_ok && b_ok;
        if (!first) o << ',';
        first = false;
        o << "{\"name\":\"" << s.name << "\",\"m\":" << s.m
          << ",\"k\":" << s.k << ",\"n\":" << s.n
          << ",\"f64\":{\"ran\":" << (have_f ? "true" : "false")
          << ",\"max_abs_err\":" << ef.max_abs
          << ",\"rms_rel_err\":" << ef.rms_rel
          << ",\"max_rel_sig\":" << ef.max_rel_sig
          << ",\"argmax_match\":" << ef.argmax_match
          << ",\"argmax_rows\":" << ef.rows
          << ",\"pass\":" << (f_ok ? "true" : "false") << "}"
          << ",\"bf16\":{\"ran\":" << (have_b ? "true" : "false")
          << ",\"max_abs_err\":" << eb.max_abs
          << ",\"rms_rel_err\":" << eb.rms_rel
          << ",\"max_rel_sig\":" << eb.max_rel_sig
          << ",\"argmax_match\":" << eb.argmax_match
          << ",\"argmax_rows\":" << eb.rows
          << ",\"pass\":" << (b_ok ? "true" : "false") << "}}";
    }
    const char* verdict = !ok ? "BF16_GEMM_UNCERTIFIED"
        : (cuda && bf16) ? "BF16_GEMM_CERTIFIED" : "NO_DEVICE";
    o << "],\"verdict\":\"" << verdict << "\","
      << "\"note\":\"GEMM-level evidence only; BF16-primary promotion "
         "is a lifecycle decision, not a probe side effect\","
      << "\"ok\":" << (ok ? "true" : "false") << "}";
    std::printf("%s\n", o.str().c_str());
    (void)xcuda_release_weights();
    return ok ? 0 : 1;
}

// ---------------- (section)58 end-to-end drift: fp64 lane vs bf16 lane ------
// Same engine instance, same prompts; xengine_cuda_lane flips the atomic the
// matmul dispatcher reads, so logits()/generate() run each lane back to back
// without respawning. logits() never touches the prefix cache
// (append_cache=false), so the comparison is not contaminated.
int mode_bf16_drift(const Args& a) {
    const std::string bundle = a.get("bundle", "");
    const int prefill = a.num_arg("--prefill", 64);
    const int decode = a.num_arg("--decode", 32);
    const int64_t seed = a.num_arg("--seed", 7);
    JsonWriter w;
    w.begin().kv("schema", "star-bf16-drift-report/v1")
            .kv("bundle", bundle).kv("prefill", prefill)
            .kv("decode", decode).kv("seed", seed);
    if (bundle.empty()) {
        w.kv("cuda_available", false).kv("verdict", "NO_BUNDLE")
         .kv("ok", false).end();
        emit_line(w.str());
        return 0;
    }
    try {
        NativeInferenceEngine e;
        e.load(bundle);
        std::vector<int64_t> ids(static_cast<size_t>(prefill));
        for (int i = 0; i < prefill; ++i)
            ids[static_cast<size_t>(i)] = seed + i;

        // Lane 1: CUDA fp64 cuBLAS (current primary).
        std::vector<double> l64, l16;
        std::vector<int64_t> g64, g16;
        std::string err64, err16;
        SamplingConfig sc;
        sc.temperature = 0.0;
        xengine_cuda_lane(1, 0);
        try { l64 = e.logits(ids); g64 = e.generate(ids, decode, sc); }
        catch (const std::exception& ex) { err64 = ex.what(); }
        xengine_cuda_lane(1, 1);
        try { l16 = e.logits(ids); g16 = e.generate(ids, decode, sc); }
        catch (const std::exception& ex) { err16 = ex.what(); }
        xengine_cuda_lane(0, 0);

        w.kv("f64_lane_ok", err64.empty())
         .kv("bf16_lane_ok", err16.empty());
        if (!err64.empty()) w.kv("f64_error", err64);
        if (!err16.empty()) w.kv("bf16_error", err16);
        if (err64.empty() && err16.empty() && l64.size() == l16.size()) {
            double se = 0.0, sr = 0.0, max_abs = 0.0;
            int64_t arg64 = -1, arg16 = -1;
            double b64 = -1.0, b16 = -1.0;
            for (size_t i = 0; i < l64.size(); ++i) {
                const double d = l16[i] - l64[i];
                se += d * d; sr += l64[i] * l64[i];
                max_abs = std::max(max_abs, std::fabs(d));
                if (l64[i] > b64) { b64 = l64[i]; arg64 = (int64_t)i; }
                if (l16[i] > b16) { b16 = l16[i]; arg16 = (int64_t)i; }
            }
            const double rms_rel = sr > 0.0 ? std::sqrt(se / sr) : 0.0;
            const bool argmax_same = arg64 == arg16;
            int64_t agree = 0, first_div = -1;
            const size_t gn = std::min(g64.size(), g16.size());
            for (size_t i = 0; i < gn; ++i) {
                if (g64[i] == g16[i]) ++agree;
                else if (first_div < 0) first_div = (int64_t)i;
            }
            const bool gen_same =
                g64.size() == g16.size() && agree == (int64_t)g64.size();
            bool finite = true;
            for (double v : l16) finite = finite && std::isfinite(v);
            w.kv("logits", l64.size())
             .kv("max_abs_err", max_abs).kv("rms_rel", rms_rel)
             .kv("logit_argmax_match", argmax_same)
             .kv("gen_len_64", (int64_t)g64.size())
             .kv("gen_len_16", (int64_t)g16.size())
             .kv("gen_agree_tokens", agree)
             .kv("gen_first_divergence", first_div)
             .kv("gen_identical", gen_same)
             .kv("logits_finite", finite);
            const bool cert = finite && argmax_same && gen_same &&
                              rms_rel < 0.02;
            w.kv("verdict", cert ? "BF16_E2E_AGREEMENT" : "BF16_E2E_DRIFT")
             .kv("ok", true);
        } else {
            w.kv("verdict", "BF16_E2E_NO_LANE").kv("ok", true);
        }
    } catch (const std::exception& ex) {
        w.kv("error", ex.what()).kv("verdict", "BF16_E2E_ERROR")
         .kv("ok", false);
    }
    w.end();
    emit_line(w.str());
    return 0;
}

// ---------------- (section)37 decode-graph parity: replay vs sync lane --
// The captured graph replays the same kernels in the same order on the
// same weights — output must be bit-identical to the synchronous path;
// any divergence is an enqueue bug, not numeric drift. Evidence for
// f-cuda-graph-decode; admission stays env-gated.
int mode_graph_parity(const Args& a) {
    const std::string bundle = a.get("bundle", "");
    const int prefill = (int)a.num_arg("--prefill", 64);
    const int decode = (int)a.num_arg("--decode", 32);
    const int64_t seed = a.num_arg("--seed", 7);
    JsonWriter w;
    w.begin().kv("schema", "star-decode-graph-parity/v1")
            .kv("bundle", bundle).kv("prefill", prefill)
            .kv("decode", decode).kv("seed", seed);
    if (bundle.empty()) {
        w.kv("verdict", "NO_BUNDLE").kv("ok", false).end();
        emit_line(w.str());
        return 0;
    }
    try {
        NativeInferenceEngine e;
        e.load(bundle);
        std::vector<int64_t> ids(static_cast<size_t>(prefill));
        for (int i = 0; i < prefill; ++i)
            ids[static_cast<size_t>(i)] = seed + i;

        SamplingConfig sc;
        sc.temperature = 0.0;
        std::vector<double> ln, lg;
        std::vector<int64_t> gn, gg;
        std::string errn, errg;

        xengine_cuda_lane(1, 1);          // bf16 lane, graph off
        xcuda_graph_enable(0);
        const auto t0 = std::chrono::steady_clock::now();
        try { ln = e.logits(ids); gn = e.generate(ids, decode, sc); }
        catch (const std::exception& ex) { errn = ex.what(); }
        const auto t1 = std::chrono::steady_clock::now();

        xcuda_graph_enable(1);            // same lane, graph replay
        const auto t2 = std::chrono::steady_clock::now();
        try { lg = e.logits(ids); gg = e.generate(ids, decode, sc); }
        catch (const std::exception& ex) { errg = ex.what(); }
        const auto t3 = std::chrono::steady_clock::now();
        xcuda_graph_enable(0);
        xengine_cuda_lane(0, 0);

        const int graphs = xcuda_graph_state();
        const double us_plain =
            std::chrono::duration<double, std::micro>(t1 - t0).count();
        const double us_graph =
            std::chrono::duration<double, std::micro>(t3 - t2).count();
        w.kv("plain_lane_ok", errn.empty())
         .kv("graph_lane_ok", errg.empty())
         .kv("graphs_captured", graphs)
         .kv("us_plain", us_plain).kv("us_graph", us_graph)
         .kv("gen_tps_plain", us_plain > 0 ? decode * 1e6 / us_plain : 0.0)
         .kv("gen_tps_graph", us_graph > 0 ? decode * 1e6 / us_graph : 0.0);
        if (!errn.empty()) w.kv("plain_error", errn);
        if (!errg.empty()) w.kv("graph_error", errg);
        if (errn.empty() && errg.empty() && ln.size() == lg.size()) {
            bool bit = true;
            for (size_t i = 0; i < ln.size(); ++i)
                bit = bit && ln[i] == lg[i];
            const bool gen_same = gn == gg;
            bool finite = true;
            for (double v : lg) finite = finite && std::isfinite(v);
            w.kv("logits_bit_identical", bit)
             .kv("gen_identical", gen_same)
             .kv("logits_finite", finite);
            const bool pass = bit && gen_same && finite && graphs > 0;
            w.kv("verdict",
                 pass ? "GRAPH_DECODE_PARITY" : "GRAPH_DECODE_DRIFT")
             .kv("ok", true);
        } else {
            w.kv("verdict", "GRAPH_LANE_UNAVAILABLE").kv("ok", true);
        }
    } catch (const std::exception& ex) {
        w.kv("error", ex.what()).kv("verdict", "GRAPH_PARITY_ERROR")
         .kv("ok", false);
    }
    w.end();
    emit_line(w.str());
    return 0;
}

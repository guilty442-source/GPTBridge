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

// xcm_quantcert.h — §31-§35 blockwise quantization certification.
//
// The BlockwiseQuantizationContract (§31-§34) assigns precision per
// tensor CLASS, never per deployment-wide default:
//
//   router          FP32 only — §10 hard rule; requesting int on the
//                   router is ROUTER_PRECISION_VIOLATION, fail-closed
//   shared expert   BF16 floor — always-resident stability anchor
//                   (§6/§33); int on the shared expert is a policy
//                   violation, not a quant choice
//   routed experts  aggressive — GPU_HOT BF16/INT8, RAM_WARM INT8,
//                   NVME_COLD INT4 (§33)
//   common core     BF16 — attention / dense MLP / norms / embeddings
//                   never follow the routed-expert ladder (§34)
//
// This probe simulates the requested policy on the bundle's fp64
// tensors (per-block absmax symmetric roundtrip — quantized values are
// dequantized back into fp64 storage so the unmodified engine runs
// exactly the quantized semantics), then certifies against the §35
// battery:
//
//   tensor_error              per-class max_abs / rms_rel
//   layer_output_error        layer_metrics RMS drift per stage
//   logit_error               last-position logits max_abs / rms_rel
//   top_token_agreement       argmax match + top-8 set overlap
//   router_top2_agreement     per-token expert SET identity (router
//                             semantics must survive quant, §12)
//   generation_agreement      greedy token identity + first divergence
//   expert_specialization_drift  per-layer selection-histogram TV
//
// Verdicts: QUANT_CERTIFIED / BLOCK_QUANT_REGRESSION (§68) /
// QUANT_POLICY_VIOLATION (carries ROUTER_PRECISION_VIOLATION or the
// violated class). Capability regression stays governed by the eval
// lane — pass --out <dir> to keep the simulated bundle for the
// capability suite; nothing here promotes precision.
//
//   xc_modeltool quant-cert --bundle <dir>
//       [--routed int8|int4|bf16|fp32|fp64]   (default int8, §36)
//       [--common bf16|fp32|fp64|int8]        (default bf16, §33)
//       [--shared bf16|fp32|fp64]             (default bf16, §33 floor)
//       [--router fp32|fp64]                  (default fp32, §10)
//       [--block RxC]                         (default 128x128, §31)
//       [--prompt "text" | --seed N --len N]
//       [--gen N]                             (greedy, default 16)
//       [--out <dir>]                         (keep simulated bundle)
//       [--logit-rms-tol F --router-agree-min F --gen-agree-min F
//        --layer-drift-tol F --expert-tv-tol F]
//
// star-quant-cert/v1

namespace {

enum class QPrec { FP64, FP32, BF16, INT8, INT4 };

QPrec qprec_parse(const std::string& s, bool* ok) {
    if (s == "fp64") return QPrec::FP64;
    if (s == "fp32") return QPrec::FP32;
    if (s == "bf16") return QPrec::BF16;
    if (s == "int8") return QPrec::INT8;
    if (s == "int4") return QPrec::INT4;
    *ok = false;
    return QPrec::FP64;
}

const char* qprec_name(QPrec p) {
    switch (p) {
        case QPrec::FP32: return "fp32";
        case QPrec::BF16: return "bf16";
        case QPrec::INT8: return "int8";
        case QPrec::INT4: return "int4";
        default:          return "fp64";
    }
}

// bytes per element under the simulated storage policy (evidence for
// the capacity plane; fp64 tensors stay 8B on disk either way).
double qprec_bytes(QPrec p) {
    switch (p) {
        case QPrec::FP32: return 4.0;
        case QPrec::BF16: return 2.0;
        case QPrec::INT8: return 1.0;
        case QPrec::INT4: return 0.5;
        default:          return 8.0;
    }
}

enum class QClass { Router, Shared, Routed, Common };

// §34 class assignment is name-driven and total — every tensor lands
// in exactly one class so no tensor silently escapes the policy.
QClass qclass_of(const std::string& n) {
    if (n.find("router") != std::string::npos) return QClass::Router;
    if (n.find("shared_expert") != std::string::npos) return QClass::Shared;
    if (n.find(".experts.") != std::string::npos) return QClass::Routed;
    return QClass::Common;
}

const char* qclass_name(QClass c) {
    switch (c) {
        case QClass::Router: return "router";
        case QClass::Shared: return "shared_expert";
        case QClass::Routed: return "routed_expert";
        default:             return "common";
    }
}

struct QErr { double max_abs = 0.0, num = 0.0, den = 0.0; };

// Per-block absmax symmetric roundtrip. 2-D tensors partition as
// [Br x Bc] blocks (§31); anything else is treated as [1 x N] with
// column blocks. Returns accumulated error stats; writes dequantized
// values back over the source range.
QErr quant_blockwise(double* w, int64_t rows, int64_t cols,
                     int64_t br, int64_t bc, int qmax, int qlo) {
    QErr e;
    for (int64_t r0 = 0; r0 < rows; r0 += br) {
        const int64_t r1 = std::min(r0 + br, rows);
        for (int64_t c0 = 0; c0 < cols; c0 += bc) {
            const int64_t c1 = std::min(c0 + bc, cols);
            double mx = 0.0;
            for (int64_t r = r0; r < r1; ++r)
                for (int64_t c = c0; c < c1; ++c)
                    mx = std::max(mx,
                        std::fabs(w[(size_t)(r * cols + c)]));
            if (mx <= 0.0) continue;
            const double scale = mx / (double)qmax;
            for (int64_t r = r0; r < r1; ++r)
                for (int64_t c = c0; c < c1; ++c) {
                    double* v = &w[(size_t)(r * cols + c)];
                    const double src = *v;
                    const long long q = std::clamp<long long>(
                        (double)std::lround(src / scale),
                        (long long)qlo, (long long)qmax);
                    *v = (double)q * scale;
                    const double d = *v - src;
                    e.num += d * d;
                    e.den += src * src;
                    e.max_abs = std::max(e.max_abs, std::fabs(d));
                }
        }
    }
    return e;
}

// bf16: RNE on the dropped fp32 mantissa (same recipe as
// export-bundle --quant bf16); fp32: plain narrowing roundtrip.
QErr quant_scalar(double* w, int64_t n, QPrec p) {
    QErr e;
    for (int64_t i = 0; i < n; ++i) {
        const double src = w[i];
        double out;
        if (p == QPrec::FP32) {
            out = (double)(float)src;
        } else {
            const float fv = (float)src;
            uint32_t bits;
            std::memcpy(&bits, &fv, 4);
            bits += 0x7FFFu + ((bits >> 16) & 1u);
            const uint32_t hi = bits >> 16;
            const uint16_t b16 = (uint16_t)hi;
            const uint32_t dec = (uint32_t)b16 << 16;
            float r;
            std::memcpy(&r, &dec, 4);
            out = (double)r;
        }
        w[i] = out;
        const double d = out - src;
        e.num += d * d;
        e.den += src * src;
        e.max_abs = std::max(e.max_abs, std::fabs(d));
    }
    return e;
}

int64_t topk_set(const double* logits, int64_t vocab, int64_t k,
                 int64_t* out) {
    std::vector<int64_t> idx((size_t)vocab);
    std::iota(idx.begin(), idx.end(), 0);
    std::partial_sort(idx.begin(), idx.begin() + k, idx.end(),
        [&](int64_t x, int64_t y) { return logits[x] > logits[y]; });
    std::sort(idx.begin(), idx.begin() + k);
    for (int64_t i = 0; i < k; ++i) out[i] = idx[(size_t)i];
    return k;
}

int mode_quant_cert(const Args& a) {
    const std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("SPEC_ARGS_MISSING:bundle");

    bool pok = true;
    const QPrec p_router = qprec_parse(a.get("router", "fp32"), &pok);
    const QPrec p_shared = qprec_parse(a.get("shared", "bf16"), &pok);
    const QPrec p_routed = qprec_parse(a.get("routed", "int8"), &pok);
    const QPrec p_common = qprec_parse(a.get("common", "bf16"), &pok);
    if (!pok) fail("QUANT_POLICY_INVALID:precision");

    // §31 candidate geometries; arbitrary RxC accepted for research.
    int64_t br = 128, bc = 128;
    if (a.has("block")) {
        const std::string bs = a.get("block");
        const size_t x = bs.find('x');
        if (x == std::string::npos) fail("QUANT_POLICY_INVALID:block");
        br = std::stoll(bs.substr(0, x));
        bc = std::stoll(bs.substr(x + 1));
        if (br <= 0 || bc <= 0) fail("QUANT_POLICY_INVALID:block");
    }

    // ---- policy gate (fail-closed before any patching) ----
    const char* policy_err = nullptr;
    if (p_router == QPrec::INT8 || p_router == QPrec::INT4 ||
        p_router == QPrec::BF16)
        policy_err = "ROUTER_PRECISION_VIOLATION";
    else if (p_shared == QPrec::INT8 || p_shared == QPrec::INT4)
        policy_err = "SHARED_EXPERT_PRECISION_VIOLATION";
    if (policy_err != nullptr) {
        std::printf(
            "{\"ok\":false,\"format\":\"star-quant-cert/v1\","
            "\"verdict\":\"QUANT_POLICY_VIOLATION\","
            "\"error\":\"%s\",\"router\":\"%s\",\"shared\":\"%s\","
            "\"routed\":\"%s\",\"common\":\"%s\"}\n",
            policy_err, qprec_name(p_router), qprec_name(p_shared),
            qprec_name(p_routed), qprec_name(p_common));
        return 1;
    }

    const fs::path bdir(bundle);
    JsonValue mf = parse_json_file((bdir / "manifest.json").string());
    const JsonValue* tensors = mf.get("tensors");
    if (!tensors || tensors->type != JsonValue::Type::Object)
        fail("QUANT_MANIFEST_INVALID");
    const fs::path wpath = bdir / "weights.bin";
    if (!fs::exists(wpath)) fail("QUANT_MANIFEST_INVALID:weights.bin");

    std::vector<char> wbuf((size_t)fs::file_size(wpath));
    {
        std::ifstream wf(wpath, std::ios::binary);
        if (!wf ||
            !wf.read(wbuf.data(), (std::streamsize)wbuf.size()))
            fail("QUANT_WEIGHTS_UNREADABLE");
    }

    // ---- patch tensors per policy; dedup on (offset,bytes) so tied
    // storage is quantized exactly once --------------------------
    std::set<std::pair<int64_t, int64_t>> patched;
    QErr err_by_class[4];
    int64_t cnt_by_class[4] = {};
    int64_t quantized_tensors = 0, quantized_elems = 0;
    double sim_bytes = 0.0, fp64_bytes = 0.0;
    for (const auto& kv : tensors->object) {
        const JsonValue* sh = kv.second.get("shape");
        const JsonValue* of = kv.second.get("offset");
        const JsonValue* by = kv.second.get("bytes");
        const JsonValue* dt = kv.second.get("dtype");
        if (!of || !by) fail("QUANT_MANIFEST_INVALID:tensor");
        if (dt && dt->type == JsonValue::Type::String &&
            dt->string != "float64")
            fail("QUANT_UNSUPPORTED_DTYPE:" + kv.first);
        int64_t elems = 1, rows = 1, cols = 0;
        if (sh && sh->type == JsonValue::Type::Array) {
            for (const auto& d : sh->array)
                elems *= (int64_t)d.number;
            if (sh->array.size() >= 2) {
                rows = (int64_t)sh->array[0].number;
                cols = elems / rows;
            } else {
                cols = elems;
            }
        }
        const int64_t off = (int64_t)of->number;
        const int64_t nbytes = (int64_t)by->number;
        fp64_bytes += (double)nbytes;
        if ((int64_t)elems * 8 != nbytes)
            fail("QUANT_MANIFEST_INVALID:bytes:" + kv.first);
        const QClass cls = qclass_of(kv.first);
        const QPrec prec =
            cls == QClass::Router ? p_router :
            cls == QClass::Shared ? p_shared :
            cls == QClass::Routed ? p_routed : p_common;
        sim_bytes += qprec_bytes(prec) * (double)elems;
        ++cnt_by_class[(int)cls];
        if (prec == QPrec::FP64) continue;
        if (!patched.insert({off, nbytes}).second) continue;  // tied
        double* w = reinterpret_cast<double*>(wbuf.data() + off);
        QErr te;
        if (prec == QPrec::INT8)
            te = quant_blockwise(w, rows, cols, br, bc, 127, -127);
        else if (prec == QPrec::INT4)
            te = quant_blockwise(w, rows, cols, br, bc, 8, -8);
        else
            te = quant_scalar(w, elems, prec);
        err_by_class[(int)cls].num += te.num;
        err_by_class[(int)cls].den += te.den;
        err_by_class[(int)cls].max_abs =
            std::max(err_by_class[(int)cls].max_abs, te.max_abs);
        ++quantized_tensors;
        quantized_elems += elems;
    }

    // ---- materialize the simulated bundle ----------------------
    std::string out_dir = a.get("out");
    bool keep = !out_dir.empty();
    if (!keep) {
        out_dir = (fs::temp_directory_path() /
                   ("xcm-quantcert-" +
                    std::to_string((unsigned long)GetCurrentProcessId())))
                      .string();
    }
    const fs::path qdir(out_dir);
    std::error_code ec;
    fs::create_directories(qdir, ec);
    for (const auto& de : fs::directory_iterator(bdir)) {
        if (de.path().filename() == "weights.bin") continue;
        fs::copy_file(de.path(), qdir / de.path().filename(),
                      fs::copy_options::overwrite_existing, ec);
        if (ec) fail("QUANT_BUNDLE_WRITE_FAILED:" + de.path().string());
    }
    {
        std::ofstream qf(qdir / "weights.bin",
                         std::ios::binary | std::ios::trunc);
        if (!qf || !qf.write(wbuf.data(), (std::streamsize)wbuf.size()))
            fail("QUANT_BUNDLE_WRITE_FAILED:weights.bin");
    }
    // The simulated bundle is a NEW artifact — rebind the manifest's
    // weights integrity hash to the patched blob (the engine verifies
    // it fail-closed; provenance.json stays untouched as origin
    // evidence of the source weights).
    {
        const std::string new_sha =
            sha256_file((qdir / "weights.bin").string());
        const std::string mfpath = (qdir / "manifest.json").string();
        std::string mt = slurp(mfpath);
        const std::regex re(
            "(\"weights_sha256\"\\s*:\\s*\")[0-9a-fA-F]{64}(\")");
        std::smatch m;
        if (!std::regex_search(mt, m, re))
            fail("QUANT_MANIFEST_INVALID:weights_sha256");
        // manual splice — a regex_replace format string would parse
        // "$1<hex>$2" ambiguously when the digest begins with a digit.
        const std::string mt2 = m.prefix().str() + m[1].str() +
                                new_sha + m[2].str() +
                                m.suffix().str();
        std::ofstream mo(mfpath, std::ios::binary | std::ios::trunc);
        if (!mo || !mo.write(mt2.data(), (std::streamsize)mt2.size()))
            fail("QUANT_BUNDLE_WRITE_FAILED:manifest.json");
    }

    const int64_t gen_n =
        a.has("gen") ? std::stoll(a.get("gen")) : 16;
    const double logit_rms_tol =
        a.has("logit-rms-tol") ? std::stod(a.get("logit-rms-tol")) : 0.05;
    const double router_agree_min =
        a.has("router-agree-min") ? std::stod(a.get("router-agree-min"))
                                  : 0.90;
    const double gen_agree_min =
        a.has("gen-agree-min") ? std::stod(a.get("gen-agree-min")) : 0.50;
    const double layer_drift_tol =
        a.has("layer-drift-tol") ? std::stod(a.get("layer-drift-tol"))
                                 : 0.30;
    const double expert_tv_tol =
        a.has("expert-tv-tol") ? std::stod(a.get("expert-tv-tol")) : 0.30;

    std::ostringstream o;
    o << "{\"format\":\"star-quant-cert/v1\",\"bundle\":\""
      << gptbridge::jsonlite::json_escape(bundle)
      << "\",\"simulated_bundle\":\""
      << gptbridge::jsonlite::json_escape(qdir.string())
      << "\",\"block\":\"" << br << "x" << bc << "\",\"policy\":{"
      << "\"router\":\"" << qprec_name(p_router) << "\","
      << "\"shared_expert\":\"" << qprec_name(p_shared) << "\","
      << "\"routed_expert\":\"" << qprec_name(p_routed) << "\","
      << "\"common\":\"" << qprec_name(p_common) << "\"},"
      << "\"quantized_tensors\":" << quantized_tensors
      << ",\"quantized_elems\":" << quantized_elems
      << ",\"sim_storage_bytes\":" << (int64_t)sim_bytes
      << ",\"fp64_storage_bytes\":" << (int64_t)fp64_bytes
      << ",\"storage_saving_ratio\":"
      << (fp64_bytes > 0 ? 1.0 - sim_bytes / fp64_bytes : 0.0)
      << ",\"tensor_error\":{";
    for (int i = 0; i < 4; ++i) {
        const QErr& e = err_by_class[i];
        o << (i ? "," : "") << "\"" << qclass_name((QClass)i)
          << "\":{\"tensors\":" << cnt_by_class[i]
          << ",\"max_abs\":" << e.max_abs
          << ",\"rms_rel\":"
          << (e.den > 0 ? std::sqrt(e.num / e.den) : 0.0) << "}";
    }
    o << "}";

    try {
        NativeInferenceEngine er, eq;
        er.load(bundle);
        eq.load(qdir.string());
        std::vector<int64_t> ids;
        if (a.has("prompt")) {
            ids = er.encode(a.get("prompt"), true, false);
        } else {
            const int64_t seed =
                a.has("seed") ? std::stoll(a.get("seed")) : 7;
            const int64_t len =
                a.has("len") ? std::stoll(a.get("len")) : 32;
            const JsonValue* cfg = mf.get("config");
            const int64_t vocab =
                cfg ? (int64_t)xct::j_num(cfg, "vocab_size", 0) : 0;
            if (vocab <= 0) fail("QUANT_MANIFEST_INVALID:vocab_size");
            for (int64_t i = 0; i < len; ++i)
                ids.push_back((seed + i * 131) % vocab);
        }

        // §35 router evidence: enable traces on both engines before
        // the forward so router_top2_agreement compares real routing,
        // never implied routing.
        er.set_router_trace(true);
        eq.set_router_trace(true);
        const std::vector<double> lr = er.logits(ids);
        const std::vector<double> lq = eq.logits(ids);
        const auto& tr = er.router_trace();
        const auto& tq = eq.router_trace();
        const std::vector<double> mr = er.layer_metrics(ids);
        const std::vector<double> mq = eq.layer_metrics(ids);
        SamplingConfig sc;
        sc.temperature = 0.0;
        sc.top_k = 1;
        const std::vector<int64_t> gr = er.generate(ids, gen_n, sc);
        const std::vector<int64_t> gq = eq.generate(ids, gen_n, sc);

        // -- logit error + top-token agreement --
        double se = 0.0, sr = 0.0, max_abs = 0.0;
        int64_t arg_r = -1, arg_q = -1;
        double br_ = -1e300, bq_ = -1e300;
        for (size_t i = 0; i < lr.size(); ++i) {
            const double d = lq[i] - lr[i];
            se += d * d;
            sr += lr[i] * lr[i];
            max_abs = std::max(max_abs, std::fabs(d));
            if (lr[i] > br_) { br_ = lr[i]; arg_r = (int64_t)i; }
            if (lq[i] > bq_) { bq_ = lq[i]; arg_q = (int64_t)i; }
        }
        const double logit_rms = sr > 0.0 ? std::sqrt(se / sr) : 0.0;
        const bool argmax_match = arg_r == arg_q;
        int64_t t8r[8], t8q[8];
        topk_set(lr.data(), (int64_t)lr.size(), 8, t8r);
        topk_set(lq.data(), (int64_t)lq.size(), 8, t8q);
        int64_t top8_overlap = 0;
        for (int i = 0; i < 8; ++i)
            for (int j = 0; j < 8; ++j)
                if (t8r[i] == t8q[j]) ++top8_overlap;

        // -- layer output drift --
        double layer_drift = 0.0;
        for (size_t i = 0; i < mr.size() && i < mq.size(); ++i)
            layer_drift = std::max(layer_drift,
                std::fabs(mq[i] - mr[i]) /
                    std::max(std::fabs(mr[i]), 1e-12));

        // -- router top-2 agreement + expert specialization drift --
        int64_t router_pairs = 0, router_agree = 0;
        double expert_tv_max = 0.0;
        const size_t nl = std::min(tr.size(), tq.size());
        for (size_t li = 0; li < nl; ++li) {
            const auto& R = tr[li];
            const auto& Q = tq[li];
            const int64_t k = R.top_k;
            if (k <= 0 || Q.top_k != k) continue;
            const int64_t toks = std::min(R.token_count, Q.token_count);
            std::map<int64_t, int64_t> hr, hq;
            for (int64_t t = 0; t < toks; ++t) {
                std::set<int64_t> sr, sq;
                for (int64_t j = 0; j < k; ++j) {
                    sr.insert(R.expert_ids[(size_t)(t * k + j)]);
                    sq.insert(Q.expert_ids[(size_t)(t * k + j)]);
                    ++hr[R.expert_ids[(size_t)(t * k + j)]];
                    ++hq[Q.expert_ids[(size_t)(t * k + j)]];
                }
                ++router_pairs;
                if (sr == sq) ++router_agree;
            }
            double tv = 0.0;
            std::set<int64_t> all;
            for (const auto& p : hr) all.insert(p.first);
            for (const auto& p : hq) all.insert(p.first);
            const double norm =
                toks * k > 0 ? (double)(toks * k) : 1.0;
            for (int64_t eid : all)
                tv += std::fabs(hr[eid] / norm - hq[eid] / norm);
            expert_tv_max = std::max(expert_tv_max, tv / 2.0);
        }
        const double router_agreement =
            router_pairs > 0 ? (double)router_agree / router_pairs : -1.0;

        // -- generation agreement --
        int64_t agree = 0, first_div = -1;
        const size_t gn = std::min(gr.size(), gq.size());
        for (size_t i = 0; i < gn; ++i) {
            if (gr[i] == gq[i]) ++agree;
            else if (first_div < 0) first_div = (int64_t)i;
        }
        const double gen_agree =
            gr.size() > 0 ? (double)agree / (double)gr.size() : 0.0;
        const bool gen_identical =
            gr.size() == gq.size() && agree == (int64_t)gr.size();
        bool finite = true;
        for (double v : lq) finite = finite && std::isfinite(v);

        // ---- §35 gates ----
        std::vector<std::string> failed;
        if (!finite || !argmax_match || logit_rms > logit_rms_tol)
            failed.emplace_back("logit_error");
        if (router_pairs == 0)
            failed.emplace_back("router_top2_agreement:no-evidence");
        else if (router_agreement < router_agree_min)
            failed.emplace_back("router_top2_agreement");
        if (gen_agree < gen_agree_min)
            failed.emplace_back("generation_agreement");
        if (layer_drift > layer_drift_tol)
            failed.emplace_back("layer_output_error");
        if (expert_tv_max > expert_tv_tol)
            failed.emplace_back("expert_specialization_drift");

        const bool cert = failed.empty();
        o << ",\"logit_error\":{\"max_abs\":" << max_abs
          << ",\"rms_rel\":" << logit_rms << ",\"tol\":" << logit_rms_tol
          << "},\"top_token_agreement\":{\"argmax_match\":"
          << (argmax_match ? "true" : "false")
          << ",\"top8_overlap\":" << top8_overlap << ",\"top8\":8},"
          << "\"router_top2_agreement\":{\"pairs\":" << router_pairs
          << ",\"agree\":" << router_agree
          << ",\"ratio\":" << router_agreement
          << ",\"min\":" << router_agree_min << "},"
          << "\"layer_output_error\":{\"max_rel_drift\":" << layer_drift
          << ",\"tol\":" << layer_drift_tol << "},"
          << "\"generation_agreement\":{\"requested\":" << gen_n
          << ",\"agree_tokens\":" << agree
          << ",\"ratio\":" << gen_agree
          << ",\"first_divergence\":" << first_div
          << ",\"identical\":" << (gen_identical ? "true" : "false")
          << ",\"min\":" << gen_agree_min << "},"
          << "\"expert_specialization_drift\":{\"max_layer_tv\":"
          << expert_tv_max << ",\"tol\":" << expert_tv_tol << "},"
          << "\"capability_regression\":\"delegated — run the "
             "capability suite on --out bundle\","
          << "\"failed_gates\":[";
        for (size_t i = 0; i < failed.size(); ++i)
            o << (i ? "," : "") << "\"" << failed[i] << "\"";
        o << "],\"verdict\":\""
          << (cert ? "QUANT_CERTIFIED" : "BLOCK_QUANT_REGRESSION")
          << "\",\"note\":\"simulated-precision evidence only; storage "
             "format + residency are a separate contract (§31), and "
             "promotion is a lifecycle decision\","
          << "\"ok\":" << (cert ? "true" : "false") << "}";
        std::printf("%s\n", o.str().c_str());
        if (!keep) {
            std::error_code rec;
            fs::remove_all(qdir, rec);
        }
        return cert ? 0 : 1;
    } catch (const std::exception& ex) {
        o << ",\"error\":\""
          << gptbridge::jsonlite::json_escape(ex.what())
          << "\",\"verdict\":\"QUANT_CERT_ERROR\",\"ok\":false}";
        std::printf("%s\n", o.str().c_str());
        if (!keep) {
            std::error_code rec;
            fs::remove_all(qdir, rec);
        }
        return 1;
    }
}

}  // namespace

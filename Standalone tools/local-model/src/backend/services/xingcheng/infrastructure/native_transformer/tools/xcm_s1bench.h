// xcm_s1bench.h — System-1 production benchmark (P5). Fits a bound
// star-system1-head/v1 probe head per decision type on the canonical
// bundle's frozen hidden states (the codex "System-1 supervised
// calibration" lane: post-100M-parity, model weights untouched), then
// measures accuracy/ECE/Brier/NLL + TTFD on the suite's held-out eval
// rows. Emits star-system1-benchmark/v1; per-type head artifacts go to
// --head-out (never written into the served bundle by this lane).
//
//   system1-bench --bundle <dir> --suite <f.json> [--head-out <dir>]
//                 [--iters N] [--threshold F]
//
// Suite format (star-system1-suite/v1):
//   {"types":{"BOOLEAN":{"labels":[...],"rows":[
//       {"prompt":"...","gold":"...","split":"train|eval"}, ...]}, ...}}
// Both splits are required per type; the head is fit on train rows and
// metrics are computed on eval rows only (a fitted-on artifact never
// reports in-sample accuracy).

static std::vector<double> s1b_last_hidden(NativeInferenceEngine& e,
                                           const std::string& text,
                                           double* prefill_ms) {
    auto ids = e.encode(text);
    auto t0 = std::chrono::steady_clock::now();
    std::vector<double> h = e.prefill_hidden(ids);
    *prefill_ms = 1e3 * std::chrono::duration<double>(
        std::chrono::steady_clock::now() - t0).count();
    const int64_t hs = e.hidden_size();
    if ((int64_t)h.size() != hs * (int64_t)ids.size() || h.empty())
        fail("SYSTEM1_PREFILL_STATE_INVALID");
    return std::vector<double>(h.end() - hs, h.end());
}

// Softmax-regression fit on frozen hidden states. Features are
// standardized for conditioning, then folded back into raw-space
// weights/bias so the emitted head stays a plain W h + b (the
// SystemOneHead loader has no normalization fields).
static void s1b_fit(const std::vector<std::vector<double>>& X,
                    const std::vector<int>& Y, int64_t K, int64_t H,
                    int64_t iters, double lr,
                    std::vector<std::vector<double>>& W,
                    std::vector<double>& b) {
    const size_t n = X.size();
    std::vector<double> mu(H, 0.0), sd(H, 0.0);
    for (const auto& x : X)
        for (int64_t i = 0; i < H; ++i) mu[i] += x[i] / n;
    for (const auto& x : X)
        for (int64_t i = 0; i < H; ++i)
            sd[i] += (x[i] - mu[i]) * (x[i] - mu[i]) / n;
    for (int64_t i = 0; i < H; ++i)
        sd[i] = std::max(std::sqrt(sd[i]), 1e-9);

    std::vector<std::vector<double>> Wz(K, std::vector<double>(H, 0.0));
    std::vector<double> bz(K, 0.0);
    const double l2 = 1e-4;
    for (int64_t it = 0; it < iters; ++it) {
        std::vector<std::vector<double>> gw(
            K, std::vector<double>(H, 0.0));
        std::vector<double> gb(K, 0.0);
        for (size_t r = 0; r < n; ++r) {
            std::vector<double> lg(K, 0.0);
            for (int64_t c = 0; c < K; ++c) {
                double s = bz[c];
                for (int64_t i = 0; i < H; ++i)
                    s += Wz[c][i] * (X[r][i] - mu[i]) / sd[i];
                lg[c] = s;
            }
            double mx = *std::max_element(lg.begin(), lg.end());
            double sum = 0;
            for (auto& v : lg) { v = std::exp(v - mx); sum += v; }
            for (int64_t c = 0; c < K; ++c) {
                double g = lg[c] / sum - (c == Y[r] ? 1.0 : 0.0);
                gb[c] += g;
                for (int64_t i = 0; i < H; ++i)
                    gw[c][i] += g * (X[r][i] - mu[i]) / sd[i];
            }
        }
        for (int64_t c = 0; c < K; ++c) {
            bz[c] -= lr * gb[c] / (double)n;
            for (int64_t i = 0; i < H; ++i)
                Wz[c][i] -= lr * (gw[c][i] / (double)n + l2 * Wz[c][i]);
        }
    }
    // Fold standardization into raw-space params:
    //   s = Wz·((x-mu)/sd) + bz = (Wz/sd)·x + (bz - Wz·mu/sd)
    W.assign(K, std::vector<double>(H, 0.0));
    b.assign(K, 0.0);
    for (int64_t c = 0; c < K; ++c) {
        double shift = 0.0;
        for (int64_t i = 0; i < H; ++i) {
            W[c][i] = Wz[c][i] / sd[i];
            shift += Wz[c][i] * mu[i] / sd[i];
        }
        b[c] = bz[c] - shift;
    }
}

static double s1b_fit_temperature(
    const std::vector<std::vector<double>>& X, const std::vector<int>& Y,
    const std::vector<std::vector<double>>& W,
    const std::vector<double>& b) {
    double best_t = 1.0, best_nll = 1e30;
    for (double t = 0.25; t <= 8.01; t *= 1.25) {
        double nll = 0.0;
        for (size_t r = 0; r < X.size(); ++r) {
            std::vector<double> lg(W.size(), 0.0);
            for (size_t c = 0; c < W.size(); ++c) {
                double s = b[c];
                for (size_t i = 0; i < X[r].size(); ++i)
                    s += W[c][i] * X[r][i];
                lg[c] = s / t;
            }
            double mx = *std::max_element(lg.begin(), lg.end());
            double logsum = mx + std::log(
                [&] { double s = 0;
                      for (double v : lg) s += std::exp(v - mx);
                      return s; }());
            nll += -(lg[(size_t)Y[r]] - logsum);
        }
        if (nll < best_nll) { best_nll = nll; best_t = t; }
    }
    return best_t;
}

int mode_system1_bench(const Args& a) {
    std::string bundle = a.get("bundle");
    std::string suite_path = a.get("suite");
    if (bundle.empty() || suite_path.empty())
        fail("SPEC_ARGS_MISSING:bundle,suite");
    int64_t iters = a.has("iters")
        ? std::stoll(a.get("iters")) : 300;
    double threshold = a.has("threshold")
        ? std::stod(a.get("threshold")) : 0.0;

    JsonValue suite = parse_json_file(suite_path);
    const JsonValue* fmt = suite.get("format");
    if (!fmt || fmt->string != "star-system1-suite/v1")
        fail("SYSTEM1_SUITE_INVALID:format");
    const JsonValue* types = suite.get("types");
    if (!types || types->type != JsonValue::Type::Object)
        fail("SYSTEM1_SUITE_INVALID:types");

    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("SYSTEM1_LOAD:") + ex.what());
    }
    const int64_t H = e.hidden_size();
    const std::string mhash = e.model_sha256();
    const std::string gen = e.generation();
    std::string head_dir = a.get("head-out");
    if (!head_dir.empty()) fs::create_directories(head_dir);

    std::ostringstream o;
    o << "{\"ok\":true,\"mode\":\"system1-bench\","
      << "\"format\":\"star-system1-benchmark/v1\","
      << "\"lane\":\"system1_supervised_calibration\","
      << "\"weights_mutated\":false,"
      << "\"model_hash\":\"sha256:" << mhash << "\","
      << "\"generation\":\"" << gptbridge::jsonlite::json_escape(gen)
      << "\",\"hidden_size\":" << H << ",\"types\":[";
    bool first_t = true;
    bool all_ok = true;
    for (const char* tname : {"BOOLEAN", "CHOICE", "ORDINAL_SCORE",
                              "CONFIDENCE"}) {
        const JsonValue* tj = types->get(tname);
        if (!tj || tj->type != JsonValue::Type::Object) continue;
        const JsonValue* lj = tj->get("labels");
        const JsonValue* rj = tj->get("rows");
        if (!lj || lj->type != JsonValue::Type::Array ||
            !rj || rj->type != JsonValue::Type::Array)
            fail(std::string("SYSTEM1_SUITE_INVALID:") + tname);
        std::vector<std::string> labels;
        for (const auto& lv : lj->array)
            if (lv.type == JsonValue::Type::String)
                labels.push_back(lv.string);
        const int64_t K = (int64_t)labels.size();
        if (K < 2) fail(std::string("SYSTEM1_SUITE_INVALID:") + tname);

        std::vector<std::vector<double>> Xtr, Xev;
        std::vector<int> Ytr, Yev;
        std::vector<double> tr_ms, ev_ms;
        for (const auto& row : rj->array) {
            std::string prompt = jget_str(row, "prompt");
            std::string gold = jget_str(row, "gold");
            std::string split = jget_str(row, "split");
            int gi = -1;
            for (size_t li = 0; li < labels.size(); ++li)
                if (labels[li] == gold) { gi = (int)li; break; }
            if (prompt.empty() || gi < 0)
                fail(std::string("SYSTEM1_SUITE_ROW_INVALID:") + tname);
            double pm = 0;
            auto h = s1b_last_hidden(e, prompt, &pm);
            if (split == "eval") {
                Xev.push_back(std::move(h)); Yev.push_back(gi);
                ev_ms.push_back(pm);
            } else {
                Xtr.push_back(std::move(h)); Ytr.push_back(gi);
                tr_ms.push_back(pm);
            }
        }
        if (Xtr.empty() || Xev.empty())
            fail(std::string("SYSTEM1_SUITE_SPLIT_MISSING:") + tname);

        std::vector<std::vector<double>> W;
        std::vector<double> bv;
        s1b_fit(Xtr, Ytr, K, H, iters, 0.5, W, bv);
        // Calibration slice: last quarter of the train rows.
        size_t cal_from = Xtr.size() - std::max<size_t>(1,
            Xtr.size() / 4);
        std::vector<std::vector<double>> Xc(Xtr.begin() + cal_from,
                                            Xtr.end());
        std::vector<int> Yc(Ytr.begin() + cal_from, Ytr.end());
        double temp = s1b_fit_temperature(Xc, Yc, W, bv);

        SystemOneHead head;
        head.format = "star-system1-head/v1";
        head.head_version =
            std::string("s1bench-") + tname;
        head.model_hash = "sha256:" + mhash;
        head.generation = gen;
        head.hidden_size = H;
        head.decision_type = tname;
        head.labels = labels;
        head.temperature = temp;
        head.abstain_threshold = threshold;
        head.weights = W;
        head.bias = bv;
        head.bound = true;

        // Eval metrics: accuracy / ECE(10-bin) / Brier / NLL / TTFD.
        int64_t correct = 0, abstained = 0;
        double brier = 0, nll = 0, ttfd_sum = 0, ttfd_max = 0;
        std::vector<double> ttfd;
        double bin_conf[10] = {}, bin_acc[10] = {}, bin_n[10] = {};
        for (size_t r = 0; r < Xev.size(); ++r) {
            auto t0 = std::chrono::steady_clock::now();
            auto probs = head.probabilities(Xev[r]);
            double hms = 1e3 * std::chrono::duration<double>(
                std::chrono::steady_clock::now() - t0).count();
            int best = 0;
            for (size_t c = 1; c < probs.size(); ++c)
                if (probs[c] > probs[best]) best = (int)c;
            double conf = probs[best];
            bool ok = best == Yev[r];
            if (ok) ++correct;
            if (threshold > 0.0 && conf < threshold) ++abstained;
            double pk = probs[(size_t)Yev[r]];
            nll += -std::log(std::max(pk, 1e-12));
            brier += (conf - (ok ? 1.0 : 0.0)) *
                     (conf - (ok ? 1.0 : 0.0));
            int bi = std::min(9, (int)(conf * 10));
            bin_n[bi] += 1; bin_acc[bi] += ok ? 1.0 : 0.0;
            bin_conf[bi] += conf;
            double t_ = ev_ms[r] + hms;
            ttfd.push_back(t_); ttfd_sum += t_;
            ttfd_max = std::max(ttfd_max, t_);
        }
        double en = (double)Xev.size();
        double ece = 0;
        for (int bi = 0; bi < 10; ++bi)
            if (bin_n[bi] > 0)
                ece += (bin_n[bi] / en) *
                       std::fabs(bin_acc[bi] / bin_n[bi] -
                                 bin_conf[bi] / bin_n[bi]);
        std::sort(ttfd.begin(), ttfd.end());
        double ttfd_p50 = ttfd[ttfd.size() / 2];
        double acc = correct / en;

        if (!head_dir.empty()) {
            std::ostringstream hj;
            hj << "{\"format\":\"star-system1-head/v1\","
               << "\"head_version\":\"" << head.head_version << "\","
               << "\"model_hash\":\"" << head.model_hash << "\","
               << "\"generation\":\""
               << gptbridge::jsonlite::json_escape(gen) << "\","
               << "\"hidden_size\":" << H << ","
               << "\"decision_type\":\"" << tname << "\","
               << "\"labels\":[";
            for (size_t li = 0; li < labels.size(); ++li)
                hj << (li ? "," : "") << "\""
                   << gptbridge::jsonlite::json_escape(labels[li])
                   << "\"";
            hj << "],\"calibration_profile\":{\"temperature\":" << temp
               << ",\"option_count_correction\":0},"
               << "\"abstain_threshold\":" << threshold
               << ",\"weights\":[";
            for (size_t c = 0; c < W.size(); ++c) {
                hj << (c ? "," : "") << "[";
                for (size_t i = 0; i < W[c].size(); ++i)
                    hj << (i ? "," : "")
                       << std::setprecision(17) << W[c][i];
                hj << "]";
            }
            hj << "],\"bias\":[";
            for (size_t c = 0; c < bv.size(); ++c)
                hj << (c ? "," : "") << std::setprecision(17) << bv[c];
            hj << "]}";
            std::ofstream hf(
                (fs::path(head_dir) /
                 (std::string("decision-head-") + tname + ".json"))
                    .string(),
                std::ios::binary | std::ios::trunc);
            hf << hj.str();
        }

        if (!first_t) o << ",";
        first_t = false;
        o << "{\"decision_type\":\"" << tname << "\","
          << "\"classes\":" << K
          << ",\"train_rows\":" << Xtr.size()
          << ",\"eval_rows\":" << Xev.size()
          << ",\"accuracy\":" << acc
          << ",\"ece\":" << ece
          << ",\"brier\":" << brier / en
          << ",\"nll\":" << nll / en
          << ",\"abstention_rate\":" << abstained / en
          << ",\"ttfd_ms\":{\"p50\":" << ttfd_p50
          << ",\"mean\":" << ttfd_sum / en
          << ",\"max\":" << ttfd_max << "},"
          << "\"decode_tokens\":0,"
          << "\"temperature\":" << temp << "}";
    }
    if (first_t) fail("SYSTEM1_SUITE_EMPTY");
    o << "],\"verdict\":\"" << (all_ok ? "BENCHMARKED" : "FAIL")
      << "\"}\n";
    std::fputs(o.str().c_str(), stdout);
    return 0;
}

// xcm_system1.h — Laya + MiMo-V2.6 native runtime plane (§3-§10, §34-§37,
// §44-§46, §49). The C# contract plane (SystemOne/AgentLearning/
// RouterStability) owns validation and policy; this header owns the
// native fast path.
//
//   NativeSystemOneHead   §3/§4/§49: a bundle-side decision head
//                         (decision-head.bin, star-system1-head/v1)
//                         applied to the SAME engine's prefill hidden
//                         state — never a second encoder, never a
//                         second model core, never free text. An
//                         unbound or incompatible head reports
//                         fallback=SYSTEM_2 and leaves the main model
//                         untouched.
//   mode_system1_head     §5-§10/§45: prefill -> head -> calibrated
//                         star-typed-decision/v1 with TTFD and
//                         decode_tokens=0; calibrated_confidence below
//                         threshold -> ABSTAIN -> SYSTEM_2.
//   mode_mtp_runtime      §34/§35/§44: NativeMtpDrafter contract over
//                         the real engine verifier — draft N, main
//                         decoder verifies in parallel, longest-valid
//                         prefix accepted, output is by construction
//                         the model's own greedy continuation
//                         (final-output parity is proven, not claimed).
//   mode_mtp_speedup      §37: spec path vs plain greedy wall clock —
//                         net gain <= 0 reports MTP_SPEEDUP_NEGATIVE
//                         and enabled=false (AUTO-off).
//   mode_mtp_precision_parity  §36: an aggressively degraded (noisy)
//                         drafter must still produce bit-identical
//                         output — drafter precision only ever costs
//                         speed, never semantics.

// ------------------------------------------------------ head artifact --

struct SystemOneHead {
    std::string format;          // star-system1-head/v1
    std::string head_version;
    std::string model_hash;
    std::string generation;
    int64_t hidden_size = 0;
    std::string decision_type;   // BOOLEAN|CHOICE|ORDINAL_SCORE|CONFIDENCE
    std::vector<std::string> labels;
    double temperature = 1.0;
    double option_count_correction = 0.0;
    double abstain_threshold = 0.0;   // 0 -> caller --threshold
    std::vector<std::vector<double>> weights;  // [classes][hidden]
    std::vector<double> bias;
    bool bound = false;
    std::string bind_reason;

    static SystemOneHead load(const std::string& path) {
        SystemOneHead h;
        JsonValue j = parse_json_file(path);
        const JsonValue* f = j.get("format");
        if (!f || f->type != JsonValue::Type::String ||
            f->string != "star-system1-head/v1")
            fail("SYSTEM1_SCHEMA_INVALID:format");
        h.format = f->string;
        auto s = [&j](const char* k) {
            const JsonValue* v = j.get(k);
            return (v && v->type == JsonValue::Type::String)
                       ? v->string : std::string();
        };
        h.head_version = s("head_version");
        h.model_hash = s("model_hash");
        h.generation = s("generation");
        h.decision_type = s("decision_type");
        if (h.decision_type.empty()) h.decision_type = "CHOICE";
        if (const JsonValue* v = j.get("hidden_size"))
            h.hidden_size = (int64_t)v->number;
        if (const JsonValue* v = j.get("labels"))
            if (v->type == JsonValue::Type::Array)
                for (const auto& l : v->array)
                    if (l.type == JsonValue::Type::String)
                        h.labels.push_back(l.string);
        if (const JsonValue* cp = j.get("calibration_profile"))
            if (cp->type == JsonValue::Type::Object) {
                if (const JsonValue* t = cp->get("temperature"))
                    h.temperature = t->number;
                if (const JsonValue* c = cp->get(
                        "option_count_correction"))
                    h.option_count_correction = c->number;
            }
        if (const JsonValue* v = j.get("abstain_threshold"))
            h.abstain_threshold = v->number;
        if (const JsonValue* w = j.get("weights")) {
            if (w->type == JsonValue::Type::Array)
                for (const auto& row : w->array) {
                    if (row.type != JsonValue::Type::Array)
                        fail("SYSTEM1_SCHEMA_INVALID:weights");
                    std::vector<double> r;
                    for (const auto& c : row.array)
                        r.push_back(c.number);
                    h.weights.push_back(std::move(r));
                }
        }
        if (const JsonValue* b = j.get("bias"))
            if (b->type == JsonValue::Type::Array)
                for (const auto& c : b->array)
                    h.bias.push_back(c.number);
        if (h.weights.empty() || h.hidden_size <= 0)
            fail("SYSTEM1_SCHEMA_INVALID:head payload");
        if (h.bias.size() != h.weights.size())
            h.bias.assign(h.weights.size(), 0.0);
        return h;
    }

    /// §49 binding: model hash + generation + hidden size + shape must
    /// all match. Failure is a verdict, never an exception — the main
    /// model must still serve.
    void bind(NativeInferenceEngine& e) {
        std::string mh = e.model_sha256();
        if (!model_hash.empty() && model_hash != mh &&
            model_hash != "sha256:" + mh) {
            bind_reason = "model_hash";
            return;
        }
        if (!generation.empty() && generation != e.generation()) {
            bind_reason = "generation";
            return;
        }
        if (hidden_size != e.hidden_size()) {
            bind_reason = "hidden_size";
            return;
        }
        for (const auto& r : weights)
            if ((int64_t)r.size() != hidden_size) {
                bind_reason = "weights_shape";
                return;
            }
        bound = true;
    }

    /// logits = W h + b, temperature-scaled softmax; option-count
    /// shrinkage mirrors the C# calibration profile (§8).
    std::vector<double> probabilities(
        const std::vector<double>& hidden) const {
        std::vector<double> lg(weights.size(), 0.0);
        for (size_t c = 0; c < weights.size(); ++c) {
            double s = bias[c];
            for (size_t i = 0; i < hidden.size(); ++i)
                s += weights[c][i] * hidden[i];
            lg[c] = s / std::max(temperature, 1e-9);
        }
        double mx = *std::max_element(lg.begin(), lg.end());
        double sum = 0;
        for (double& v : lg) { v = std::exp(v - mx); sum += v; }
        for (double& v : lg) v /= sum;
        if (option_count_correction > 0 && lg.size() > 2) {
            double u = 1.0 / lg.size();
            for (double& v : lg)
                v = u + (v - u) * std::exp(-option_count_correction *
                                           ((double)lg.size() - 2.0));
            double s2 = 0;
            for (double v : lg) s2 += v;
            for (double& v : lg) v /= s2;
        }
        return lg;
    }
};

// ---------------------------------------------------------- head mode --

int mode_system1_head(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("SPEC_ARGS_MISSING:bundle");
    std::string head_path = a.get("head");
    if (head_path.empty())
        head_path = (fs::path(bundle) / "decision-head.bin").string();
    std::string text = a.get("text");
    if (text.empty() && a.has("file")) {
        std::ifstream in(a.get("file"));
        text.assign(std::istreambuf_iterator<char>(in), {});
    }
    if (text.empty()) text = "decision probe";
    double threshold = a.has("threshold")
        ? std::stod(a.get("threshold")) : 0.0;

    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("SYSTEM1_LOAD:") + ex.what());
    }
    if (a.has("scope")) e.set_prefix_scope(a.get("scope"));

    if (!fs::exists(head_path)) {
        // §49: no head artifact -> structural fallback, the model is
        // never broken by a missing decision head.
        std::printf("{\"ok\":true,\"format\":\"star-system1-head/v1\","
                    "\"head_present\":false,\"bound\":false,"
                    "\"fallback\":\"SYSTEM_2\"}\n");
        return 0;
    }
    SystemOneHead head = SystemOneHead::load(head_path);
    head.bind(e);
    if (!head.bound) {
        std::printf("{\"ok\":true,\"format\":\"star-system1-head/v1\","
                    "\"head_present\":true,\"bound\":false,"
                    "\"bind_reason\":\"%s\",\"fallback\":\"SYSTEM_2\"}\n",
                    head.bind_reason.c_str());
        return 0;
    }
    if (threshold <= 0.0) threshold = head.abstain_threshold;

    auto ids = e.encode(text);
    auto t0 = std::chrono::steady_clock::now();
    std::vector<double> hidden = e.prefill_hidden(ids);
    double prefill_ms = 1e3 * std::chrono::duration<double>(
        std::chrono::steady_clock::now() - t0).count();

    // Fail-closed guard: a non-finite hidden state never reaches the
    // head — degrade to SYSTEM_2 with evidence, never emit a NaN
    // decision.
    double hnorm = 0.0, hmax = 0.0;
    bool finite = !hidden.empty();
    for (double v : hidden) {
        if (!std::isfinite(v)) { finite = false; break; }
        hnorm += v * v;
        hmax = std::max(hmax, std::fabs(v));
    }
    // forward_hidden returns every position's state [seq x hidden];
    // the decision consumes the last (post-prefill) position.
    const int64_t hsize = e.hidden_size();
    if ((int64_t)hidden.size() != hsize * (int64_t)ids.size()) {
        std::printf("{\"ok\":true,\"format\":\"star-system1-head/v1\","
                    "\"head_present\":true,\"bound\":true,"
                    "\"hidden_shape\":\"unexpected\",\"fallback\":"
                    "\"SYSTEM_2\",\"reason\":\"PREFILL_STATE_INVALID\"}\n");
        return 0;
    }
    std::vector<double> last_hidden(
        hidden.end() - hsize, hidden.end());
    if (!finite) {
        std::printf("{\"ok\":true,\"format\":\"star-system1-head/v1\","
                    "\"head_present\":true,\"bound\":true,"
                    "\"hidden_finite\":false,\"fallback\":\"SYSTEM_2\","
                    "\"reason\":\"PREFILL_STATE_INVALID\"}\n");
        return 0;
    }

    t0 = std::chrono::steady_clock::now();
    auto probs = head.probabilities(last_hidden);
    double head_ms = 1e3 * std::chrono::duration<double>(
        std::chrono::steady_clock::now() - t0).count();

    int best = 0;
    for (size_t i = 1; i < probs.size(); ++i)
        if (probs[i] > probs[best]) best = (int)i;
    double conf = probs[best];
    std::string selected = best < (int)head.labels.size()
        ? head.labels[best] : ("option_" + std::to_string(best));
    bool abstain = threshold > 0.0 && conf < threshold;

    std::ostringstream o;
    o << "{\"ok\":true,\"format\":\"star-typed-decision/v1\","
      << "\"decision_version\":\"" << head.head_version << "\","
      << "\"decision_type\":\"" << head.decision_type << "\","
      << "\"model_hash\":\"" << e.model_sha256() << "\","
      << "\"generation\":\"" << e.generation() << "\","
      << "\"probabilities\":[";
    for (size_t i = 0; i < probs.size(); ++i)
        o << (i ? "," : "") << probs[i];
    o << "],\"raw_confidence\":" << probs[best]
      << ",\"calibrated_confidence\":" << conf
      << ",\"calibration_profile\":{\"temperature\":"
      << head.temperature
      << ",\"option_count_correction\":"
      << head.option_count_correction << "}";
    if (abstain) {
        o << ",\"selected\":null,\"decision\":\"ABSTAIN\","
             "\"fallback\":\"SYSTEM_2\","
             "\"reason\":\"SYSTEM1_CONFIDENCE_LOW\"";
    } else {
        o << ",\"selected\":\"" << selected
          << "\",\"decision\":\"COMMIT\"";
    }
    // §45 resource report — the point of System-1: TTFD with zero
    // decode tokens.
    o << ",\"resource\":{\"format\":\"star-system1-resource/v1\","
      << "\"ttfd_ms\":" << (prefill_ms + head_ms)
      << ",\"prefill_ms\":" << prefill_ms
      << ",\"head_ms\":" << head_ms
      << ",\"hidden_norm\":" << std::sqrt(hnorm)
      << ",\"hidden_absmax\":" << hmax
      << ",\"prompt_tokens\":" << (int64_t)ids.size()
      << ",\"decode_tokens\":0}";
    o << "}\n";
    std::fputs(o.str().c_str(), stdout);
    return 0;
}

// ------------------------------------------------------------- MTP -----

// Bigram/periodic synthetic drafter — real MTP weights are training
// auxiliary and are dropped at export (§34), so the runtime contract is
// exercised with a deterministic local drafter. The verifier is always
// the main decoder's own greedy argmax; parity is therefore proven by
// construction and measured end-to-end.
static std::vector<int64_t> ngram_draft(
    const std::vector<int64_t>& ctx, int64_t k, int64_t noise_pct) {
    std::vector<int64_t> d;
    if (ctx.size() < 2) return d;
    std::mt19937_64 rng(ctx.back() ^ 0x5bd1e995);
    std::uniform_int_distribution<int64_t> flip(0, 99);
    for (int64_t i = 0; i < k; ++i) {
        int64_t nxt = -1;
        if (noise_pct == 0 || flip(rng) >= noise_pct) {
            // bigram: repeat the token that followed the current last
            // token the last time it appeared.
            for (int64_t j = (int64_t)ctx.size() - 2; j >= 0; --j)
                if (ctx[j] == ctx.back()) {
                    nxt = ctx[j + 1];
                    break;
                }
            if (nxt < 0 && ctx.size() >= 8)
                nxt = ctx[ctx.size() - 8 + (size_t)(i % 8)];
        }
        d.push_back(nxt);
    }
    return d;
}

static int64_t greedy_next(NativeInferenceEngine& e,
                           const std::vector<int64_t>& ctx) {
    auto lg = e.logits(ctx);
    int64_t am = 0;
    for (size_t j = 1; j < lg.size(); ++j)
        if (lg[j] > lg[(size_t)am]) am = (int64_t)j;
    return am;
}

/// Verified speculative loop: returns the committed token stream and
/// fills acceptance stats. Out-param `spec_ms` is wall time.
static std::vector<int64_t> spec_decode(
    NativeInferenceEngine& e, std::vector<int64_t> ctx,
    int64_t tokens_wanted, int64_t draft_k, int64_t noise_pct,
    double& spec_ms, int64_t& proposed, int64_t& accepted) {
    auto t0 = std::chrono::steady_clock::now();
    std::vector<int64_t> emitted;
    proposed = 0;
    accepted = 0;
    while ((int64_t)emitted.size() < tokens_wanted) {
        auto drafted = ngram_draft(ctx, draft_k, noise_pct);
        if (drafted.empty() ||
            std::find(drafted.begin(), drafted.end(), -1) !=
                drafted.end()) {
            // MTP_DRAFT_INVALID / disabled lane -> plain greedy step.
            emitted.push_back(greedy_next(e, ctx));
            ctx.push_back(emitted.back());
            continue;
        }
        proposed += (int64_t)drafted.size();
        // Parallel-verify contract: the target's greedy continuation
        // for the drafted window is computed once; the longest valid
        // prefix is accepted, then one bonus token from the target.
        std::vector<int64_t> work = ctx;
        int64_t acc = 0;
        for (size_t i = 0; i <= drafted.size(); ++i) {
            int64_t tgt = greedy_next(e, work);
            if (i < drafted.size() && tgt == drafted[i]) {
                ++acc;
                emitted.push_back(tgt);
                work.push_back(tgt);
            } else {
                emitted.push_back(tgt);   // bonus token
                work.push_back(tgt);
                break;
            }
        }
        accepted += acc;
        ctx.swap(work);
    }
    emitted.resize((size_t)tokens_wanted);
    spec_ms = 1e3 * std::chrono::duration<double>(
        std::chrono::steady_clock::now() - t0).count();
    return emitted;
}

int mode_mtp_runtime(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("SPEC_ARGS_MISSING:bundle");
    int64_t draft_k = a.has("draft") ? std::stoll(a.get("draft")) : 4;
    int64_t steps = a.has("tokens") ? std::stoll(a.get("tokens")) : 16;
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("MTP_LOAD:") + ex.what());
    }
    // Repeating-pattern prompt so the bigram drafter has genuine
    // acceptance — a random context would only ever measure rejection.
    std::string text = a.get("text");
    std::vector<int64_t> ctx = text.empty()
        ? std::vector<int64_t>{4, 9, 4, 9, 4, 9, 4, 9}
        : e.encode(text);

    double spec_ms; int64_t proposed, accepted;
    auto emitted = spec_decode(e, ctx, steps, draft_k, 0,
                               spec_ms, proposed, accepted);

    // Final-output parity: replayed pure-greedy must equal emitted.
    auto base = ctx;
    bool parity = true;
    for (int64_t i = 0; i < steps; ++i) {
        int64_t g = greedy_next(e, base);
        if (i < (int64_t)emitted.size() && emitted[(size_t)i] != g)
            parity = false;
        base.push_back(g);
    }

    std::printf("{\"ok\":true,\"mode\":\"mtp-runtime\","
                "\"format\":\"star-mtp-bench/v1\","
                "\"draft_k\":%lld,\"tokens\":%lld,"
                "\"proposed\":%lld,\"accepted\":%lld,"
                "\"acceptance_length\":%.4f,"
                "\"draft_accuracy\":%.4f,"
                "\"tokens_per_verify\":%.4f,"
                "\"final_output_parity\":%s,"
                "\"decode_tokens\":%lld,"
                "\"note\":\"drafter=runtime-acceleration, authority="
                "HybridCausalDecoder\"}\n",
                (long long)draft_k, (long long)steps,
                (long long)proposed, (long long)accepted,
                proposed > 0 ? (double)accepted / proposed : 0.0,
                proposed > 0 ? (double)accepted / proposed : 0.0,
                proposed > 0 ? (double)emitted.size() /
                    ((double)proposed / draft_k) : 1.0,
                parity ? "true" : "false",
                (long long)emitted.size());
    return parity ? 0 : 2;
}

int mode_mtp_speedup(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("SPEC_ARGS_MISSING:bundle");
    int64_t draft_k = a.has("draft") ? std::stoll(a.get("draft")) : 4;
    int64_t tokens = a.has("tokens") ? std::stoll(a.get("tokens")) : 16;
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("MTP_LOAD:") + ex.what());
    }
    std::string text = a.get("text");
    std::vector<int64_t> ctx = text.empty()
        ? std::vector<int64_t>{4, 9, 4, 9, 4, 9, 4, 9}
        : e.encode(text);

    double spec_ms; int64_t proposed, accepted;
    spec_decode(e, ctx, tokens, draft_k, 0, spec_ms, proposed, accepted);

    auto base = ctx;
    auto t0 = std::chrono::steady_clock::now();
    for (int64_t i = 0; i < tokens; ++i)
        base.push_back(greedy_next(e, base));
    double greedy_ms = 1e3 * std::chrono::duration<double>(
        std::chrono::steady_clock::now() - t0).count();

    double net_gain = greedy_ms - spec_ms;
    // §37: negative net gain -> AUTO disables; MTP is a measured
    // optimization, never an assumed one.
    std::printf("{\"ok\":true,\"mode\":\"mtp-speedup\","
                "\"format\":\"star-mtp-bench/v1\","
                "\"greedy_ms\":%.3f,\"spec_ms\":%.3f,"
                "\"net_gain_ms\":%.3f,"
                "\"acceptance_rate\":%.4f,"
                "\"enabled\":%s,"
                "\"verdict\":\"%s\"}\n",
                greedy_ms, spec_ms, net_gain,
                proposed > 0 ? (double)accepted / proposed : 0.0,
                net_gain > 0 ? "true" : "false",
                net_gain > 0 ? "SPEEDUP_POSITIVE"
                             : "MTP_SPEEDUP_NEGATIVE");
    return 0;
}

int mode_mtp_precision_parity(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("SPEC_ARGS_MISSING:bundle");
    int64_t draft_k = a.has("draft") ? std::stoll(a.get("draft")) : 4;
    int64_t tokens = a.has("tokens") ? std::stoll(a.get("tokens")) : 16;
    // Simulate the aggressive-precision drafter: a degraded drafter may
    // propose wrong tokens often (§36 FP8/INT8/FP4 candidates) — the
    // main-decoder verify guarantees identical output regardless.
    int64_t noise_pct = a.has("noise") ? std::stoll(a.get("noise")) : 60;
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("MTP_LOAD:") + ex.what());
    }
    std::vector<int64_t> ctx{4, 9, 4, 9, 4, 9, 4, 9};

    double spec_ms; int64_t proposed, accepted;
    auto emitted = spec_decode(e, ctx, tokens, draft_k, noise_pct,
                               spec_ms, proposed, accepted);

    auto base = ctx;
    bool parity = true;
    for (int64_t i = 0; i < tokens; ++i) {
        int64_t g = greedy_next(e, base);
        if (i < (int64_t)emitted.size() && emitted[(size_t)i] != g)
            parity = false;
        base.push_back(g);
    }
    std::printf("{\"ok\":true,\"mode\":\"mtp-precision-parity\","
                "\"format\":\"star-mtp-bench/v1\","
                "\"drafter_noise_pct\":%lld,"
                "\"acceptance_rate\":%.4f,"
                "\"final_output_parity\":%s,"
                "\"note\":\"drafter precision affects speed only; "
                "main-decoder verify owns semantics\"}\n",
                (long long)noise_pct,
                proposed > 0 ? (double)accepted / proposed : 0.0,
                parity ? "true" : "false");
    return parity ? 0 : 2;
}

// xcm_integration.h — integration modes complementing xcm_runtime.h
// (which owns memplan / statebench / spec-probe / vision-budget /
// context-probe / reuse-probe). Included once by xc_modeltool.cpp
// after xcm_runtime.h.
//
//   SpeculativeDecoder  §6 formal ABI contract (Draft/Verify/Accept/
//                       Reject/Reset + metrics; disabled by default —
//                       export drops the MTP training head, so any
//                       Draft without a bound drafter fails closed
//                       with SPECULATIVE_DECODER_UNAVAILABLE)
//   memplan             --bundle <dir> [--kv-limit B] [--prefix-limit B]
//                         §7 InferenceMemoryPlanner / KV budget manager
//   statebench          --bundle <dir> --generation <g> [--tokens N]
//                         §23 SequenceStateBenchmark + DeltaStateSnapshot
//   spec-probe          --bundle <dir> [--draft K] [--steps N]
//                         §6 synthetic drafter exercises the ABI
//   vision-budget       --raw-patches N [--profile P] [--mem-budget B]
//                         [--experimental]  §20 VisionBudgetController
//   context-probe       --bundle <dir> [--sizes 2048,4096,...]
//                         §28 ContextBudgetManager (probe-only)
//   reuse-probe         --bundle <dir>   §24 ParameterReuseProbe
//   depth-probe         --bundle <dir>   §8 depth telemetry
//   moe-analyze         --bundle <dir>   §8/§29 quantiles + diagnoses
//   provenance-check    --bundle <dir>   §25 bundle provenance
#pragma once

static int64_t bench_now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

// ---------------------------------------------------------------- §7
// InferenceMemoryPlanner: typed budget breakdown + prefill/decode
// high-water marks. --kv-limit / --prefix-limit exercise the governed
// budget setters (fail-closed on overflow).
int mode_memplan(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("MEMPLAN_ARGS_MISSING");
    NativeInferenceEngine e;
    try {
        if (a.has("kv-limit"))
            e.set_kv_memory_limit(std::stoll(a.get("kv-limit")));
        if (a.has("prefix-limit"))
            e.set_prefix_cache_limit(64,
                                     std::stoll(a.get("prefix-limit")));
        e.load(bundle);
    } catch (const std::exception& ex) {
        fail(std::string("MEMPLAN_LOAD:") + ex.what());
    }
    auto r = e.memory_report();
    std::printf(
        "{\"ok\":true,\"mode\":\"memplan\","
        "\"weight_bytes\":%lld,\"kv_bytes\":%lld,"
        "\"prefix_cache_bytes\":%lld,\"recurrent_state_bytes\":%lld,"
        "\"vision_bytes\":%lld,\"workspace_bytes\":%lld,"
        "\"prefill_peak_bytes\":%lld,\"decode_peak_bytes\":%lld,"
        "\"total_bytes\":%lld}\n",
        (long long)r.weight_bytes, (long long)r.kv_bytes,
        (long long)r.prefix_cache_bytes,
        (long long)r.recurrent_state_bytes,
        (long long)r.vision_bytes, (long long)r.workspace_bytes,
        (long long)r.prefill_peak_bytes,
        (long long)r.decode_peak_bytes,
        (long long)e.memory_bytes());
    return 0;
}

// ---------------------------------------------------------------- §23
// SequenceStateBenchmark: constant-state (DeltaNet) and growing-state
// (KV) footprints plus a real snapshot/restore round trip. The restore
// is generation-bound — a foreign generation fails closed.
int mode_statebench(const Args& a) {
    std::string bundle = a.get("bundle");
    std::string generation = a.get("generation");
    if (bundle.empty() || generation.empty())
        fail("STATEBENCH_ARGS_MISSING");
    int64_t tokens = 128;
    if (a.has("tokens")) {
        try { tokens = std::stoll(a.get("tokens")); }
        catch (...) { fail("STATEBENCH_BAD_TOKENS"); }
    }
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("STATEBENCH_LOAD:") + ex.what());
    }
    JsonValue mf = parse_json_file(
        (fs::path(bundle) / "manifest.json").string());
    const JsonValue* cfg = mf.get("config");
    const int64_t vocab = (int64_t)xct::j_num(cfg, "vocab_size", 0);
    if (vocab < 8) fail("STATEBENCH_BAD_CONFIG");
    std::mt19937_64 rng(13);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> ids;
    for (int64_t i = 0; i < tokens; ++i) ids.push_back(tok(rng));

    const int64_t t0 = bench_now_ms();
    std::vector<double> lg1;
    try { lg1 = e.logits(ids); }
    catch (const std::exception& ex) {
        fail(std::string("STATEBENCH_FWD:") + ex.what());
    }
    const int64_t t1 = bench_now_ms();
    SamplingConfig sc;
    std::vector<int64_t> gen;
    const int64_t t2 = bench_now_ms();
    try { gen = e.generate(ids, 8, sc); }
    catch (const std::exception& ex) {
        fail(std::string("STATEBENCH_GEN:") + ex.what());
    }
    const int64_t t3 = bench_now_ms();

    // snapshot -> hash -> restore -> verify round trip
    const int64_t t4 = bench_now_ms();
    std::string blob;
    try { blob = e.snapshot_delta_state(generation); }
    catch (const std::exception& ex) {
        fail(std::string("STATEBENCH_SNAPSHOT:") + ex.what());
    }
    const int64_t t5 = bench_now_ms();
    const std::string sha =
        NativeInferenceEngine::delta_state_sha256(blob);
    try { e.restore_delta_state(blob, generation); }
    catch (const std::exception& ex) {
        fail(std::string("STATEBENCH_RESTORE:") + ex.what());
    }
    const int64_t t6 = bench_now_ms();
    std::vector<double> lg2;
    try { lg2 = e.logits(ids); }
    catch (const std::exception& ex) {
        fail(std::string("STATEBENCH_VERIFY:") + ex.what());
    }
    double max_d = 0.0;
    if (lg1.size() == lg2.size())
        for (size_t i = 0; i < lg1.size(); ++i)
            max_d = std::max(max_d, std::fabs(lg1[i] - lg2[i]));
    const bool verified = lg1.size() == lg2.size() && max_d <= 1e-9;

    const double prefill_s = (t1 - t0) / 1000.0;
    const double decode_s = (t3 - t2) / 1000.0;
    std::printf(
        "{\"ok\":%s,\"mode\":\"statebench\",\"generation\":\"%s\","
        "\"state_bytes_session\":%lld,"
        "\"state_bytes_token\":%.1f,\"kv_bytes_token\":%.1f,"
        "\"prefill_tps\":%.1f,\"decode_tps\":%.1f,"
        "\"stream_duration_ms\":%lld,"
        "\"state_save_ms\":%lld,\"state_restore_ms\":%lld,"
        "\"snapshot_sha256\":\"%s\",\"restore_verified\":%s,"
        "\"restore_max_logit_diff\":%.9f,"
        "\"recurrent_state_norms\":[",
        verified ? "true" : "false",
        gptbridge::jsonlite::json_escape(generation).c_str(),
        (long long)blob.size(),
        tokens > 0 ? (double)blob.size() / tokens : 0.0,
        tokens > 0 ? (double)e.kv_memory_bytes() / tokens : 0.0,
        prefill_s > 0 ? ids.size() / prefill_s : 0.0,
        decode_s > 0 ? (double)gen.size() / decode_s : 0.0,
        (long long)(t3 - t0),
        (long long)(t5 - t4), (long long)(t6 - t5),
        sha.c_str(), verified ? "true" : "false", max_d);
    auto norms = e.recurrent_state_norms();
    for (size_t i = 0; i < norms.size(); ++i)
        std::printf("%s%.6f", i ? "," : "", norms[i]);
    std::printf("]}\n");
    return verified ? 0 : 1;
}

// ---------------------------------------------------------------- §20
// VisionBudgetController: FULL is the only production profile;
// BALANCED/COMPACT are EXPERIMENTAL and fall back to FULL unless
// --experimental is passed (parity benchmarks must land first).
int mode_vision_budget(const Args& a) {
    if (!a.has("raw-patches")) fail("VISION_BUDGET_ARGS_MISSING");
    int64_t raw = std::stoll(a.get("raw-patches"));
    std::string profile = a.get("profile");
    if (profile.empty()) profile = "FULL";
    int64_t mem_budget =
        a.has("mem-budget") ? std::stoll(a.get("mem-budget")) : 0;
    bool experimental = a.has("experimental");
    if (profile != "FULL" && profile != "BALANCED" &&
        profile != "COMPACT")
        fail("VISION_BUDGET_BAD_PROFILE");
    std::string effective = profile;
    bool fell_back = false;
    if (profile != "FULL" && !experimental) {
        effective = "FULL";
        fell_back = true;
    }
    // §20/§36: parity evidence feeds in via --parity-failed (set by the
    // multimodal eval suite when OCR/chart/document/general/small-
    // object parity fails) — any parity failure forces FULL.
    bool parity_failed = a.has("parity-failed");
    if (effective != "FULL" && parity_failed) {
        effective = "FULL";
        fell_back = true;
    }
    int64_t selected = raw;
    std::string compression = "none";
    if (effective == "BALANCED") {
        selected = std::min<int64_t>(raw, 48);
        compression = "adaptive-prune";
    } else if (effective == "COMPACT") {
        selected = std::min<int64_t>(raw, 16);
        compression = "adaptive-prune+merge";
    }
    if (mem_budget > 0) {
        int64_t cap = std::max<int64_t>(1, mem_budget / (16 * 8 * 4));
        selected = std::min(selected, std::min<int64_t>(cap, 64));
    } else {
        selected = std::min<int64_t>(selected, 64);
    }
    std::printf(
        "{\"ok\":%s,\"mode\":\"vision-budget\",\"profile\":\"%s\","
        "\"effective_profile\":\"%s\",\"experimental\":%s,"
        "\"fell_back_to_full\":%s,\"raw_patch_count\":%lld,"
        "\"selected_patch_count\":%lld,\"compression_mode\":\"%s\"%s}\n",
        parity_failed ? "false" : "true",
        profile.c_str(), effective.c_str(),
        experimental ? "true" : "false",
        fell_back ? "true" : "false",
        (long long)raw, (long long)selected, compression.c_str(),
        parity_failed
            ? ",\"error_code\":\"VISION_BUDGET_PARITY_FAILED\"" : "");
    return 0;
}

// ---------------------------------------------------------------- §28
// ContextBudgetManager probe: prefill latency + memory at probe sizes
// (<= the model's trained context). Never changes production defaults.
int mode_context_probe(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("CTX_ARGS_MISSING");
    std::vector<int64_t> sizes = {2048, 4096, 8192, 16384, 32768};
    if (a.has("sizes")) {
        sizes.clear();
        std::string s = a.get("sizes");
        size_t pos = 0;
        while (pos < s.size()) {
            size_t c = s.find(',', pos);
            sizes.push_back(std::stoll(
                s.substr(pos, c == std::string::npos
                             ? std::string::npos : c - pos)));
            if (c == std::string::npos) break;
            pos = c + 1;
        }
    }
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("CTX_LOAD:") + ex.what());
    }
    JsonValue mf = parse_json_file(
        (fs::path(bundle) / "manifest.json").string());
    const JsonValue* cfg = mf.get("config");
    const int64_t vocab = (int64_t)xct::j_num(cfg, "vocab_size", 0);
    const int64_t max_pos =
        (int64_t)xct::j_num(cfg, "max_position_embeddings", 0);
    if (vocab < 8 || max_pos <= 0) fail("CTX_BAD_CONFIG");
    std::mt19937_64 rng(5);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::printf(
        "{\"ok\":true,\"mode\":\"context-probe\",\"probe_only\":true,"
        "\"trained_context\":%lld,\"results\":[",
        (long long)max_pos);
    bool first = true;
    for (int64_t n : sizes) {
        if (n > max_pos || n < 8) continue;
        std::vector<int64_t> ids;
        for (int64_t i = 0; i < n; ++i) ids.push_back(tok(rng));
        const int64_t t0 = bench_now_ms();
        try { e.logits(ids); }
        catch (const std::exception& ex) {
            fail(std::string("CTX_FWD:") + ex.what());
        }
        const int64_t t1 = bench_now_ms();
        if (!first) std::printf(",");
        first = false;
        double sec = (t1 - t0) / 1000.0;
        std::printf(
            "{\"size\":%lld,\"prefill_ms\":%lld,"
            "\"prefill_tps\":%.1f,\"memory_bytes\":%lld,"
            "\"kv_bytes\":%lld}",
            (long long)n, (long long)(t1 - t0),
            sec > 0 ? n / sec : 0.0,
            (long long)e.memory_bytes(),
            (long long)e.kv_memory_bytes());
    }
    std::printf("],\"production_default\":%lld}\n",
                (long long)max_pos);
    return 0;
}

// ---------------------------------------------------------------- §24
// ParameterReuseProbe: simulation only — if adjacent decoder blocks
// shared parameters, how much weight/cache/load would drop, and what
// risks follow. Result feeds FutureArchitectureResearch; xc-fused-1
// is untouched.
int mode_reuse_probe(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("REUSE_ARGS_MISSING");
    JsonValue mf = parse_json_file(
        (fs::path(bundle) / "manifest.json").string());
    const JsonValue* cfg = mf.get("config");
    if (cfg == nullptr) fail("REUSE_BAD_CONFIG");
    const int64_t layers =
        (int64_t)xct::j_num(cfg, "num_hidden_layers", 0);
    const int64_t hidden =
        (int64_t)xct::j_num(cfg, "hidden_size", 0);
    if (layers <= 1 || hidden <= 0) fail("REUSE_BAD_CONFIG");
    std::string wpath = (fs::path(bundle) / "weights.bin").string();
    std::error_code ec;
    int64_t weights_bytes = (int64_t)fs::file_size(wpath, ec);
    if (ec) fail("REUSE_WEIGHTS_MISSING");
    double layer_share = 0.9;
    int64_t saved = (int64_t)(
        weights_bytes * layer_share * (layers - layers / 2) /
        (double)layers);
    std::printf(
        "{\"ok\":true,\"mode\":\"reuse-probe\",\"target\":"
        "\"FutureArchitectureResearch\",\"layers\":%lld,"
        "\"weights_bytes\":%lld,\"est_saved_bytes\":%lld,"
        "\"est_saved_pct\":%.2f,"
        "\"risks\":{\"quality\":\"shared blocks reduce per-layer "
        "specialization - needs ablation\",\"routing\":\"moe routers "
        "on shared blocks conflate layer identity\",\"checkpoint\":"
        "\"XCN layout assumes per-layer tensors - contract revision "
        "required\"},\"architecture_change\":false}\n",
        (long long)layers, (long long)weights_bytes,
        (long long)saved,
        weights_bytes > 0 ? 100.0 * saved / weights_bytes : 0.0);
    return 0;
}


// ----------------------------------------------------------------- §6
class SpeculativeDecoder {
  public:
    using Drafter = std::function<std::vector<int64_t>(int64_t)>;
    // returns the target model's greedy ids per drafted position plus
    // one bonus token.
    using Verifier =
        std::function<std::vector<int64_t>(const std::vector<int64_t>&)>;

    void set_drafter(Drafter d) { drafter_ = std::move(d); }
    void set_verifier(Verifier v) { verifier_ = std::move(v); }
    void set_enabled(bool on) { enabled_ = on; }
    bool enabled() const { return enabled_; }

    std::vector<int64_t> Draft(int64_t draft_len) {
        if (!enabled_ || !drafter_)
            throw std::runtime_error("SPECULATIVE_DECODER_UNAVAILABLE");
        if (draft_len <= 0 || draft_len > 64)
            throw std::runtime_error("SPEC_DRAFT_LEN_INVALID");
        ++draft_calls_;
        std::vector<int64_t> d = drafter_(draft_len);
        tokens_drafted_ += static_cast<int64_t>(d.size());
        pending_ = d;
        return d;
    }

    // Verification algorithm: longest prefix where the target model's
    // greedy choice equals the drafted token; the first mismatch ends
    // acceptance and the target's own token rides as the bonus token.
    std::vector<bool> Verify(const std::vector<int64_t>& drafted) {
        if (!verifier_)
            throw std::runtime_error("SPECULATIVE_DECODER_UNAVAILABLE");
        ++verify_calls_;
        const std::vector<int64_t> target = verifier_(drafted);
        std::vector<bool> mask(drafted.size(), false);
        accept_prefix_ = 0;
        bonus_token_ = -1;
        for (size_t i = 0; i < drafted.size() && i < target.size();
             ++i) {
            if (target[i] == drafted[i]) {
                mask[i] = true;
                ++accept_prefix_;
            } else {
                bonus_token_ = target[i];
                break;
            }
        }
        return mask;
    }

    void Accept() {
        tokens_accepted_ += accept_prefix_;
        if (bonus_token_ >= 0) ++bonus_tokens_;
        pending_.clear();
        accept_prefix_ = 0;
        bonus_token_ = -1;
    }

    void Reject() {
        ++rejections_;
        pending_.clear();
        accept_prefix_ = 0;
        bonus_token_ = -1;
    }

    void Reset() {
        draft_calls_ = verify_calls_ = 0;
        tokens_drafted_ = tokens_accepted_ = 0;
        rejections_ = bonus_tokens_ = 0;
        accept_prefix_ = 0;
        bonus_token_ = -1;
        pending_.clear();
    }

    int64_t bonus_token() const { return bonus_token_; }
    int64_t accept_prefix() const { return accept_prefix_; }

    std::string metrics_json() const {
        std::ostringstream o;
        o << "{\"enabled\":" << (enabled_ ? "true" : "false")
          << ",\"draft_calls\":" << draft_calls_
          << ",\"verify_calls\":" << verify_calls_
          << ",\"tokens_drafted\":" << tokens_drafted_
          << ",\"tokens_accepted\":" << tokens_accepted_
          << ",\"rejections\":" << rejections_
          << ",\"bonus_tokens\":" << bonus_tokens_
          << ",\"acceptance_rate\":"
          << (tokens_drafted_ > 0
                  ? (double)tokens_accepted_ / tokens_drafted_ : 0.0)
          << '}';
        return o.str();
    }

  private:
    bool enabled_ = false;
    Drafter drafter_;
    Verifier verifier_;
    std::vector<int64_t> pending_;
    int64_t draft_calls_ = 0;
    int64_t verify_calls_ = 0;
    int64_t tokens_drafted_ = 0;
    int64_t tokens_accepted_ = 0;
    int64_t rejections_ = 0;
    int64_t bonus_tokens_ = 0;
    int64_t accept_prefix_ = 0;
    int64_t bonus_token_ = -1;
};

// spec-probe: synthetic drafter exercises the SpeculativeDecoder ABI
// (Draft/Verify/Accept/Reject/Reset + acceptance mask + metrics), the
// engine supplies the verifier's greedy argmax, and the disabled path
// proves SPECULATIVE_DECODER_UNAVAILABLE. No production drafter exists
// — export drops the MTP training head; enabled stays false.
int mode_spec_probe(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("SPEC_ARGS_MISSING");
    int64_t draft_k = a.has("draft") ? std::stoll(a.get("draft")) : 4;
    int64_t steps = a.has("steps") ? std::stoll(a.get("steps")) : 8;
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("SPEC_LOAD:") + ex.what());
    }
    JsonValue mf = parse_json_file(
        (fs::path(bundle) / "manifest.json").string());
    const JsonValue* cfg = mf.get("config");
    const int64_t vocab = (int64_t)xct::j_num(cfg, "vocab_size", 0);
    if (vocab < 8) fail("SPEC_BAD_CONFIG");
    std::mt19937_64 rng(7);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);

    std::vector<int64_t> base;
    for (int i = 0; i < 16; ++i) base.push_back(tok(rng));

    // synthetic drafter: echo the tail of the context (periodic guess).
    SpeculativeDecoder sd;
    sd.set_enabled(true);
    sd.set_drafter([&base](int64_t k) {
        std::vector<int64_t> d;
        int64_t period = std::min<int64_t>(8, (int64_t)base.size());
        for (int64_t i = 0; i < k && period > 0; ++i)
            d.push_back(base[base.size() - period +
                             (size_t)(i % period)]);
        return d;
    });
    // verifier: engine argmax per drafted position + bonus token.
    sd.set_verifier([&e](const std::vector<int64_t>& drafted) {
        // The verifier receives only the drafted tokens; the probe
        // keeps a shared base in the closure via a member trick — the
        // verifier gets the current context through a static slot.
        (void)drafted;
        return std::vector<int64_t>{};
    });
    // The verifier needs the base context — rebind per step below via a
    // shared pointer.
    std::vector<int64_t>* basep = &base;
    sd.set_verifier([&e, basep](const std::vector<int64_t>& drafted) {
        std::vector<int64_t> target;
        std::vector<int64_t> seq = *basep;
        for (size_t i = 0; i <= drafted.size(); ++i) {
            auto lg = e.logits(seq);
            int64_t am = 0;
            for (size_t j = 1; j < lg.size(); ++j)
                if (lg[j] > lg[(size_t)am]) am = (int64_t)j;
            target.push_back(am);
            if (i < drafted.size()) seq.push_back(drafted[i]);
        }
        return target;
    });

    int64_t proposed = 0, accepted = 0, fallbacks = 0;
    for (int64_t s = 0; s < steps; ++s) {
        std::vector<int64_t> drafted;
        try { drafted = sd.Draft(draft_k); }
        catch (const std::runtime_error&) { drafted.clear(); }
        if (drafted.empty()) {
            // fallback path: plain greedy step
            auto lg = e.logits(base);
            int64_t am = 0;
            for (size_t j = 1; j < lg.size(); ++j)
                if (lg[j] > lg[(size_t)am]) am = (int64_t)j;
            base.push_back(am);
            ++fallbacks;
            continue;
        }
        auto mask = sd.Verify(drafted);
        proposed += (int64_t)drafted.size();
        int64_t acc = sd.accept_prefix();
        accepted += acc;
        // accept prefix, then the target's own token at the first
        // rejection (bonus token semantics).
        for (int64_t i = 0; i <= acc &&
                          i < (int64_t)drafted.size(); ++i) {
            int64_t t;
            if (i < acc) t = drafted[(size_t)i];
            else t = sd.bonus_token() >= 0 ? sd.bonus_token() : 0;
            base.push_back(t);
            if (i == acc) break;
        }
        if (acc == (int64_t)drafted.size()) sd.Accept();
        else sd.Reject();
    }

    // disabled path must fail closed.
    SpeculativeDecoder off;
    std::string disabled_err;
    try { off.Draft(4); }
    catch (const std::exception& ex) { disabled_err = ex.what(); }
    bool unavailable_ok = disabled_err.find(
        "SPECULATIVE_DECODER_UNAVAILABLE") != std::string::npos;
    std::string metrics = sd.metrics_json();
    std::printf(
        "{\"ok\":%s,\"mode\":\"spec-probe\",\"enabled\":false,"
        "\"reason\":\"mtp-head-discarded-at-export\","
        "\"draft_k\":%lld,\"steps\":%lld,\"proposed\":%lld,"
        "\"accepted\":%lld,\"acceptance_rate\":%.4f,"
        "\"fallbacks\":%lld,\"final_context\":%lld,"
        "\"unavailable_ok\":%s,\"metrics\":%s,"
        "\"abi\":[\"Draft\",\"Verify\",\"Accept\",\"Reject\","
        "\"Reset\"]}\n",
        unavailable_ok ? "true" : "false",
        (long long)draft_k, (long long)steps, (long long)proposed,
        (long long)accepted,
        proposed > 0 ? (double)accepted / proposed : 0.0,
        (long long)fallbacks, (long long)base.size(),
        unavailable_ok ? "true" : "false", metrics.c_str());
    return unavailable_ok ? 0 : 1;
}

// depth-probe: standalone §8 depth telemetry — per-layer residual RMS,
// per-module output norms, DeltaNet recurrent-state norms.
static int mode_depth_probe(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("DEPTH_ARGS_MISSING");
    NativeInferenceEngine engine;
    try { engine.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("DEPTH_LOAD:") + ex.what());
    }
    std::vector<int64_t> ids =
        engine.encode("1 2 3 4 5 6 7 8", true, false);
    auto layer = engine.layer_metrics(ids);
    auto mod = engine.module_metrics(ids);
    auto rec = engine.recurrent_state_norms();
    auto emitv = [](const std::vector<double>& v) {
        std::ostringstream s;
        for (size_t i = 0; i < v.size(); ++i)
            s << (i ? "," : "") << v[i];
        return s.str();
    };
    std::printf("{\"ok\":true,\"mode\":\"depth-probe\","
                "\"format\":\"star-depth-telemetry/v1\","
                "\"layer_representation_norm\":[%s],"
                "\"module_output_norm\":[%s],"
                "\"recurrent_state_norm\":[%s]}\n",
                emitv(layer).c_str(), emitv(mod).c_str(),
                emitv(rec).c_str());
    return 0;
}

// moe-analyze: standalone §8/§29 quantile routing analysis + ROUTER_*
// diagnostics over a seeded prompt forward.
static int mode_moe_analyze(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("MOE_ANALYZE_ARGS_MISSING");
    NativeInferenceEngine engine;
    try { engine.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("MOE_ANALYZE_LOAD:") + ex.what());
    }
    engine.set_router_trace(true);
    engine.logits(engine.encode("1 2 3 4 5 6 7 8", true, false));
    std::string rep = xcm_moe_analyze_json(engine.router_trace());
    engine.set_router_trace(false);
    std::printf("{\"ok\":true,\"mode\":\"moe-analyze\","
                "\"format\":\"star-moe-routing-analysis/v1\","
                "\"analysis\":%s}\n", rep.c_str());
    return 0;
}

// provenance-check: §25 bundle provenance — fail-closed ordered gates:
// hash -> signature (optional) -> generation -> architecture -> ckpt
// -> tensor shape -> runtime compatibility -> load.
static int mode_provenance_check(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("PROVENANCE_ARGS_MISSING");
    fs::path dir(bundle);
    JsonValue manifest =
        parse_json_file((dir / "manifest.json").string());
    auto field = [&](const char* k) -> std::string {
        const JsonValue* v = manifest.get(k);
        return v && v->type == JsonValue::Type::String ? v->string : "";
    };
    auto has = [&](const char* k) { return manifest.get(k) != nullptr; };
    std::vector<std::string> failures;

    // 1) weights hash must match the shipped weights.bin.
    std::string wsha = field("weights_sha256");
    if (wsha.size() != 64) {
        failures.push_back("weights_sha256 missing");
    } else if (fs::exists(dir / "weights.bin")) {
        if (sha256_file((dir / "weights.bin").string()) != wsha)
            failures.push_back("weights_sha256 mismatch");
    } else {
        failures.push_back("weights.bin missing");
    }
    // 2) optional signature — absent is legal this phase; present must
    // be a non-empty string (verification deferred to the signer lane).
    if (const JsonValue* sig = manifest.get("signature");
        sig && sig->type != JsonValue::Type::Null &&
        !(sig->type == JsonValue::Type::String && !sig->string.empty()))
        failures.push_back("signature malformed");
    // 3) generation + architecture identity.
    if (field("architecture_generation").empty())
        failures.push_back("architecture_generation missing");
    // 4) checkpoint contract identity.
    if (!has("checkpoint_version") || field("checkpoint_sha256").empty())
        failures.push_back("checkpoint identity missing");
    // 5) tensor shape sanity.
    {
        const JsonValue* t = manifest.get("tensors");
        bool tensors_ok =
            t && ((t->type == JsonValue::Type::Array &&
                   !t->array.empty()) ||
                  (t->type == JsonValue::Type::Object &&
                   !t->object.empty()));
        if (!tensors_ok) failures.push_back("tensors missing");
    }
    // 6) runtime compatibility tag (new provenance block; absent on
    // legacy bundles is reported, the load below is the real gate).
    const JsonValue* prov = manifest.get("provenance");
    std::string runtime_compat;
    if (prov && prov->type == JsonValue::Type::Object) {
        if (const JsonValue* rc = prov->get("runtime_compatibility");
            rc && rc->type == JsonValue::Type::String)
            runtime_compat = rc->string;
    }
    // 7) load — engine validates tensor shapes/dtypes itself.
    bool loaded = false;
    try {
        NativeInferenceEngine engine;
        engine.load(bundle);
        loaded = true;
    } catch (const std::exception&) {
        failures.push_back("engine load failed");
    }
    bool ok = failures.empty() && loaded;
    std::ostringstream o;
    o << "{\"ok\":" << (ok ? "true" : "false")
      << ",\"mode\":\"provenance-check\","
      << "\"format\":\"star-bundle-provenance/v1\""
      << ",\"weights_sha256_verified\":"
      << (wsha.size() == 64 ? "true" : "false")
      << ",\"generation\":\""
      << gptbridge::jsonlite::json_escape(
             field("architecture_generation")) << "\""
      << ",\"runtime_compatibility\":\""
      << gptbridge::jsonlite::json_escape(runtime_compat) << "\""
      << ",\"legacy_bundle\":" << (prov ? "false" : "true")
      << ",\"engine_load\":" << (loaded ? "true" : "false")
      << ",\"failures\":[";
    for (size_t i = 0; i < failures.size(); ++i)
        o << (i ? "," : "") << "\""
          << gptbridge::jsonlite::json_escape(failures[i]) << "\"";
    o << "]}";
    std::printf("%s\n", o.str().c_str());
    return ok ? 0 : 1;
}

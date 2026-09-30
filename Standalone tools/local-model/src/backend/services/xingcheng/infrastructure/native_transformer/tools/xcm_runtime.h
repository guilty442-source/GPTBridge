// xcm_runtime.h — runtime-capability probe modes for xc_modeltool.
// Included once by xc_modeltool.cpp (after xcm_corpus.h) inside the
// anonymous namespace. Surfaces the engine's planner/state/telemetry
// APIs plus contract-level probes; all are report/probe-only — none
// change model weights, architecture semantics, or production defaults.
//
//   memplan        --bundle <dir> [--kv-limit B] [--prefix-limit B]
//                  §7 InferenceMemoryPlanner / KV budget manager
//   statebench     --bundle <dir> --generation <g> [--tokens N]
//                  §23 SequenceStateBenchmark + DeltaStateSnapshot
//   spec-probe     --bundle <dir> [--draft K] [--steps N]
//                  §6 SpeculativeDecoder ABI + synthetic drafter probe
//   vision-budget  --raw-patches N [--profile FULL|BALANCED|COMPACT]
//                  [--mem-budget B] [--experimental]
//                  §20 VisionBudgetController (EXPERIMENTAL gate)
//   context-probe  --bundle <dir> [--sizes 2048,4096,8192]
//                  §28 ContextBudgetManager (probe-only sizes)
//   reuse-probe    --bundle <dir>
//                  §24 ParameterReuseProbe -> research report only
#pragma once

// -------------------------------------------------------- §8/§29 ----
// MoERoutingAnalyzer: quantile routing analysis over the engine's
// two-level router trace (P01..P99 over per-expert selection counts —
// never just the mean) plus ROUTER_* diagnoses.
static std::string xcm_moe_analyze_json(
    const std::vector<NativeInferenceEngine::RouterLayerTrace>&
        layers) {
    auto esc = [](const std::string& s) {
        return gptbridge::jsonlite::json_escape(s);
    };
    std::ostringstream o;
    o << "{\"layers\":[";
    bool first = true;
    std::set<std::string> router_types;
    for (const auto& tr : layers) {
        router_types.insert(tr.router_type);
        // per-expert selection counts -> quantiles
        std::map<int64_t, int64_t> counts;
        for (int64_t t = 0; t < tr.token_count; ++t)
            for (int64_t k = 0; k < tr.top_k; ++k) {
                int64_t e = tr.expert_ids[(size_t)(t * tr.top_k + k)];
                counts[e]++;
            }
        std::vector<int64_t> vals;
        for (auto& kv : counts) vals.push_back(kv.second);
        std::sort(vals.begin(), vals.end());
        auto q = [&](double p) -> double {
            if (vals.empty()) return 0.0;
            double idx = p * (vals.size() - 1);
            size_t lo = (size_t)idx, hi = std::min(lo + 1, vals.size() - 1);
            return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo);
        };
        double p50 = q(0.50), p99 = q(0.99);
        int64_t total = 0, zero_e = 0, max_e = 0;
        for (int64_t v : vals) {
            total += v; if (v == 0) ++zero_e; max_e = std::max(max_e, v);
        }
        // diagnoses
        std::vector<std::string> diag;
        if (!vals.empty()) {
            if (max_e >= total * 9 / 10 && vals.size() > 1)
                diag.push_back("ROUTER_COLLAPSE");
            if (p50 > 0 && p99 > p50 * 8)
                diag.push_back("ROUTER_HOTSPOT");
            if (zero_e > (int64_t)vals.size() / 2)
                diag.push_back("ROUTER_STARVATION");
            double hhi = 0.0;   // load concentration
            for (int64_t v : vals)
                hhi += (v / (double)std::max<int64_t>(1, total)) *
                       (v / (double)std::max<int64_t>(1, total));
            if (hhi > 0.9 && vals.size() > 2)
                diag.push_back("ROUTER_INSTABILITY");
        }
        if (!first) o << ",";
        first = false;
        o << "{\"layer_id\":" << tr.layer_id
          << ",\"router_type\":\"" << esc(tr.router_type)
          << "\",\"top_k\":" << tr.top_k
          << ",\"token_count\":" << tr.token_count
          << ",\"experts_observed\":" << vals.size()
          << ",\"shared_experts\":" << tr.shared_experts
          << ",\"shared_expert_gated\":"
          << (tr.shared_expert_gated ? "true" : "false")
          << ",\"expert_load_quantiles\":{\"p01\":" << q(0.01)
          << ",\"p05\":" << q(0.05) << ",\"p25\":" << q(0.25)
          << ",\"p50\":" << p50 << ",\"p75\":" << q(0.75)
          << ",\"p95\":" << q(0.95) << ",\"p99\":" << p99 << "}"
          << ",\"diagnoses\":[";
        for (size_t i = 0; i < diag.size(); ++i)
            o << (i ? "," : "") << "\"" << diag[i] << "\"";
        o << "]}";
    }
    o << "],\"router_types\":[";
    bool ft = true;
    for (auto& t : router_types) {
        if (!ft) o << ",";
        ft = false;
        o << "\"" << esc(t) << "\"";
    }
    o << "]}";
    return o.str();
}

// --------------------------------------------------------------- §11
// star-fim/v1 runtime envelope: literal control markers — the
// tokenizer is never modified for FIM.
static std::string xcm_fim_envelope(const std::string& prefix,
                                    const std::string& suffix) {
    return "<|fim_prefix|>" + prefix + "<|fim_suffix|>" + suffix +
           "<|fim_middle|>";
}

static int64_t bench_now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

// ---------------------------------------------------------------- §7
int mode_memplan(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("MEMPLAN_ARGS_MISSING");
    NativeInferenceEngine e;
    try {
        if (a.has("kv-limit"))
            e.set_kv_memory_limit(std::stoll(a.get("kv-limit")));
        if (a.has("prefix-limit")) {
            e.set_prefix_cache_limit(64,
                                     std::stoll(a.get("prefix-limit")));
        }
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
// SequenceStateBenchmark: measures the constant-state (DeltaNet) and
// growing-state (KV) footprints plus a real snapshot/restore round
// trip. generation-bound: --generation must name the bundle's lineage;
// a restore under a foreign generation fails closed.
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
    const std::string sha = NativeInferenceEngine::delta_state_sha256(blob);
    try { e.restore_delta_state(blob, generation); }
    catch (const std::exception& ex) {
        fail(std::string("STATEBENCH_RESTORE:") + ex.what());
    }
    const int64_t t6 = bench_now_ms();
    // verify: same prompt must produce identical logits post-restore
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

// ----------------------------------------------------------------- §6
// SpeculativeDecoder: Draft/Verify/Accept/Reject/Reset ABI exercised by
// a synthetic drafter (periodic-context guesser). enabled=false — the
// training MTP head is discarded at export, so no production drafter
// exists; this probe validates the verification algorithm, acceptance
// mask, metrics and fallback path only.
struct SpeculativeDecoder {
    int64_t period = 8;
    std::vector<int64_t> ctx;
    std::vector<int64_t> pending;
    void reset() { ctx.clear(); pending.clear(); }
    std::vector<int64_t> draft(int64_t k) {
        pending.clear();
        if (ctx.size() >= (size_t)period)
            for (int64_t i = 0; i < k; ++i)
                pending.push_back(
                    ctx[ctx.size() - period +
                        (size_t)(i % period)]);
        return pending;
    }
    // verify: argmax over engine logits after appending drafts
    std::vector<bool> verify(NativeInferenceEngine& e,
                             const std::vector<int64_t>& base,
                             const std::vector<int64_t>& drafted) {
        std::vector<bool> ok;
        if (drafted.empty()) return ok;
        std::vector<int64_t> seq = base;
        for (int64_t d : drafted) {
            auto lg = e.logits(seq);
            int64_t am = 0;
            for (size_t i = 1; i < lg.size(); ++i)
                if (lg[i] > lg[(size_t)am]) am = (int64_t)i;
            ok.push_back(am == d);
            seq.push_back(d);
        }
        return ok;
    }
};

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
    SpeculativeDecoder sd;
    std::vector<int64_t> base;
    for (int i = 0; i < 16; ++i) {
        int64_t t = tok(rng);
        base.push_back(t); sd.ctx.push_back(t);
    }
    int64_t proposed = 0, accepted = 0, fallbacks = 0;
    for (int64_t s = 0; s < steps; ++s) {
        auto drafted = sd.draft(draft_k);
        if (drafted.empty()) {
            // fallback: single-token greedy step
            auto lg = e.logits(base);
            int64_t am = 0;
            for (size_t i = 1; i < lg.size(); ++i)
                if (lg[i] > lg[(size_t)am]) am = (int64_t)i;
            base.push_back(am); sd.ctx.push_back(am);
            ++fallbacks;
            continue;
        }
        auto ok = sd.verify(e, base, drafted);
        proposed += (int64_t)drafted.size();
        int64_t acc = 0;
        for (bool b : ok) { if (b) ++acc; else break; }  // prefix mask
        accepted += acc;
        // accept prefix; first rejection ends the burst
        for (int64_t i = 0; i <= acc && i < (int64_t)drafted.size(); ++i) {
            int64_t t;
            if (i < acc) t = drafted[(size_t)i];
            else {
                // correction token = engine argmax at rejection point
                std::vector<int64_t> seq = base;
                auto lg = e.logits(seq);
                int64_t am = 0;
                for (size_t j = 1; j < lg.size(); ++j)
                    if (lg[j] > lg[(size_t)am]) am = (int64_t)j;
                t = am;
            }
            base.push_back(t); sd.ctx.push_back(t);
            if (i == acc) break;
        }
    }
    std::printf(
        "{\"ok\":true,\"mode\":\"spec-probe\",\"enabled\":false,"
        "\"reason\":\"mtp-head-discarded-at-export\","
        "\"draft_k\":%lld,\"steps\":%lld,\"proposed\":%lld,"
        "\"accepted\":%lld,\"acceptance_rate\":%.4f,"
        "\"fallbacks\":%lld,\"final_context\":%lld,"
        "\"abi\":[\"draft\",\"verify\",\"accept\",\"reject\",\"reset\"]}\n",
        (long long)draft_k, (long long)steps, (long long)proposed,
        (long long)accepted,
        proposed > 0 ? (double)accepted / proposed : 0.0,
        (long long)fallbacks, (long long)base.size());
    return 0;
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
    int64_t selected = raw;
    std::string compression = "none";
    if (effective == "BALANCED") {
        selected = std::min<int64_t>(raw, 48);
        compression = "adaptive-prune";
    } else if (effective == "COMPACT") {
        selected = std::min<int64_t>(raw, 16);
        compression = "adaptive-prune+merge";
    }
    // memory bound: patch row ~ patch_dim floats; hard cap at 64.
    if (mem_budget > 0) {
        int64_t cap = std::max<int64_t>(
            1, mem_budget / (16 * 8 * 4));   // conservative row cost
        selected = std::min(selected, std::min<int64_t>(cap, 64));
    } else {
        selected = std::min<int64_t>(selected, 64);
    }
    std::printf(
        "{\"ok\":true,\"mode\":\"vision-budget\",\"profile\":\"%s\","
        "\"effective_profile\":\"%s\",\"experimental\":%s,"
        "\"fell_back_to_full\":%s,\"raw_patch_count\":%lld,"
        "\"selected_patch_count\":%lld,\"compression_mode\":\"%s\"}\n",
        profile.c_str(), effective.c_str(),
        experimental ? "true" : "false",
        fell_back ? "true" : "false",
        (long long)raw, (long long)selected, compression.c_str());
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
    int64_t weights_bytes = 0;
    std::error_code ec;
    weights_bytes = (int64_t)fs::file_size(wpath, ec);
    if (ec) fail("REUSE_WEIGHTS_MISSING");
    // estimate: non-embedding per-layer weights dominate; sharing pairs
    // halves them. embed ~ vocab*hidden excluded via layer share 0.9.
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
        "specialization — needs ablation\",\"routing\":\"moe routers "
        "on shared blocks conflate layer identity\",\"checkpoint\":"
        "\"XCN layout assumes per-layer tensors — contract revision "
        "required\"},\"architecture_change\":false}\n",
        (long long)layers, (long long)weights_bytes,
        (long long)saved,
        weights_bytes > 0 ? 100.0 * saved / weights_bytes : 0.0);
    return 0;
}

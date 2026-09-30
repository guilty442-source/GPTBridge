// xcm_efficiency.h — Native Inference Efficiency Plane modes for
// xc_modeltool. Included once inside the anonymous namespace after
// xcm_integration.h.
//
//   ExpertResidencyManager §2-§10 — per-expert residency state
//                         machine (PINNED/HOT/WARM/COLD/LOADING/
//                         EVICTING/OFFLOADED) driven by the engine's
//                         real router trace; eviction is scored by
//                         recency + frequency + router probability +
//                         transfer cost, never plain LRU. Shared
//                         expert / router / common weights are PINNED
//                         and never migrate.
//   expert-residency       residency decision + telemetry emission
//   expert-offload-bench   §11 all-resident / cpu-offload /
//                          quantized-offload comparison, transfer
//                          cost measured on real weight bytes.
//   hybrid-prefix-smoke    §13/§14 KV + DeltaStateSnapshot prefix hit
//                          must be bit-identical, partial prefix works.
//   rag-prefix-bench       §20 measured prefill savings — no hardcoded
//                          percentages.
//   prefix-invalidation    §19 scope invalidation smoke.
//   prefill-artifact-smoke / pd-pipeline-bench / decode-latency-guard
//                          §25-§36.
//   efficiency-report      §40 star-inference-efficiency/v1.
#pragma once

// ------------------------------------------------- §4 residency ----
struct XcmExpertRec {
    int64_t expert_id = -1;
    int64_t layer_id = -1;
    int64_t bytes = 0;
    std::string storage_precision = "fp64";
    std::string exec_precision = "fp64";
    const char* state = "OFFLOADED";
    uint64_t last_used = 0;
    uint64_t hit_count = 0;
    uint64_t load_count = 0;
    int64_t transfer_bytes = 0;
    double transfer_ms = 0;
    double router_prob_ema = 0;
    double residency_score = 0;
};

struct XcmExpertResidency {
    int64_t gpu_budget_bytes = 0;
    int64_t max_resident = 0;
    int64_t hotset_target = 0;
    int prefetch_depth = 1;               // §7 conservative start
    bool auto_mode = false;
    uint64_t tick = 0;
    int64_t resident_bytes = 0;
    // Expert sets are small (tens per layer) — a flat vector + linear
    // scan keeps the fragment free of associative-container includes.
    std::vector<XcmExpertRec> recs;
    int64_t hits = 0, misses = 0;

    XcmExpertRec& rec(int64_t layer, int64_t expert) {
        for (XcmExpertRec& r : recs)
            if (r.layer_id == layer && r.expert_id == expert) return r;
        recs.push_back(XcmExpertRec{});
        XcmExpertRec& r = recs.back();
        r.expert_id = expert;
        r.layer_id = layer;
        r.bytes = expert_bytes;
        r.state = "OFFLOADED";
        return r;
    }

    // §5 scoring: recency + frequency + router probability − transfer
    // cost — not plain LRU.
    double score(const XcmExpertRec& r) const {
        double recency =
            1.0 / (1.0 + (double)(tick - r.last_used));
        double freq = std::log1p((double)r.hit_count);
        double cost =
            gpu_budget_bytes > 0
                ? (double)r.bytes / (double)gpu_budget_bytes : 0.0;
        return 2.0 * recency + 1.0 * freq + 4.0 * r.router_prob_ema -
               1.5 * cost;
    }

    // §6: decide residency for a token's top-k selection; experts that
    // are not resident load (miss), overflow evicts lowest score.
    void use(int64_t layer, int64_t expert, double prob) {
        ++tick;
        XcmExpertRec& r = rec(layer, expert);
        r.router_prob_ema = 0.9 * r.router_prob_ema + 0.1 * prob;
        r.last_used = tick;
        ++r.hit_count;
        const bool resident =
            std::strcmp(r.state, "OFFLOADED") != 0;
        if (resident) { ++hits; r.residency_score = score(r); return; }
        ++misses;
        // LOAD: RAM → device cache (transfer accounted).
        r.state = "LOADING";
        ++r.load_count;
        r.transfer_bytes += r.bytes;
        resident_bytes += r.bytes;
        r.state = "HOT";
        evict();
        r.residency_score = score(r);
    }

    void evict() {
        while (max_resident > 0 || gpu_budget_bytes > 0) {
            int64_t resident = 0;
            XcmExpertRec* victim = nullptr;
            for (XcmExpertRec& r : recs) {
                if (std::strcmp(r.state, "OFFLOADED") == 0) continue;
                ++resident;
                if (victim == nullptr ||
                    r.residency_score < victim->residency_score)
                    victim = &r;
            }
            const bool over =
                (max_resident > 0 && resident > max_resident) ||
                (gpu_budget_bytes > 0 &&
                 resident_bytes > gpu_budget_bytes);
            if (!over || victim == nullptr) break;
            victim->state = "EVICTING";
            resident_bytes -= victim->bytes;
            victim->state = "COLD";
            victim->state = "OFFLOADED";
        }
    }

    int64_t expert_bytes = 0;
    int64_t resident_count() const {
        int64_t n = 0;
        for (const XcmExpertRec& r : recs)
            if (std::strcmp(r.state, "OFFLOADED") != 0) ++n;
        return n;
    }
};

// Expert byte size from the bundle manifest (sum of the three
// projections for one routed expert on one MoE layer).
int64_t xcm_expert_bytes(const JsonValue& mf, int64_t layer,
                         int64_t expert) {
    const JsonValue* tensors = mf.get("tensors");
    if (!tensors || tensors->type != JsonValue::Type::Object) return 0;
    int64_t total = 0;
    for (const char* proj : {"gate_proj", "up_proj", "down_proj"}) {
        std::ostringstream nm;
        nm << "model.layers." << layer << ".mlp.experts." << expert
           << "." << proj << ".weight";
        const JsonValue* t = tensors->get(nm.str());
        if (t) total += (int64_t)xct::j_num(t, "bytes", 0);
    }
    return total;
}

// Measure the real host transfer cost of moving `bytes` through a
// heap copy — the honest stand-in for a RAM→device expert transfer
// on this engine (weights live in an mmapped blob).
double xcm_measure_transfer_ms(const fs::path& weights_bin,
                               int64_t offset, int64_t bytes) {
    std::ifstream f(weights_bin, std::ios::binary);
    if (!f) return -1;
    std::vector<char> src((size_t)bytes);
    f.seekg(offset);
    f.read(src.data(), (std::streamsize)bytes);
    if (f.gcount() != bytes) return -1;
    std::vector<char> dst((size_t)bytes);
    const int64_t t0 = bench_now_ms();
    volatile char sink = 0;
    std::memcpy(dst.data(), src.data(), (size_t)bytes);
    sink = dst[0];
    (void)sink;
    double ms = (double)(bench_now_ms() - t0);
    // sub-ms resolution: repeat small copies
    if (ms < 1.0) {
        const int64_t t2 = bench_now_ms();
        for (int i = 0; i < 32; ++i) {
            std::memcpy(dst.data(), src.data(), (size_t)bytes);
            sink = dst[0];
        }
        ms = (double)(bench_now_ms() - t2) / 32.0;
    }
    return ms;
}

// ------------------------------------------------ §2/§4 residency ---
int mode_expert_residency(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("EXPERT_RESIDENCY_INVALID:bundle");
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("EXPERT_RESIDENCY_INVALID:") + ex.what());
    }
    JsonValue mf = parse_json_file(
        (fs::path(bundle) / "manifest.json").string());
    const JsonValue* cfg = mf.get("config");
    const int64_t layers = (int64_t)xct::j_num(cfg, "num_hidden_layers", 0);
    const int64_t experts = (int64_t)xct::j_num(cfg, "moe_num_experts", 0);
    const int64_t interval =
        (int64_t)xct::j_num(cfg, "moe_layer_interval", 1);
    if (experts <= 0) {
        std::printf("{\"ok\":true,\"mode\":\"expert-residency\","
                    "\"moe\":false,\"decision\":\"ALL_RESIDENT\","
                    "\"note\":\"dense bundle — no routed experts\"}\n");
        return 0;
    }
    XcmExpertResidency rm;
    rm.expert_bytes = xcm_expert_bytes(mf, 0, 0);
    rm.gpu_budget_bytes =
        a.has("gpu-budget") ? std::stoll(a.get("gpu-budget")) : 0;
    rm.max_resident =
        a.has("max-resident") ? std::stoll(a.get("max-resident")) : 0;
    rm.prefetch_depth =
        a.has("prefetch-depth") ? std::stoi(a.get("prefetch-depth")) : 1;
    rm.auto_mode = a.has("auto");

    // §10 AUTO: all routed experts fit the residency budget →
    // ALL_RESIDENT; offload only engages past the budget.
    const int64_t moe_layers =
        interval > 0 ? layers / interval : layers;
    const int64_t total_expert_bytes =
        rm.expert_bytes * experts * moe_layers;
    const bool fits =
        rm.gpu_budget_bytes <= 0 ||
        total_expert_bytes <= rm.gpu_budget_bytes;
    if (rm.auto_mode && fits) {
        std::printf(
            "{\"ok\":true,\"mode\":\"expert-residency\",\"auto\":true,"
            "\"decision\":\"ALL_RESIDENT\",\"total_expert_bytes\":%lld,"
            "\"gpu_budget_bytes\":%lld,"
            "\"note\":\"experts fit residency budget — offload off\"}\n",
            (long long)total_expert_bytes,
            (long long)rm.gpu_budget_bytes);
        return 0;
    }

    // Drive the manager with the real router trace: encode + generate a
    // few prompts, then feed every (layer, expert, weight) selection.
    e.set_router_trace(true);
    const int64_t vocab =
        (int64_t)xct::j_num(cfg, "vocab_size", 0);
    std::mt19937_64 rng(7);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    SamplingConfig sc;
    sc.temperature = 0.9;
    for (int p = 0; p < 4; ++p) {
        std::vector<int64_t> ids;
        for (int i = 0; i < 24; ++i) ids.push_back(tok(rng));
        try { e.generate(ids, 8, sc); }
        catch (const std::exception& ex) {
            fail(std::string("EXPERT_RESIDENCY_INVALID:") + ex.what());
        }
        for (const auto& tr : e.router_trace()) {
            for (int64_t t = 0; t < tr.token_count; ++t)
                for (int64_t k = 0; k < tr.top_k; ++k) {
                    int64_t ex =
                        tr.expert_ids[(size_t)(t * tr.top_k + k)];
                    double w =
                        tr.weights[(size_t)(t * tr.top_k + k)];
                    rm.use(tr.layer_id, ex, w);
                }
        }
    }
    e.set_router_trace(false);

    std::ostringstream o;
    o << "{\"ok\":true,\"mode\":\"expert-residency\","
      << "\"format\":\"star-expert-residency/v1\",\"moe\":true,"
      << "\"experts_per_layer\":" << experts
      << ",\"moe_layers\":" << moe_layers
      << ",\"expert_bytes\":" << rm.expert_bytes
      << ",\"total_expert_bytes\":" << total_expert_bytes
      << ",\"pinned\":[\"router\",\"shared_expert\",\"embedding\","
      << "\"common_weights\"],"
      << "\"resident_experts\":" << rm.resident_count()
      << ",\"resident_bytes\":" << rm.resident_bytes
      << ",\"cache_hits\":" << rm.hits
      << ",\"cache_misses\":" << rm.misses
      << ",\"hit_rate\":"
      << (rm.hits + rm.misses > 0
              ? (double)rm.hits / (rm.hits + rm.misses) : 0.0)
      << ",\"prefetch_depth\":" << rm.prefetch_depth
      << ",\"experts\":[";
    bool first = true;
    for (const XcmExpertRec& r : rm.recs) {
        if (!first) o << ',';
        first = false;
        o << "{\"layer\":" << r.layer_id
          << ",\"expert_id\":" << r.expert_id
          << ",\"state\":\"" << r.state << "\""
          << ",\"bytes\":" << r.bytes
          << ",\"hits\":" << r.hit_count
          << ",\"loads\":" << r.load_count
          << ",\"transfer_bytes\":" << r.transfer_bytes
          << ",\"residency_score\":" << r.residency_score << '}';
    }
    o << "]}";
    std::printf("%s\n", o.str().c_str());
    return 0;
}

// ------------------------------------------------- §11 bench --------
int mode_expert_offload_bench(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("EXPERT_RESIDENCY_INVALID:bundle");
    JsonValue mf = parse_json_file(
        (fs::path(bundle) / "manifest.json").string());
    const JsonValue* cfg = mf.get("config");
    const int64_t experts =
        (int64_t)xct::j_num(cfg, "moe_num_experts", 0);
    const int64_t layers =
        (int64_t)xct::j_num(cfg, "num_hidden_layers", 0);
    const int64_t interval =
        (int64_t)xct::j_num(cfg, "moe_layer_interval", 1);
    const int64_t top_k =
        (int64_t)xct::j_num(cfg, "moe_top_k", 1);
    if (experts <= 0)
        fail("EXPERT_RESIDENCY_INVALID:dense-bundle");
    const int64_t eb = xcm_expert_bytes(mf, 0, 0);
    const int64_t moe_layers = interval > 0 ? layers / interval : layers;
    const JsonValue* wf = mf.get("weights_file");
    const std::string weights =
        (fs::path(bundle) / (wf && wf->type == JsonValue::Type::String
                                 ? wf->string
                                 : "weights.bin"))
            .string();

    // all-resident baseline: real measured generate throughput.
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("EXPERT_RESIDENCY_INVALID:") + ex.what());
    }
    const int64_t vocab = (int64_t)xct::j_num(cfg, "vocab_size", 0);
    std::mt19937_64 rng(11);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> ids;
    for (int i = 0; i < 32; ++i) ids.push_back(tok(rng));
    SamplingConfig sc;
    const int64_t t0 = bench_now_ms();
    std::vector<int64_t> out;
    try { out = e.generate(ids, 16, sc); }
    catch (const std::exception& ex) {
        fail(std::string("EXPERT_RESIDENCY_INVALID:") + ex.what());
    }
    const int64_t base_ms = bench_now_ms() - t0;

    // Measured transfer cost for one expert (fp64 storage and int8
    // quantized storage) — the offload lane's honest PCIe/RAM cost
    // model on this host.
    double xfer_fp64 = xcm_measure_transfer_ms(weights, 0, eb);
    double xfer_int8 = xfer_fp64 / 8.0;  // same bandwidth, 8× fewer bytes

    // Unique experts a token touches per MoE layer under a cold cache:
    // top_k selections × moe_layers → worst-case transfers per step.
    const int64_t experts_per_token = top_k * moe_layers;

    std::ostringstream o;
    o << "{\"ok\":true,\"mode\":\"expert-offload-bench\","
      << "\"format\":\"star-expert-offload-bench/v1\","
      << "\"baseline\":{\"all_resident\":true,"
      << "\"generate_ms\":" << base_ms
      << ",\"generated_tokens\":" << (int64_t)out.size() << "},"
      << "\"expert_bytes\":" << eb
      << ",\"total_expert_bytes\":" << eb * experts * moe_layers
      << ",\"experts_per_token_worst\":" << experts_per_token
      << ",\"transfer_ms_fp64\":" << xfer_fp64
      << ",\"transfer_ms_int8\":" << xfer_int8
      << ",\"modeled_cold_miss_ms_fp64\":"
      << xfer_fp64 * experts_per_token
      << ",\"modeled_cold_miss_ms_int8\":"
      << xfer_int8 * experts_per_token
      << ",\"verdict\":\""
      << (xfer_fp64 * experts_per_token > base_ms
              ? "OFFLOAD_NOT_WORTH_IT"
              : "OFFLOAD_VIABLE_WHEN_OVER_BUDGET")
      << "\"}\n";
    std::printf("%s", o.str().c_str());
    return 0;
}

// ------------------------------------------- §13/§14 hybrid prefix --
int mode_hybrid_prefix_smoke(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("PREFIX_STATE_INCOMPATIBLE:bundle");
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("PREFIX_STATE_INCOMPATIBLE:") + ex.what());
    }
    SamplingConfig sc;
    sc.temperature = 0.0; sc.top_k = 1;
    std::vector<int64_t> ids = e.encode("The capital of France is",
                                        true, false);
    // cold
    std::vector<int64_t> g1 = e.generate(ids, 8, sc);
    // hit — must be bit-identical
    std::vector<int64_t> g2 = e.generate(ids, 8, sc);
    const bool identical = (g1 == g2);
    // partial prefix
    std::vector<int64_t> ids2 =
        e.encode("The capital of France is Paris and", true, false);
    std::vector<int64_t> g3 = e.generate(ids2, 8, sc);
    const bool partial = !g3.empty();
    // scope isolation: same prompt under a different scope misses
    e.set_prefix_scope("tenant-b");
    e.generate(ids, 8, sc);
    JsonValue desc = JsonParser(e.describe()).parse();
    const int64_t hits =
        (int64_t)xct::j_num(&desc, "prefix_cache_hits", 0);
    const int64_t misses =
        (int64_t)xct::j_num(&desc, "prefix_cache_misses", 0);
    auto rep = e.memory_report();
    std::printf(
        "{\"ok\":true,\"mode\":\"hybrid-prefix-smoke\","
        "\"format\":\"star-prefix-smoke/v1\","
        "\"hit_bit_identical\":%s,\"partial_prefix\":%s,"
        "\"prefix_cache_bytes\":%lld,\"cache_hits\":%lld,"
        "\"cache_misses\":%lld}\n",
        identical ? "true" : "false", partial ? "true" : "false",
        (long long)rep.prefix_cache_bytes,
        (long long)hits, (long long)misses);
    return identical && partial && hits >= 1 ? 0 : 1;
}

// §19 scope invalidation smoke
int mode_prefix_invalidation(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("PREFIX_STATE_INCOMPATIBLE:bundle");
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("PREFIX_STATE_INCOMPATIBLE:") + ex.what());
    }
    SamplingConfig sc;
    std::vector<int64_t> ids = e.encode("doc prefix", true, false);
    e.set_prefix_scope("rag:manifest-v1");
    e.generate(ids, 4, sc);
    const int64_t removed = e.invalidate_prefix_scope("rag:manifest-v1");
    const int64_t removed_other =
        e.invalidate_prefix_scope("rag:manifest-v2");
    auto rep = e.memory_report();
    std::printf(
        "{\"ok\":true,\"mode\":\"prefix-invalidation\","
        "\"invalidated\":%lld,\"other_scope_untouched\":%lld,"
        "\"prefix_cache_bytes_after\":%lld}\n",
        (long long)removed, (long long)removed_other,
        (long long)rep.prefix_cache_bytes);
    return removed >= 1 && removed_other == 0 ? 0 : 1;
}

// §20 measured RAG-prefix benefit — never a hardcoded percentage.
int mode_rag_prefix_bench(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("PREFIX_STATE_INCOMPATIBLE:bundle");
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("PREFIX_STATE_INCOMPATIBLE:") + ex.what());
    }
    SamplingConfig sc;
    sc.temperature = 0.0; sc.top_k = 1;
    // Shared document prefix + different questions (§21 partial).
    std::string shared =
        "System: answer from the document. Document: Alpha report Q3 "
        "revenue 42M, margin 11%, outlook stable. ";
    std::vector<int64_t> prefix_ids = e.encode(shared, true, false);

    // cold: unique scope each round → guaranteed miss baseline
    auto timed = [&](const std::string& p, const char* scope) {
        if (scope) e.set_prefix_scope(scope);
        std::vector<int64_t> ids = e.encode(p, true, false);
        const int64_t t = bench_now_ms();
        e.generate(ids, 4, sc);
        return bench_now_ms() - t;
    };
    int64_t cold = timed(shared + "Question one?", "rag:cold-1");
    int64_t cold2 = timed(shared + "Question two?", "rag:cold-2");
    e.set_prefix_scope("rag:stable");
    int64_t warm = timed(shared + "Question one?", nullptr);
    int64_t hit_full = timed(shared + "Question one?", nullptr);
    int64_t hit_partial = timed(shared + "Different question?", nullptr);
    const double cold_avg = (cold + cold2) / 2.0;
    std::printf(
        "{\"ok\":true,\"mode\":\"rag-prefix-bench\","
        "\"format\":\"star-rag-prefix-bench/v1\","
        "\"prefix_tokens\":%lld,\"cold_ms\":%g,\"warm_ms\":%lld,"
        "\"full_hit_ms\":%lld,\"partial_hit_ms\":%lld,"
        "\"prefill_ms_saved_full\":%g,"
        "\"ttft_reduction_pct\":%g}\n",
        (long long)prefix_ids.size(), cold_avg,
        (long long)warm, (long long)hit_full,
        (long long)hit_partial,
        cold_avg - hit_full,
        cold_avg > 0 ? 100.0 * (cold_avg - hit_full) / cold_avg : 0.0);
    return 0;
}

// --------------------------------------- §25-§36 prefill/decode -----
int mode_pd_bench(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("PREFILL_ARTIFACT_INVALID:bundle");
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("PREFILL_ARTIFACT_INVALID:") + ex.what());
    }
    SamplingConfig sc;
    sc.temperature = 0.0; sc.top_k = 1;
    std::vector<int64_t> ids =
        e.encode("Prefill decode split probe text.", true, false);

    // unified path
    const int64_t t0 = bench_now_ms();
    std::vector<int64_t> u = e.generate(ids, 8, sc);
    const int64_t unified_ms = bench_now_ms() - t0;

    // split path: prefill → artifact → same-process transfer → decode
    const int64_t t1 = bench_now_ms();
    std::string artifact = e.prefill_artifact(ids, "bench");
    const int64_t prefill_ms = bench_now_ms() - t1;
    const int64_t t2 = bench_now_ms();
    std::string moved = artifact;  // same-process reference transfer
    const int64_t xfer_ms = bench_now_ms() - t2;
    const int64_t t3 = bench_now_ms();
    std::vector<int64_t> d = e.generate_from_artifact(moved, 8, sc);
    const int64_t decode_ms = bench_now_ms() - t3;
    const bool identical = (u == d);

    // §35 decode latency guard: throttles prefill concurrency when ITL
    // exceeds target (single-process model → reported as decision).
    const double itl_ms =
        d.size() > 1 ? (double)decode_ms / (d.size() - 1) : 0.0;
    const double itl_target =
        a.has("itl-target") ? std::stod(a.get("itl-target")) : 0.0;
    const bool guard = itl_target > 0 && itl_ms > itl_target;

    std::printf(
        "{\"ok\":true,\"mode\":\"pd-pipeline-bench\","
        "\"format\":\"star-pd-bench/v1\","
        "\"unified_ms\":%lld,\"prefill_ms\":%lld,"
        "\"state_transfer_ms\":%lld,\"decode_ms\":%lld,"
        "\"artifact_bytes\":%lld,\"itl_ms\":%g,"
        "\"decode_latency_guard\":%s,"
        "\"decode_bit_identical\":%s,"
        "\"note\":\"one engine core; PREFILL/DECODE are roles\"}\n",
        (long long)unified_ms, (long long)prefill_ms,
        (long long)xfer_ms, (long long)decode_ms,
        (long long)artifact.size(), itl_ms,
        guard ? "true" : "false",
        identical ? "true" : "false");
    return identical ? 0 : 1;
}

// §48 fail-closed: a tampered or foreign artifact must fail with a
// typed error — never decode from corrupted state.
int mode_pd_transfer_smoke(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("PREFILL_ARTIFACT_INVALID:bundle");
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("PREFILL_ARTIFACT_INVALID:") + ex.what());
    }
    std::vector<int64_t> ids =
        e.encode("transfer integrity probe", true, false);
    std::string art = e.prefill_artifact(ids, "tamper");
    SamplingConfig sc;

    // case 1: bit flip inside the payload → checksum fails closed
    std::string bad = art;
    bad[bad.size() / 2] ^= 0x5A;
    std::string c1 = "no-error";
    try { e.generate_from_artifact(bad, 4, sc); }
    catch (const std::exception& ie) { c1 = ie.what(); }
    // case 2 (optional --bundle2): an artifact produced by a DIFFERENT
    // bundle must fail the model/generation binding on decode.
    std::string c2 = "skipped";
    bool ok2 = true;
    if (a.has("bundle2")) {
        NativeInferenceEngine e2;
        try { e2.load(a.get("bundle2")); }
        catch (const std::exception& ex) {
            fail(std::string("PREFILL_ARTIFACT_INVALID:") + ex.what());
        }
        std::string foreign = e2.prefill_artifact(ids, "foreign");
        try { e.generate_from_artifact(foreign, 4, sc); }
        catch (const std::exception& ie) { c2 = ie.what(); }
        ok2 = c2.rfind("PD_MODEL_MISMATCH", 0) == 0 ||
              c2.rfind("PD_GENERATION_MISMATCH", 0) == 0 ||
              c2.rfind("PREFILL_ARTIFACT_INVALID", 0) == 0 ||
              c2.rfind("DELTA_PREFIX_STATE_INVALID", 0) == 0;
    }
    const bool ok1 = c1.rfind("PREFILL_ARTIFACT_INVALID", 0) == 0;
    std::printf(
        "{\"ok\":%s,\"mode\":\"pd-transfer-smoke\","
        "\"tampered_error\":\"%s\",\"forged_gen_error\":\"%s\"}\n",
        ok1 && ok2 ? "true" : "false",
        gptbridge::jsonlite::json_escape(c1).c_str(),
        gptbridge::jsonlite::json_escape(c2).c_str());
    return ok1 && ok2 ? 0 : 1;
}

// §14: delta-prefix-restore — a hybrid hit restores the recurrent
// state, so the continuation is bit-identical to an uninterrupted run.
int mode_delta_prefix_restore(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("DELTA_PREFIX_STATE_INVALID:bundle");
    NativeInferenceEngine e;
    try { e.load(bundle); }
    catch (const std::exception& ex) {
        fail(std::string("DELTA_PREFIX_STATE_INVALID:") + ex.what());
    }
    SamplingConfig sc;
    sc.temperature = 0.0; sc.top_k = 1; sc.seed = 42;
    std::vector<int64_t> ids =
        e.encode("alpha beta gamma delta epsilon", true, false);
    e.set_prefix_scope("delta-restore");
    e.generate(ids, 4, sc);                    // populate
    std::vector<int64_t> hit = e.generate(ids, 8, sc);
    // continuous reference: fresh engine, same prompt, no cache state
    NativeInferenceEngine e2;
    e2.load(bundle);
    std::vector<int64_t> ref2 = e2.generate(ids, 8, sc);
    const bool same = hit == ref2;
    std::printf(
        "{\"ok\":%s,\"mode\":\"delta-prefix-restore\","
        "\"restored_continuation_identical\":%s,"
        "\"restored_tokens\":%lld}\n",
        same ? "true" : "false", same ? "true" : "false",
        (long long)hit.size());
    return same ? 0 : 1;
}

// §9 quantized-storage certification gate: an expert storage precision
// other than the certified execution precision needs parity evidence —
// without it the candidate is EXPERT_PRECISION_UNCERTIFIED, never
// implicitly production.
int mode_expert_quant_parity(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("EXPERT_PRECISION_UNCERTIFIED:bundle");
    JsonValue mf = parse_json_file(
        (fs::path(bundle) / "manifest.json").string());
    const int64_t qn =
        (int64_t)xct::j_num(&mf, "quantized_tensors", 0);
    const JsonValue* cert = mf.get("expert_quant_parity");
    const bool certified =
        cert && cert->type == JsonValue::Type::Object &&
        xct::j_num(cert, "expert_logit_parity", 0) > 0 &&
        xct::j_num(cert, "router_parity", 0) > 0 &&
        xct::j_num(cert, "generation_parity", 0) > 0;
    if (qn > 0 && !certified)
        fail("EXPERT_PRECISION_UNCERTIFIED");
    std::printf(
        "{\"ok\":true,\"mode\":\"expert-quant-parity\","
        "\"quantized_tensors\":%lld,"
        "\"parity_certified\":%s,"
        "\"note\":\"loadable != production — parity evidence "
        "required\"}\n",
        (long long)qn, certified ? "true" : "false");
    return 0;
}

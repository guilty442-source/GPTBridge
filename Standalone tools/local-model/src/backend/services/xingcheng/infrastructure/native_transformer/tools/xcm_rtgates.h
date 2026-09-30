// xcm_rtgates.h — runtime-gate modes converged from the devin lane into
// the canonical xc_modeltool. Included once by xc_modeltool.cpp inside
// the anonymous namespace, after xcm_capability.h (helpers it uses:
// fail/jget_str/parse_json_file/manifest_field/xct::j_num/bench_now_ms)
// and after xcm_batch2.h (xcm2::* probe infrastructure).
//
// Ported surfaces (research/probe only — nothing here changes weights
// or production dispatch):
//   mtp-draft-probe     300M §21-§23 real draft/verify vs exported MTP head
//   cuda-parity-all     §19 every CUDA lane vs CPU fp64 reference
//   sparse-probe        §7.1 KVBlockIndex/QueryBlockSelector/gather bench
//   kv-gather-probe     §7.2 kv_outer_gather operator prototype
//   sched-smoke         §10 SequenceLayerScheduler prepare/run/commit/
//                       rollback lifecycle
//   state-drift         §12 FP64 vs BF16/FP16 recurrent drift ladder
//   hw-caps             §27 hardware capability registry (detect only)
//   state2-smoke        §29 star-native-state/v2 seal/verify + bind gate
//   memory-plan         §7 InferenceMemoryPlanner (star-memory-report/v1)
//   state-snapshot      §23 DeltaNet state save/extend/restore round trip
//   native-thinking-eval §7-§9 thinking lane run record
//   state-bench         §23 SequenceStateBenchmark + snapshot round trip
//   hw-baseline         hardware baseline evidence (CPU/RAM/tps honest
//                       nulls where unavailable)
//   router-analyze      §8/§29 MoERoutingAnalyzer quantiles + ROUTER_*
//   spec-verify         §6/§11/§13 NativeSpeculativeDecoder ABI contract
//   param-reuse-probe   §24 cross-layer block-sharing simulation
//   precision-parity    §26 REFERENCE_FP64 vs PRODUCTION_BF16 gate
#pragma once

// §19 cuda-parity-all: every CUDA compute lane verified against the
// CPU fp64 reference — a kernel that compiles is not a kernel that is
// correct. Absent lanes report "not_run" rather than PASS; a built
// lane that diverges is a hard failure.
// xcuda_probe is declared by the host TU; gpu_stats is only needed by
// the lanes below (NVML instantaneous sensors: bit0 gpu util %, bit1
// power mW).
extern "C" int xcuda_gpu_stats(unsigned* gpu_util_pct,
                               unsigned* power_mw);
extern "C" int xcuda_available();
extern "C" int xcuda_matmul_f64(const double*, long long, long long,
                                const double*, long long, double*);
extern "C" int xcuda_matmul_f64_grouped(
    const double*, const long long*, long long, const double* const*,
    long long, long long, double*);
extern "C" int xcuda_bf16_available();
extern "C" int xcuda_matmul_bf16(const double*, long long, long long,
                                 const double*, long long, double*);
extern "C" int xcuda_fp8_available();
extern "C" int xcuda_matmul_fp8(const double*, long long, long long,
                                const double*, long long, double*);
extern "C" int xcuda_kv_available();
extern "C" int xcuda_kv_alloc(long long, long long, long long,
                              long long);
extern "C" void xcuda_kv_free();
extern "C" int xcuda_kv_write_rows(int, long long, long long, long long,
                                   long long, const double*);
extern "C" int xcuda_kv_attention(long long, const double*, long long,
                                  long long, long long, long long,
                                  long long, double*, long long);

static void cpup_ref_matmul(const double* a, long long m, long long k,
                            const double* b, long long n, double* out) {
    for (long long i = 0; i < m; ++i)
        for (long long j = 0; j < n; ++j) {
            double s = 0.0;
            for (long long p = 0; p < k; ++p)
                s += a[i * k + p] * b[p * n + j];
            out[i * n + j] = s;
        }
}

static double cpup_maxdiff(const std::vector<double>& a,
                           const std::vector<double>& b) {
    double d = 0.0;
    for (size_t i = 0; i < a.size() && i < b.size(); ++i)
        d = std::max(d, std::fabs(a[i] - b[i]));
    return d;
}

static double cpup_maxabs(const std::vector<double>& v) {
    double m = 0.0;
    for (double x : v) m = std::max(m, std::fabs(x));
    return m;
}

int mode_cuda_parity_all(const Args&) {
    long long fb = 0, tb = 0;
    int ccm = 0, ccn = 0;
    const bool cuda = xcuda_probe(&fb, &tb, &ccm, &ccn) != 0;
    const long long M = 8, K = 16, N = 12;
    std::vector<double> A(M * K), B(K * N);
    {
        std::mt19937_64 rng(4);
        std::uniform_real_distribution<double> u(-0.5, 0.5);
        for (auto& v : A) v = u(rng);
        for (auto& v : B) v = u(rng);
    }
    std::vector<double> ref(M * N), got(M * N, 0.0);
    cpup_ref_matmul(A.data(), M, K, B.data(), N, ref.data());
    const double refmax = std::max(cpup_maxabs(ref), 1e-12);

    struct Lane { const char* name; const char* status;
                  double max_diff; double tol; };
    std::vector<Lane> lanes;
    auto run_gemm = [&](const char* name,
                        int (*fn)(const double*, long long, long long,
                                  const double*, long long, double*),
                        double tol_scale) {
        if (!cuda) { lanes.push_back({name, "not_run", 0, 0}); return; }
        std::fill(got.begin(), got.end(), 0.0);
        if (fn(A.data(), M, K, B.data(), N, got.data()) != 0) {
            lanes.push_back({name, "unavailable", 0, 0});
            return;
        }
        double d = cpup_maxdiff(ref, got);
        lanes.push_back({name, d <= tol_scale * refmax ? "PASS" : "FAIL",
                         d, tol_scale * refmax});
    };
    run_gemm("gemm_f64", xcuda_matmul_f64, 1e-9);
    run_gemm("gemm_bf16", xcuda_matmul_bf16, 0.02);
    run_gemm("gemm_fp8", xcuda_matmul_fp8, 0.25);

    // Grouped fp64 GEMM: two row groups, per-group weight matrices.
    if (cuda) {
        const long long rows[2] = {3, 5};
        std::vector<double> B2(K * N);
        {
            std::mt19937_64 rng(7);
            std::uniform_real_distribution<double> u(-0.5, 0.5);
            for (auto& v : B2) v = u(rng);
        }
        const double* bl[2] = {B.data(), B2.data()};
        std::vector<double> gref(M * N), ggot(M * N, 0.0);
        cpup_ref_matmul(A.data(), 3, K, B.data(), N, gref.data());
        cpup_ref_matmul(A.data() + 3 * K, 5, K, B2.data(), N,
                        gref.data() + 3 * N);
        if (xcuda_matmul_f64_grouped(A.data(), rows, 2, bl, K, N,
                                     ggot.data()) != 0) {
            lanes.push_back({"gemm_f64_grouped", "unavailable", 0, 0});
        } else {
            double d = cpup_maxdiff(gref, ggot);
            double tol = 1e-9 * std::max(cpup_maxabs(gref), 1e-12);
            lanes.push_back({"gemm_f64_grouped",
                             d <= tol ? "PASS" : "FAIL", d, tol});
        }
    } else {
        lanes.push_back({"gemm_f64_grouped", "not_run", 0, 0});
    }

    // Device KV + online-softmax attention vs a CPU reference of the
    // same causal semantics (query s attends 0..position_offset+s).
    if (cuda && xcuda_kv_available()) {
        const long long L = 1, KH = 1, D = 8, ML = 16;
        bool kv_ok = xcuda_kv_alloc(L, KH, D, ML) == 0;
        double kv_diff = -1.0;
        if (kv_ok) {
            const long long seq_in = 4;
            std::vector<double> kdat(seq_in * D), vdat(seq_in * D),
                qdat(seq_in * D), out(2 * D, 0.0);
            {
                std::mt19937_64 rng(11);
                std::uniform_real_distribution<double> u(-1.0, 1.0);
                for (auto& v : kdat) v = u(rng);
                for (auto& v : vdat) v = u(rng);
                for (auto& v : qdat) v = u(rng);
            }
            kv_ok &= xcuda_kv_write_rows(1, 0, 0, 0, seq_in,
                                         kdat.data()) == 0;
            kv_ok &= xcuda_kv_write_rows(0, 0, 0, 0, seq_in,
                                         vdat.data()) == 0;
            const long long poff = 2, qs = 2;
            // q covers positions poff..poff+qs-1
            kv_ok &= xcuda_kv_attention(0, qdat.data() + 0, 1, qs, 1, D,
                                        poff, out.data(), D) == 0;
            // CPU reference of the same kernel semantics
            std::vector<double> cref(qs * D, 0.0);
            for (long long s = 0; s < qs; ++s) {
                const long long last = poff + s;
                std::vector<double> sc(last + 1);
                for (long long t = 0; t <= last; ++t) {
                    double dot = 0.0;
                    for (long long d = 0; d < D; ++d)
                        dot += qdat[s * D + d] * kdat[t * D + d];
                    sc[t] = dot / std::sqrt((double)D);
                }
                double mx = *std::max_element(sc.begin(), sc.end());
                double l = 0.0;
                for (long long t = 0; t <= last; ++t) {
                    sc[t] = std::exp(sc[t] - mx); l += sc[t];
                }
                for (long long d = 0; d < D; ++d) {
                    double a = 0.0;
                    for (long long t = 0; t <= last; ++t)
                        a += sc[t] * vdat[t * D + d];
                    cref[s * D + d] = a / l;
                }
            }
            kv_diff = cpup_maxdiff(cref, out);
            xcuda_kv_free();
        }
        lanes.push_back({"kv_attention",
                         !kv_ok ? "unavailable"
                                : (kv_diff <= 1e-9 ? "PASS" : "FAIL"),
                         kv_diff, 1e-9});
    } else {
        lanes.push_back({"kv_attention", "not_run", 0, 0});
    }

    bool all = true;
    std::ostringstream lj;
    for (size_t i = 0; i < lanes.size(); ++i) {
        bool pass = std::string(lanes[i].status) == "PASS";
        bool fail = std::string(lanes[i].status) == "FAIL";
        if (fail) all = false;
        if (i) lj << ',';
        lj << '"' << lanes[i].name << "\":{\"status\":\""
           << lanes[i].status << "\"";
        if (pass || fail)
            lj << ",\"max_diff\":" << lanes[i].max_diff
               << ",\"tol\":" << lanes[i].tol;
        lj << '}';
    }
    std::printf("{\"ok\":%s,\"format\":\"star-cuda-parity/v1\","
                "\"cuda_available\":%s,\"lanes\":{%s}}\n",
                all ? "true" : "false", cuda ? "true" : "false",
                lj.str().c_str());
    return all ? 0 : 1;
}


// ------------------------------------------------ batch-2 probes (§7/§10/§12/§13/§27/§29)

// §7.1 sparse-attention-probe: synthetic KV block index + centroid
// selector + gather plan; recall measured against full attention.
int mode_sparse_probe(const Args& a) {
    int64_t tokens = std::stoll(a.get("tokens", "8192"));
    int dim = std::stoi(a.get("dim", "64"));
    int64_t max_blocks = std::stoll(a.get("max-blocks", "16"));
    int64_t block_tokens = std::stoll(a.get("block-tokens", "64"));
    xcm2::KVBlockIndex idx;
    idx.block_tokens = block_tokens;
    idx.build(tokens, dim, 17);
    std::vector<float> q((size_t)dim);
    std::mt19937_64 rng(3);
    std::normal_distribution<float> nd(0.f, 1.f);
    for (auto& v : q) v = nd(rng);
    auto r = xcm2::bench_sparse(idx, q, max_blocks);
    std::printf(
        "{\"ok\":true,\"format\":\"star-sparse-attention-probe/v1\","
        "\"selected_blocks\":%lld,\"coverage_ratio\":%.4f,"
        "\"kv_bytes_read\":%lld,\"memory_coalescing\":%.4f,"
        "\"recall_against_full_attention\":%.4f,"
        "\"production_callable\":false}\n",
        (long long)r.selected_blocks, r.coverage_ratio,
        (long long)r.kv_bytes_read, r.memory_coalescing,
        r.recall_against_full);
    return 0;
}

// §7.2 kv-outer-gather-probe: operator prototype on synthetic tensors.
int mode_kv_gather_probe(const Args& a) {
    int64_t tokens = std::stoll(a.get("tokens", "4096"));
    int dim = std::stoi(a.get("dim", "64"));
    int64_t max_blocks = std::stoll(a.get("max-blocks", "8"));
    xcm2::KVBlockIndex idx;
    idx.build(tokens, dim, 23);
    std::vector<float> q((size_t)dim);
    std::mt19937_64 rng(5);
    std::normal_distribution<float> nd(0.f, 1.f);
    for (auto& v : q) v = nd(rng);
    auto sel = xcm2::QueryBlockSelector::select(idx, q, max_blocks);
    auto plan = xcm2::BlockGatherPlan::from(sel, idx.block_tokens, dim);
    auto g = xcm2::kv_outer_gather_q(idx, q, plan.block_ids);
    bool sane = g.block_max.size() == plan.block_ids.size() &&
                std::all_of(g.block_argmax.begin(), g.block_argmax.end(),
                            [&](int64_t t) { return t >= 0; });
    std::printf(
        "{\"ok\":%s,\"format\":\"star-kv-outer-gather-probe/v1\","
        "\"blocks\":%zu,\"element_reads\":%lld,"
        "\"production_dispatch\":false}\n",
        sane ? "true" : "false", g.block_max.size(),
        (long long)g.reads);
    return sane ? 0 : 1;
}

// §10 sched-smoke: unified recurrent/attention state lifecycle —
// prepare -> run -> commit advances state; rollback restores it.
int mode_sched_smoke(const Args&) {
    xcm2::SequenceLayerScheduler s;
    std::vector<bool> recurrent = {true, false, true, false};
    s.build(4, recurrent, 8);
    bool ok = true;
    for (int64_t l = 0; l < 4; ++l) {
        s.PrepareLayer(l);
        std::vector<double> upd(8, 1.0);
        if (recurrent[(size_t)l]) s.RunRecurrent(l, upd);
        else s.RunAttention(l, upd);
        s.CommitState(l);
        for (auto v : s.at(l).state) ok &= v == 1.0;
    }
    // Rollback path: dirty a layer, roll back, state must be restored.
    s.PrepareLayer(0);
    s.RunRecurrent(0, std::vector<double>(8, 5.0));
    s.RollbackState(0);
    for (auto v : s.at(0).state) ok &= v == 1.0;
    std::printf("{\"ok\":%s,\"format\":\"star-sequence-scheduler/v1\","
                "\"layers\":%zu,\"production_dispatch\":false}\n",
                ok ? "true" : "false", s.slots.size());
    return ok ? 0 : 1;
}

// §12 state-drift: FP64 reference vs BF16/FP16 recurrent state over the
// 1K..16K token ladder; FP8 deliberately excluded.
int mode_state_drift(const Args& a) {
    int64_t max_tok = std::stoll(a.get("tokens", "16384"));
    std::vector<int64_t> ladder = {1024, 2048, 4096, 8192, 16384};
    ladder.erase(std::remove_if(ladder.begin(), ladder.end(),
                                [&](int64_t t) { return t > max_tok; }),
                 ladder.end());
    std::ostringstream o;
    o << "{\"ok\":true,\"format\":\"star-recurrent-drift/v1\","
         "\"precisions\":{";
    const char* precs[] = {"BF16", "FP16"};
    bool first = true;
    for (const char* p : precs) {
        auto pts = xcm2::recurrent_drift(p, ladder);
        if (!first) o << ',';
        first = false;
        o << '"' << p << "\":[";
        for (size_t i = 0; i < pts.size(); ++i) {
            if (i) o << ',';
            o << "{\"tokens\":" << pts[i].tokens
              << ",\"max_drift\":" << pts[i].max_drift << '}';
        }
        o << ']';
    }
    o << "}}\n";
    std::printf("%s", o.str().c_str());
    return 0;
}


// §27 hw-caps: hardware capability registry — detection only; a
// precision profile is never enabled by env-var fiat (§27 last rule).
int mode_hw_caps(const Args&) {
    xcm2::HardwareCapabilityRegistry r;
    r.detect_cpu();
    r.detect_mem();
    long long fb = 0, tb = 0; int ccm = 0, ccn = 0;
    r.cuda_available = xcuda_probe(&fb, &tb, &ccm, &ccn) != 0;
    r.vram_free_mb = fb / (1024 * 1024);
    r.vram_total_mb = tb / (1024 * 1024);
    r.cuda_cc_major = ccm; r.cuda_cc_minor = ccn;
    // CUDA arch hints for low-precision lanes — capability detection,
    // not enablement (parity certification gates use, §27/§28).
    if (r.cuda_available) {
        r.fp8 = ccm >= 9 || (ccm == 8 && ccn >= 9);  // sm_89+/sm_90+
        r.bf16 = r.bf16 || ccm >= 8;                  // sm_80+ bf16 hw
    }
    std::printf(
        "{\"ok\":true,\"format\":\"star-hw-capability/v1\","
        "\"cpu\":{\"avx2\":%s,\"fma\":%s,\"avx512f\":%s,"
        "\"bf16\":%s,\"fp16\":%s},"
        "\"cuda\":{\"available\":%s,\"cc\":\"%d.%d\","
        "\"vram_free_mb\":%lld,\"vram_total_mb\":%lld,"
        "\"fp8_hw\":%s,\"fp4_hw\":%s},"
        "\"system_ram_mb\":%lld,"
        "\"note\":\"detection only — promotion needs quant-cert\"}\n",
        r.avx2 ? "true" : "false", r.fma ? "true" : "false",
        r.avx512f ? "true" : "false", r.bf16 ? "true" : "false",
        r.fp16 ? "true" : "false",
        r.cuda_available ? "true" : "false",
        r.cuda_cc_major, r.cuda_cc_minor,
        (long long)r.vram_free_mb, (long long)r.vram_total_mb,
        r.fp8 ? "true" : "false", r.fp4 ? "true" : "false",
        (long long)r.sys_ram_mb);
    return 0;
}

// §29/§14-15 state2-smoke: star-native-state/v2 seal/verify + the
// typed binding gate — every mismatch class must return its §15 code.
int mode_state2_smoke(const Args&) {
    xcm2::NativeStateHeader h;
    h.generation = "gen-2-consolidated";
    h.bundle_hash = "b1";
    h.model_hash = "m1";
    h.tokenizer_hash = "tok";
    h.architecture = "xc-fused-1";
    h.state_type = "DELTA_RECURRENT";
    h.precision = "FP64";
    h.sequence_length = 8;
    double state[8] = {0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8};
    h.seal(state, sizeof(state));
    bool ok = h.verify(state, sizeof(state));
    ok &= h.bind_error("gen-2-consolidated", "b1", "m1", "tok")
          == nullptr;
    auto code = [&](const char* g, const char* b, const char* m,
                    const char* t) {
        const char* e = h.bind_error(g, b, m, t);
        return e == nullptr ? "" : std::string(e);
    };
    ok &= code("gen-3", "b1", "m1", "tok") == "STATE_GENERATION_MISMATCH";
    ok &= code("gen-2-consolidated", "b2", "m1", "tok")
          == "STATE_MODEL_MISMATCH";
    ok &= code("gen-2-consolidated", "b1", "m2", "tok")
          == "STATE_MODEL_MISMATCH";
    ok &= code("gen-2-consolidated", "b1", "m1", "tok2")
          == "STATE_TOKENIZER_MISMATCH";
    double bad[8]; std::memcpy(bad, state, sizeof(bad)); bad[0] = 9.9;
    ok &= !h.verify(bad, sizeof(bad));
    std::ostringstream types;
    for (const char* t : xcm2::NativeStateHeader::kStateTypes)
        types << (types.tellp() > 0 ? ",\"" : "\"") << t << '"';
    std::printf("{\"ok\":%s,\"format\":\"star-native-state/v2\","
                "\"state_version\":%lld,\"state_types\":[%s],"
                "\"model_hash_binding\":true,"
                "\"tokenizer_binding\":true}\n",
                ok ? "true" : "false",
                (long long)h.state_version, types.str().c_str());
    return ok ? 0 : 1;
}


// Serialize the engine's two-level MoE trace: level 1 router decisions
// (router_type, top_k, bounded per-token selection sample) + level 2
// expert dispatch (per-expert routed counts, shared-expert
// participation), tagged with request/model/architecture identifiers so
// both levels join on one deterministic context.
void emit_moe_trace(std::ostringstream& o,
                    xingcheng::inference::NativeInferenceEngine& engine,
                    const JsonValue& req) {
    using xingcheng::inference::MoeTraceLayer;
    const auto& tr = engine.moe_trace();
    o << "{\"request_id\":\""
      << gptbridge::jsonlite::json_escape(jget_str(req, "request_id"))
      << "\""
      << ",\"model_version\":\""
      << gptbridge::jsonlite::json_escape(
             engine.bundle() ? engine.bundle()->weights_sha256().substr(0, 16)
                             : "")
      << "\""
      << ",\"architecture_generation\":\""
      << gptbridge::jsonlite::json_escape(
             engine.bundle() ? engine.bundle()->architecture_generation()
                             : "")
      << "\""
      << ",\"forwards\":" << tr.forwards << ",\"layers\":[";
    for (size_t i = 0; i < tr.layers.size(); ++i) {
        const MoeTraceLayer& tl = tr.layers[i];
        if (i) o << ',';
        o << "{\"layer_id\":" << tl.layer_id
          << ",\"router_type\":\"" << tl.router_type << "\""
          << ",\"top_k\":" << tl.top_k
          << ",\"tokens_routed\":" << tl.tokens_routed
          << ",\"shared_expert_used\":"
          << (tl.shared_expert_used ? "true" : "false")
          << ",\"expert_counts\":[";
        for (size_t e = 0; e < tl.expert_counts.size(); ++e) {
            if (e) o << ',';
            o << tl.expert_counts[e];
        }
        o << "],\"selected\":[";
        for (size_t s = 0; s < tl.selected.size(); ++s) {
            if (s) o << ',';
            o << '[';
            for (size_t k = 0; k < tl.selected[s].size(); ++k) {
                if (k) o << ',';
                o << tl.selected[s][k];
            }
            o << ']';
        }
        o << "]}";
    }
    o << "]}";
}


// ------------------------------------------- capability modes (P3-P7) ----

int mode_memory_plan(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("MEMORY_PLAN_ARGS_MISSING");
    xingcheng::inference::WeightBundle wb = xingcheng::inference::
        WeightBundle::load((fs::path(bundle) / "manifest.json").string());
    int64_t ctx = a.has("context")
        ? (int64_t)std::stoll(a.get("context")) : 0;
    int64_t batch = a.has("batch")
        ? (int64_t)std::stoll(a.get("batch")) : 1;
    std::string kv = a.get("kv", "fp64");
    int64_t vp = a.has("vision-patches")
        ? (int64_t)std::stoll(a.get("vision-patches")) : 0;
    MemoryPlan p = plan_memory(wb, ctx, batch, kv, vp);
    // §16 star-memory-report/v1: per-category bytes (weights, KV,
    // prefix cache, DeltaNet state, thinking state, vision, workspace,
    // CUDA workspace) and the four governed peaks.
    std::printf(
        "{\"ok\":true,\"format\":\"star-memory-report/v1\","
        "\"weight_bytes\":%lld,\"kv_bytes\":%lld,"
        "\"prefix_cache_bytes\":%lld,\"recurrent_state_bytes\":%lld,"
        "\"vision_bytes\":%lld,\"workspace_bytes\":%lld,"
        "\"thinking_state_bytes\":%lld,\"cuda_workspace_bytes\":%lld,"
        "\"idle_bytes\":%lld,"
        "\"prefill_peak_bytes\":%lld,\"decode_peak_bytes\":%lld,"
        "\"thinking_peak_bytes\":%lld,"
        "\"context_tokens\":%lld,\"batch\":%lld,\"kv_mode\":\"%s\","
        "\"cuda_available\":%s,"
        "\"hybrid\":%s,\"prefix_reconstructs_state\":%s}\n",
        (long long)p.weight_bytes, (long long)p.kv_bytes,
        (long long)p.prefix_cache_bytes,
        (long long)p.recurrent_state_bytes,
        (long long)p.vision_bytes, (long long)p.workspace_bytes,
        (long long)p.thinking_state_bytes,
        (long long)p.cuda_workspace_bytes,
        (long long)p.idle_bytes,
        (long long)p.prefill_peak_bytes,
        (long long)p.decode_peak_bytes,
        (long long)p.thinking_peak_bytes,
        (long long)p.context_tokens, (long long)p.batch,
        gptbridge::jsonlite::json_escape(p.kv_mode).c_str(),
        p.cuda_available ? "true" : "false",
        p.hybrid ? "true" : "false",
        p.prefix_reconstructs_state ? "true" : "false");
    return 0;
}

// §23 state snapshot probe: prefill a slot, snapshot, extend, restore,
// and verify the DeltaNet state comes back byte-identical (the engine
// validates geometry + the envelope validates generation/model hash).
int mode_state_snapshot(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("STATE_SNAPSHOT_ARGS_MISSING");
    NativeInferenceEngine engine;
    engine.load(bundle);
    if (!engine.has_delta_state()) {
        std::printf("{\"ok\":true,\"format\":\"%s\",\"delta_state\":false,"
                    "\"note\":\"dense model — no recurrent state\"}\n",
                    DeltaStateSnapshot::kFormat);
        return 0;
    }
    JsonValue manifest =
        parse_json_file((fs::path(bundle) / "manifest.json").string());
    const JsonValue* mcfg = manifest.get("config");
    int64_t vocab = mcfg ? (int64_t)xct::j_num(mcfg, "vocab_size", 0) : 0;
    if (vocab < 4) fail("STATE_SNAPSHOT_BAD_CONFIG");
    std::mt19937_64 rng(7);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> ids(24);
    for (auto& t : ids) t = tok(rng);
    SamplingConfig sc;
    sc.temperature = 0.0;
    // Warm the slot, snapshot, extend, restore, verify. generate()
    // allocates slot 0 and appends KV + DeltaNet state; logits() is the
    // cache-free probe path and would leave lin_states_ empty.
    (void)engine.generate(ids, 4, sc);
    std::vector<char> blob_a;
    if (!engine.delta_state_save(0, blob_a)) fail("STATE_SNAPSHOT_SAVE");
    std::vector<int64_t> ext(8);
    for (auto& t : ext) t = tok(rng);
    (void)engine.generate(ext, 4, sc);   // advances the recurrence
    if (!engine.delta_state_restore(0, blob_a.data(),
                                    (int64_t)blob_a.size())) {
        fail("STATE_SNAPSHOT_RESTORE");
    }
    std::vector<char> blob_b;
    if (!engine.delta_state_save(0, blob_b)) fail("STATE_SNAPSHOT_SAVE2");
    const bool identical = blob_a == blob_b;
    DeltaStateSnapshot env =
        delta_snapshot_save(engine, 0, "xc-fused-1");
    // Generation mismatch must fail closed.
    bool gen_reject = false;
    {
        DeltaStateSnapshot bad = env;
        bad.generation = "gen-x-other";
        gen_reject = delta_snapshot_restore(engine, bad) != nullptr;
    }
    // A bad hash must fail closed too.
    bool hash_reject = false;
    {
        DeltaStateSnapshot bad = env;
        bad.state_sha256 = "00";
        hash_reject = delta_snapshot_restore(engine, bad) != nullptr;
    }
    const bool ok = identical && gen_reject && hash_reject;
    std::printf(
        "{\"ok\":%s,\"format\":\"%s\",\"version\":%lld,"
        "\"delta_state\":true,\"slot\":0,\"state_bytes\":%lld,"
        "\"state_sha256\":\"%s\",\"restore_identical\":%s,"
        "\"generation_mismatch_rejected\":%s,"
        "\"hash_mismatch_rejected\":%s,"
        "\"generation\":\"%s\"}\n",
        ok ? "true" : "false",
        DeltaStateSnapshot::kFormat,
        (long long)DeltaStateSnapshot::kVersion,
        (long long)env.state_bytes,
        env.state_sha256.c_str(),
        identical ? "true" : "false",
        gen_reject ? "true" : "false",
        hash_reject ? "true" : "false",
        gptbridge::jsonlite::json_escape(env.generation).c_str());
    return ok ? 0 : 1;
}

// §7–§9 Native Thinking evaluation lane — emits star-native-thinking/v1
// run records (§8) plus a thinking summary. Records keep only the
// evaluable telemetry (steps/branches/scores/latency/memory); no
// private reasoning text is stored. --quick runs one MEDIUM probe
// (release-gate smoke); the full ladder runs OFF/LOW/MEDIUM/HIGH.
int mode_native_thinking_eval(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("THINKING_ARGS_MISSING");
    std::string prompt = a.get("prompt");
    if (prompt.empty()) prompt = "說明：1+1 為什麼等於 2？";
    int64_t max_new = 32;
    if (a.has("max-new")) {
        try { max_new = std::stoll(a.get("max-new")); }
        catch (...) { fail("THINKING_BAD_MAX_NEW"); }
    }
    if (max_new < 1 || max_new > 512) fail("THINKING_BAD_MAX_NEW");
    bool quick = a.has("quick");

    NativeInferenceEngine engine;
    try { engine.load(bundle); }
    catch (const std::exception& e) {
        fail(std::string("THINKING_LOAD_FAILED:") + e.what());
    }
    JsonValue manifest =
        parse_json_file((fs::path(bundle) / "manifest.json").string());
    std::string generation;
    if (const JsonValue* pv = manifest.get("provenance"))
        if (const JsonValue* g = pv->get("generation"))
            if (g->type == JsonValue::Type::String)
                generation = g->string;
    const std::string bundle_hash =
        sha256_file((fs::path(bundle) / "manifest.json").string());
    const std::string request_id =
        "think-" + sha256_text(prompt + bundle).substr(0, 12);
    std::vector<int64_t> pids = engine.encode(prompt, true, false);
    if (pids.empty()) fail("THINKING_EMPTY_PROMPT");
    SamplingConfig sc;
    sc.do_sample = false;
    sc.temperature = 0.0;

    struct Level { const char* name; int64_t steps; int64_t branches; };
    static const Level kLevels[] = {
        {"off", 0, 1}, {"low", 2, 1}, {"medium", 4, 4},
        {"high", 8, 4},
    };
    const size_t n_level = quick ? 1 : 4;
    const Level* levels = quick ? kLevels + 2 : kLevels;

    std::ostringstream runs;
    runs << '[';
    double off_latency_ms = -1.0;
    bool all_ok = true;
    for (size_t li = 0; li < n_level; ++li) {
        const Level& lv = levels[li];
        auto t0 = std::chrono::steady_clock::now();
        NativeInferenceEngine::ThinkingResult res;
        std::vector<int64_t> out_ids;
        int64_t steps_used = 0;
        int64_t chosen = -1;
        std::vector<double> scores;
        if (lv.steps <= 0) {
            out_ids = engine.generate(pids, max_new, sc);
        } else {
            res = engine.generate_thinking(
                pids, lv.steps, lv.branches, max_new, sc);
            out_ids = res.answer_ids;
            steps_used = res.think_steps;
            chosen = res.chosen_branch;
            scores = res.branch_scores;
        }
        double lat_ms = 1e3 * std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t0).count();
        if (lv.steps <= 0) off_latency_ms = lat_ms;
        bool run_ok = !out_ids.empty();
        if (lv.steps > 0)
            run_ok = run_ok && steps_used > 0 && steps_used <= 32
                     && chosen >= 0 && chosen < lv.branches
                     && (int64_t)scores.size() == lv.branches;
        if (!run_ok) all_ok = false;
        std::ostringstream ids_hash;
        // §8: persist the answer hash, not the text.
        std::string ans_sha;
        {
            std::ostringstream b;
            for (int64_t id : out_ids) b << id << ',';
            ans_sha = sha256_text(b.str());
        }
        if (li) runs << ',';
        runs << "{\"format\":\"star-native-thinking/v1\""
             << ",\"request_id\":\"" << request_id << "-" << lv.name
             << "\",\"level\":\"" << lv.name
             << "\",\"generation\":\""
             << gptbridge::jsonlite::json_escape(generation)
             << "\",\"bundle_hash\":\"sha256:" << bundle_hash
             << "\",\"think_steps_requested\":" << lv.steps
             << ",\"think_steps_used\":" << steps_used
             << ",\"branches_requested\":" << lv.branches
             << ",\"branches_used\":" << (int64_t)scores.size()
             << ",\"branch_scores\":[";
        for (size_t i = 0; i < scores.size(); ++i) {
            if (i) runs << ',';
            runs << scores[i];
        }
        runs << "],\"selected_branch\":" << chosen
             << ",\"thinking_latency_ms\":" << lat_ms
             << ",\"thinking_memory_bytes\":"
             << (long long)engine.kv_memory_bytes()
             << ",\"output_tokens\":" << (int64_t)out_ids.size()
             << ",\"answer_sha256\":\"sha256:" << ans_sha
             << "\",\"fallback_reason\":null"
             << ",\"ok\":" << (run_ok ? "true" : "false") << '}';
    }
    runs << ']';
    std::printf(
        "{\"ok\":%s,\"format\":\"star-native-thinking-eval/v1\","
        "\"bundle\":\"%s\",\"quick\":%s,"
        "\"baseline_latency_ms\":%.1f,\"runs\":%s,"
        "\"thinking_gain\":{\"basis\":"
        "\"latency + branch self-confidence only — no ground-truth "
        "suite bound; AUTO default stays OFF per §9\","
        "\"value\":null}}\n",
        all_ok ? "true" : "false",
        gptbridge::jsonlite::json_escape(bundle).c_str(),
        quick ? "true" : "false",
        off_latency_ms, runs.str().c_str());
    return all_ok ? 0 : 1;
}

// §23 sequence-state benchmark: state/kv bytes per token, prefill/decode
// throughput, stream duration, snapshot restore time.
int mode_state_bench(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("STATE_BENCH_ARGS_MISSING");
    int64_t prefill = a.has("prefill")
        ? (int64_t)std::stoll(a.get("prefill")) : 64;
    int64_t decode = a.has("decode")
        ? (int64_t)std::stoll(a.get("decode")) : 16;
    NativeInferenceEngine engine;
    engine.load(bundle);
    JsonValue manifest =
        parse_json_file((fs::path(bundle) / "manifest.json").string());
    const JsonValue* mcfg = manifest.get("config");
    int64_t vocab = mcfg ? (int64_t)xct::j_num(mcfg, "vocab_size", 0) : 0;
    if (vocab < 4) fail("STATE_BENCH_BAD_CONFIG");
    std::mt19937_64 rng(5);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> ids((size_t)prefill);
    for (auto& t : ids) t = tok(rng);
    SamplingConfig sc;
    sc.temperature = 0.0;
    auto t0 = std::chrono::steady_clock::now();
    (void)engine.logits(ids);
    auto t1 = std::chrono::steady_clock::now();
    std::vector<int64_t> gen = engine.generate(ids, decode, sc);
    auto t2 = std::chrono::steady_clock::now();
    std::vector<char> blob;
    const bool have_state = engine.delta_state_save(0, blob);
    std::vector<char> copy = blob;
    auto t3 = std::chrono::steady_clock::now();
    bool restored = false;
    if (have_state)
        restored = engine.delta_state_restore(
            0, copy.data(), (int64_t)copy.size());
    auto t4 = std::chrono::steady_clock::now();
    const double prefill_s =
        std::chrono::duration<double>(t1 - t0).count();
    const double decode_s =
        std::chrono::duration<double>(t2 - t1).count();
    const double restore_s =
        std::chrono::duration<double>(t4 - t3).count();
    const int64_t kv_bytes = engine.kv_memory_bytes();
    const int64_t st_bytes = engine.delta_state_bytes(0);
    const int64_t total = prefill + (int64_t)gen.size();
    std::printf(
        "{\"ok\":true,\"format\":\"star-sequence-state-bench/v1\","
        "\"state_bytes_session\":%lld,\"kv_bytes\":%lld,"
        "\"delta_state_bytes\":%lld,"
        "\"state_bytes_token\":%.1f,\"kv_bytes_token\":%.1f,"
        "\"prefill_tps\":%.1f,\"decode_tps\":%.1f,"
        "\"stream_duration_s\":%.4f,\"state_restore_time_s\":%.6f,"
        "\"state_restore_ok\":%s,\"has_delta_state\":%s}\n",
        (long long)(kv_bytes + st_bytes), (long long)kv_bytes,
        (long long)st_bytes,
        total > 0 ? (double)(kv_bytes + st_bytes) / total : 0.0,
        total > 0 ? (double)kv_bytes / total : 0.0,
        prefill_s > 0 ? prefill / prefill_s : 0.0,
        decode_s > 0 ? (double)gen.size() / decode_s : 0.0,
        prefill_s + decode_s, restore_s,
        restored ? "true" : "false",
        engine.has_delta_state() ? "true" : "false");
    return 0;
}

// ---------------------------------------------------------- hw-baseline ----
//
// §66 300M Hardware Baseline (star-hardware-baseline-300m/v1): a single
// measured record every later optimization compares against (§67/§68).
// Loads the bundle, runs a timed prefill + decode, and reports the full
// §66 field set — VRAM delta via the CUDA probe when a device exists,
// process RAM peak via psapi, CPU utilization from process times over
// the bench window, prefill/decode TPS, TTFT (prefill + first decode
// step) and ITL (per-token decode latency). GPU utilization and power
// come from NVML sampled on a 50ms cadence across the bench window
// (peak values); a host without NVML emits null rather than a
// fabricated value. --train-report <file.json> supplies the
// trainer-side tokens_per_sec from a governed train report.
//
//   xc_modeltool hw-baseline --bundle <dir> [--prefill N] [--decode N]
//       [--train-report <report.json>]

static double filetime_s(const FILETIME& ft) {
    ULARGE_INTEGER u;
    u.LowPart = ft.dwLowDateTime; u.HighPart = ft.dwHighDateTime;
    return (double)u.QuadPart * 1e-7;
}

int mode_hw_baseline(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("HW_BASELINE_ARGS_MISSING");
    int64_t prefill = a.has("prefill")
        ? (int64_t)std::stoll(a.get("prefill")) : 512;
    int64_t decode = a.has("decode")
        ? (int64_t)std::stoll(a.get("decode")) : 64;

    // Device VRAM before load — baseline is the delta the model+KV adds.
    long long vb0 = -1, vt0 = -1, vb1 = -1, vt1 = -1;
    int ccmaj = 0, ccmin = 0;
    const bool cuda = xcuda_probe(&vb0, &vt0, &ccmaj, &ccmin) != 0;
    // NVML init on the main thread before the bench: the sampler thread
    // below may only query already-bound sensors — nvmlInit racing the
    // engine's first cuBLAS/NVRTC init crashes the driver.
    bool nvml_ready = false;
    if (cuda) {
        unsigned u = 0, p = 0;
        nvml_ready = xcuda_gpu_stats(&u, &p) != 0;
    }

    NativeInferenceEngine engine;
    auto tl0 = std::chrono::steady_clock::now();
    engine.load(bundle);
    auto tl1 = std::chrono::steady_clock::now();

    JsonValue manifest =
        parse_json_file((fs::path(bundle) / "manifest.json").string());
    const JsonValue* mcfg = manifest.get("config");
    int64_t vocab = mcfg ? (int64_t)xct::j_num(mcfg, "vocab_size", 0) : 0;
    if (vocab < 4) fail("HW_BASELINE_BAD_CONFIG");
    std::mt19937_64 rng(11);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> ids((size_t)prefill);
    for (auto& t : ids) t = tok(rng);
    SamplingConfig sc;
    sc.temperature = 0.0;

    // CPU% is measured over the bench window only — the FILETIME pair
    // must align with t0/t2 or the engine load inflates the numerator.
    // NVML sampling runs concurrently on a 50ms cadence so a short
    // decode still catches the working utilization/power rather than a
    // post-idle read. Missing sensors stay unmeasured (null below).
    std::atomic<bool> sample_run{true};
    std::atomic<unsigned> util_max{0}, power_max{0};
    std::atomic<int> util_seen{0}, power_seen{0};
    std::thread sampler;
    if (nvml_ready) {
        sampler = std::thread([&] {
            while (sample_run.load()) {
                unsigned u = 0, p = 0;
                const int m = xcuda_gpu_stats(&u, &p);
                if (m & 1) {
                    util_seen.fetch_add(1);
                    unsigned cur = util_max.load();
                    while (u > cur &&
                           !util_max.compare_exchange_weak(cur, u)) {}
                }
                if (m & 2) {
                    power_seen.fetch_add(1);
                    unsigned cur = power_max.load();
                    while (p > cur &&
                           !power_max.compare_exchange_weak(cur, p)) {}
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
        });
    }
    FILETIME c0{}, e0{}, k0{}, u0{};
    GetProcessTimes(GetCurrentProcess(), &c0, &e0, &k0, &u0);
    auto t0 = std::chrono::steady_clock::now();
    (void)engine.logits(ids);                    // prefill
    auto t1 = std::chrono::steady_clock::now();
    std::vector<int64_t> gen = engine.generate(ids, decode, sc);
    auto t2 = std::chrono::steady_clock::now();
    sample_run.store(false);
    if (sampler.joinable()) sampler.join();

    FILETIME c1{}, e1{}, k1{}, u1{};
    GetProcessTimes(GetCurrentProcess(), &c1, &e1, &k1, &u1);
    PROCESS_MEMORY_COUNTERS pmc{};
    int64_t ram_peak = 0;
    if (GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof(pmc)))
        ram_peak = (int64_t)pmc.PeakWorkingSetSize;
    if (cuda) (void)xcuda_probe(&vb1, &vt1, &ccmaj, &ccmin);

    const double prefill_s =
        std::chrono::duration<double>(t1 - t0).count();
    const double decode_s =
        std::chrono::duration<double>(t2 - t1).count();
    const double load_s =
        std::chrono::duration<double>(tl1 - tl0).count();
    const double wall_s =
        std::chrono::duration<double>(t2 - t0).count();
    const double cpu_s =
        (filetime_s(k1) + filetime_s(u1)) -
        (filetime_s(k0) + filetime_s(u0));
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    const double cpu_util =
        wall_s > 0 && si.dwNumberOfProcessors > 0
            ? cpu_s / (wall_s * (double)si.dwNumberOfProcessors) : 0.0;
    const double itl_ms = gen.size() > 0
        ? decode_s * 1000.0 / (double)gen.size() : 0.0;
    // TTFT = prefill + one decode step.
    const double ttft_ms = prefill_s * 1000.0 + itl_ms;
    const int64_t vram_used =
        (cuda && vb0 >= 0 && vb1 >= 0) ? (vb0 - vb1) : -1;

    // §66 training side: tokens/sec comes from a governed train report,
    // not from this inference process — pass it in explicitly.
    double train_tps = -1.0;
    if (a.has("train-report")) {
        JsonValue rep = parse_json_file(a.get("train-report"));
        const JsonValue* v = rep.get("tokens_per_sec");
        if (v && v->type == JsonValue::Type::Number)
            train_tps = v->number;
    }
    char vram_buf[24] = "null", tps_buf[32] = "null";
    char gpu_buf[24] = "null", pwr_buf[24] = "null";
    if (vram_used >= 0)
        std::snprintf(vram_buf, sizeof(vram_buf), "%lld", vram_used);
    if (train_tps >= 0.0)
        std::snprintf(tps_buf, sizeof(tps_buf), "%.2f", train_tps);
    // §66: NVML reports util in whole %; normalize to the same 0-1
    // fraction as cpu_utilization. Power is milliwatts -> watts.
    // Unread sensors keep "null" — never fabricated.
    if (util_seen.load() > 0)
        std::snprintf(gpu_buf, sizeof(gpu_buf), "%.4f",
                      util_max.load() / 100.0);
    if (power_seen.load() > 0)
        std::snprintf(pwr_buf, sizeof(pwr_buf), "%.1f",
                      power_max.load() / 1000.0);

    std::printf(
        "{\"ok\":true,\"format\":\"star-hardware-baseline-300m/v1\","
        "\"model_scale\":\"300m\",\"bundle\":\"%s\","
        "\"weights_sha256\":\"%s\","
        "\"vram_peak_bytes\":%s,\"ram_peak_bytes\":%lld,"
        "\"cpu_utilization\":%.4f,\"gpu_utilization\":%s,"
        "\"prefill_tps\":%.1f,\"decode_tps\":%.1f,"
        "\"ttft_ms\":%.2f,\"itl_ms\":%.2f,"
        "\"training_tokens_per_sec\":%s,\"power_watts\":%s,"
        "\"prefill_tokens\":%lld,\"decode_tokens\":%lld,"
        "\"load_s\":%.3f,\"kv_bytes\":%lld,"
        "\"cuda_available\":%s}\n",
        gptbridge::jsonlite::json_escape(bundle).c_str(),
        engine.bundle() ? engine.bundle()->weights_sha256().c_str() : "",
        vram_buf,
        (long long)ram_peak, cpu_util, gpu_buf,
        prefill_s > 0 ? prefill / prefill_s : 0.0,
        decode_s > 0 ? (double)gen.size() / decode_s : 0.0,
        ttft_ms, itl_ms, tps_buf, pwr_buf,
        (long long)prefill, (long long)gen.size(),
        load_s, (long long)engine.kv_memory_bytes(),
        cuda ? "true" : "false");
    return 0;
}

// §8 router analyzer: generate with the MoE trace armed and emit the
// per-layer quantiles + ROUTER_* diagnostics.
int mode_router_analyze(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("ROUTER_ARGS_MISSING");
    NativeInferenceEngine engine;
    engine.load(bundle);
    JsonValue manifest =
        parse_json_file((fs::path(bundle) / "manifest.json").string());
    const JsonValue* mcfg = manifest.get("config");
    if (!mcfg || xct::j_num(mcfg, "moe_num_experts", 0) <= 0)
        fail("ROUTER_NOT_MOE");
    int64_t vocab = (int64_t)xct::j_num(mcfg, "vocab_size", 0);
    std::mt19937_64 rng(3);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> ids(32);
    for (auto& t : ids) t = tok(rng);
    SamplingConfig sc;
    sc.temperature = 0.0;
    engine.set_moe_trace_enabled(true);
    (void)engine.generate(ids, 16, sc);
    const auto& tr = engine.moe_trace();
    // §20/§21 unified emission — the single star-moe-trace/v1 record:
    // per-layer trace (router type, score summary, bounded per-token
    // selection+weight sample, dispatch histogram, shared gate weight)
    // fused with the analyzer fields (utilization, affinity, overlap,
    // hotspot, starvation, shared dependency, entropy, quantiles).
    // Observability only — no router-weight updates while capability
    // training is frozen.
    std::ostringstream o;
    o << "{\"ok\":true,\"format\":\"star-moe-trace/v1\","
         "\"forwards\":" << tr.forwards << ",\"layers\":[";
    bool any_flag = false;
    for (size_t i = 0; i < tr.layers.size(); ++i) {
        const auto& tl = tr.layers[i];
        Quantiles q;
        RouterDiagnosis d = analyze_router(tl, q);
        const double routed =
            std::max<double>(tl.tokens_routed, 1);
        if (i) o << ',';
        o << "{\"layer_id\":" << tl.layer_id
          << ",\"router_type\":\"" << tl.router_type << "\""
          << ",\"top_k\":" << tl.top_k
          << ",\"tokens_routed\":" << tl.tokens_routed
          << ",\"router_score_summary\":{\"min\":"
          << (tl.score_n ? tl.score_min : 0.0)
          << ",\"max\":" << (tl.score_n ? tl.score_max : 0.0)
          << ",\"mean\":"
          << (tl.score_n ? tl.score_sum / tl.score_n : 0.0) << "}"
          << ",\"selected_experts\":[";
        for (size_t s = 0; s < tl.selected.size(); ++s) {
            if (s) o << ',';
            o << '[';
            for (size_t k = 0; k < tl.selected[s].size(); ++k) {
                if (k) o << ',';
                o << tl.selected[s][k];
            }
            o << ']';
        }
        o << "],\"normalized_weights\":[";
        for (size_t s = 0; s < tl.weights.size(); ++s) {
            if (s) o << ',';
            o << '[';
            for (size_t k = 0; k < tl.weights[s].size(); ++k) {
                if (k) o << ',';
                o << tl.weights[s][k];
            }
            o << ']';
        }
        o << "],\"shared_expert_weight\":"
          << (tl.tokens_routed ? tl.shared_weight_sum / routed : 0.0)
          << ",\"dispatch_histogram\":[";
        for (size_t e = 0; e < tl.expert_counts.size(); ++e) {
            if (e) o << ',';
            o << tl.expert_counts[e];
        }
        o << "],\"expert_utilization\":[";
        const double disp_total = std::max<double>(
            std::accumulate(tl.expert_counts.begin(),
                            tl.expert_counts.end(), int64_t{0}), 1);
        for (size_t e = 0; e < tl.expert_counts.size(); ++e) {
            if (e) o << ',';
            o << tl.expert_counts[e] / disp_total;
        }
        o << "],\"router_entropy\":" << d.router_entropy
          << ",\"router_quantiles\":{"
          << "\"p01\":" << q.p01 << ",\"p05\":" << q.p05
          << ",\"p25\":" << q.p25 << ",\"p50\":" << q.p50
          << ",\"p75\":" << q.p75 << ",\"p95\":" << q.p95
          << ",\"p99\":" << q.p99 << "}"
          << ",\"expert_affinity\":" << d.expert_affinity
          << ",\"expert_overlap\":" << d.expert_overlap
          << ",\"expert_hotspot\":{\"expert\":" << d.hotspot_expert
          << ",\"share\":" << d.top_share << "}"
          << ",\"expert_starvation\":" << d.starved
          << ",\"shared_expert_dependency\":"
          << d.shared_expert_dependency
          << ",\"instability\":" << d.instability
          << ",\"shared_expert_used\":"
          << (tl.shared_expert_used ? "true" : "false")
          << ",\"diagnostics\":[";
        for (size_t f = 0; f < d.flags.size(); ++f) {
            if (f) o << ',';
            o << '"' << d.flags[f] << '"';
            any_flag = true;
        }
        o << "]}";
    }
    o << "],\"any_diagnostic\":" << (any_flag ? "true" : "false")
      << ",\"capability_training_frozen\":true}\n";
    std::fputs(o.str().c_str(), stdout);
    return 0;
}


// §6/§11/§13 speculative-decoder probe: the full NativeSpeculativeDecoder
// contract (PrepareDraft -> DraftTokens -> VerifyTokens -> AcceptPrefix
// -> RejectFrom -> CommitState / RollbackState) on a synthetic drafter.
// Production stays structurally disabled — no drafter is ever bound
// outside this probe (MTP heads are dropped at export).
int mode_spec_verify(const Args&) {
    NativeSpeculativeDecoder dec;
    bool disabled_ok =
        dec.PrepareDraft({1, 2}, 4)
            != nullptr && std::string(
                dec.PrepareDraft({1, 2}, 4))
                == "SPECULATIVE_DECODER_DISABLED";
    SyntheticDrafter d;
    dec.BindDrafter(&d);
    bool ok = disabled_ok && dec.enabled();
    std::vector<int64_t> ctx{5, 6, 7, 8};
    ok &= dec.PrepareDraft(ctx, 4) == nullptr;
    auto pending = dec.DraftTokens();
    ok &= pending.size() == 4;
    // Scripted target continuation: first two match, rest diverge.
    std::vector<int64_t> target{pending[0], pending[1], 99, 98};
    const int64_t acc = dec.VerifyTokens(target);
    ok &= acc == 2;
    auto committed = dec.AcceptPrefix(acc);
    ok &= committed.size() == 2;
    dec.RejectFrom(acc);
    dec.CommitState();
    // Second round exercises RollbackState.
    ok &= dec.PrepareDraft(dec.context(), 4) == nullptr;
    (void)dec.DraftTokens();
    dec.RollbackState();
    ok &= dec.context().size() == ctx.size() + 2;
    const auto& m = dec.metrics();
    ok &= m.draft_tokens == 8 && m.accepted_tokens == 2
          && m.rejected_tokens == 6 && m.rollback_count == 2;
    std::printf(
        "{\"ok\":%s,\"format\":\"star-speculative-decoder/v1\","
        "\"enabled\":false,\"production_enabled\":false,"
        "\"reason\":\"MTP heads are dropped at export — no production "
        "drafter exists; contract + verification + metrics only\","
        "\"api\":[\"PrepareDraft\",\"DraftTokens\",\"VerifyTokens\","
        "\"AcceptPrefix\",\"RejectFrom\",\"CommitState\","
        "\"RollbackState\"],"
        "\"synthetic\":{\"accepted\":%lld,\"rejected\":%lld,"
        "\"acceptance_rate\":%.4f,\"draft_latency_ms\":%.3f,"
        "\"verify_latency_ms\":%.3f,\"rollback_count\":%lld,"
        "\"net_tps_gain\":%.4f,\"net_latency_gain\":%.3f}}\n",
        ok ? "true" : "false",
        (long long)m.accepted_tokens, (long long)m.rejected_tokens,
        m.acceptance_rate, m.draft_latency_ms, m.verify_latency_ms,
        (long long)m.rollback_count, m.net_tps_gain,
        m.net_latency_gain);
    return ok ? 0 : 1;
}

// §24 parameter-reuse probe — research evidence only.
int mode_param_reuse(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("REUSE_ARGS_MISSING");
    xingcheng::inference::WeightBundle wb = xingcheng::inference::
        WeightBundle::load((fs::path(bundle) / "manifest.json").string());
    ReuseProbeResult r = probe_parameter_reuse(wb);
    std::printf(
        "{\"ok\":true,\"format\":\"star-parameter-reuse-probe/v1\","
        "\"sink\":\"FutureArchitectureResearch\","
        "\"weights_bytes\":%lld,\"weights_saved_bytes\":%lld,"
        "\"weights_saved_frac\":%.4f,"
        "\"quality_risk\":\"%s\",\"routing_complexity\":\"%s\","
        "\"checkpoint_complexity\":\"%s\"}\n",
        (long long)r.weights_bytes,
        (long long)r.weights_saved_bytes, r.weights_saved_frac,
        gptbridge::jsonlite::json_escape(r.quality_risk).c_str(),
        gptbridge::jsonlite::json_escape(r.routing_complexity).c_str(),
        gptbridge::jsonlite::json_escape(r.checkpoint_complexity)
            .c_str());
    return 0;
}

// §26 precision parity: REFERENCE_FP64 baseline vs PRODUCTION_BF16
// candidate. BF16 unavailable → resolves FP64 with an explicit
// UNAVAILABLE status (fail-closed, never a silent claim).
// §18 candidate-bundle parity: decode a DLTS state image and return the
// flat f64 stream so FP64-reference vs BF16-candidate recurrent state can
// be diffed (bf16 loads expanded to f64, so layouts are identical).
bool decode_delta_state_flat(const std::vector<char>& blob,
                             std::vector<double>& flat) {
    if (blob.size() < 24) return false;
    auto u32 = [&](size_t o) {
        return (uint32_t)(uint8_t)blob[o] |
               ((uint32_t)(uint8_t)blob[o + 1] << 8) |
               ((uint32_t)(uint8_t)blob[o + 2] << 16) |
               ((uint32_t)(uint8_t)blob[o + 3] << 24);
    };
    auto i64 = [&](size_t o) {
        uint64_t v = 0;
        for (int i = 0; i < 8; ++i)
            v |= (uint64_t)(uint8_t)blob[o + i] << (8 * i);
        return (int64_t)v;
    };
    if (u32(0) != 0x53544C44u || u32(4) != 1) return false;
    const int64_t layers = i64(16);   // magic|ver|slot|layers header
    size_t p = 24;
    for (int64_t l = 0; l < layers; ++l) {
        if (p + 1 > blob.size()) return false;
        ++p;   // present flag — geometry already proven by restore path
        for (int v = 0; v < 2; ++v) {   // conv_tail, s
            if (p + 8 > blob.size()) return false;
            int64_t n = i64(p);
            p += 8;
            if (n < 0 || p + (size_t)n * 8 > blob.size()) return false;
            for (int64_t i = 0; i < n; ++i) {
                double d;
                std::memcpy(&d, blob.data() + p + (size_t)i * 8, 8);
                flat.push_back(d);
            }
            p += (size_t)n * 8;
        }
        if (p + 8 > blob.size()) return false;
        p += 8;   // tokens
    }
    return p == blob.size();
}

// §18: FP64 active bundle -> BF16 conversion -> parity evaluation.
// Compares reference vs candidate *bundles* on identical prompts:
// logit MAE/max, top1/top5, generation agreement, router agreement and
// DeltaNet state drift; TPS/TTFT per side. FP64 stays the production
// reference — this mode only produces evidence, it never promotes.
int parity_bundle_vs_bundle(const std::string& ref_bundle,
                            const std::string& cand_bundle) {
    JsonValue manifest =
        parse_json_file((fs::path(ref_bundle) / "manifest.json").string());
    const JsonValue* mcfg = manifest.get("config");
    int64_t vocab = mcfg ? (int64_t)xct::j_num(mcfg, "vocab_size", 0) : 0;
    if (vocab < 4) fail("PRECISION_BAD_CONFIG");
    std::mt19937_64 rng(9);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> ids(16);
    for (auto& t : ids) t = tok(rng);
    SamplingConfig sc;
    sc.temperature = 0.0;

    struct Side {
        std::vector<double> logits;
        std::vector<int64_t> gen;
        std::vector<char> state;
        xingcheng::inference::MoeTrace trace;
        double ttft = 0, gen_s = 0;
        bool has_state = false;
    };
    auto run_side = [&](const std::string& b, Side& s) {
        NativeInferenceEngine e;
        e.load(b);
        auto t0 = std::chrono::steady_clock::now();
        s.logits = e.logits(ids);
        s.ttft = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t0).count();
        e.set_moe_trace_enabled(true);
        t0 = std::chrono::steady_clock::now();
        s.gen = e.generate(ids, 16, sc);
        s.gen_s = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t0).count();
        s.trace = e.moe_trace();
        s.has_state = e.delta_state_save(0, s.state);
    };
    Side r, c;
    run_side(ref_bundle, r);
    run_side(cand_bundle, c);

    const size_t n = std::min(r.logits.size(), c.logits.size());
    if (n == 0) fail("PRECISION_EMPTY_LOGITS");
    double mae = 0.0, max_diff = 0.0;
    for (size_t i = 0; i < n; ++i) {
        const double d = std::abs(r.logits[i] - c.logits[i]);
        mae += d;
        max_diff = std::max(max_diff, d);
    }
    mae /= n;
    auto topk = [](const std::vector<double>& v, int k) {
        std::vector<int64_t> ord(v.size());
        std::iota(ord.begin(), ord.end(), 0);
        std::partial_sort(ord.begin(), ord.begin() + k, ord.end(),
                          [&](int64_t x, int64_t y) {
                              return v[(size_t)x] > v[(size_t)y];
                          });
        ord.resize(k);
        return ord;
    };
    const auto r5 = topk(r.logits, 5), c5 = topk(c.logits, 5);
    const bool top1 = r5[0] == c5[0];
    int64_t top5_hits = 0;
    for (int64_t x : c5)
        if (std::find(r5.begin(), r5.end(), x) != r5.end()) ++top5_hits;
    const double top5 = top5_hits / 5.0;
    const bool gen_agree = r.gen == c.gen;

    int64_t router_same = 0, router_total = 0;
    for (size_t li = 0;
         li < r.trace.layers.size() && li < c.trace.layers.size();
         ++li) {
        const auto& rs = r.trace.layers[li].selected;
        const auto& cs = c.trace.layers[li].selected;
        for (size_t i = 0; i < rs.size() && i < cs.size(); ++i) {
            ++router_total;
            if (rs[i] == cs[i]) ++router_same;
        }
    }
    const double router_agree =
        router_total ? (double)router_same / router_total : 1.0;

    double state_drift = 0.0;
    bool state_eval = false;
    if (r.has_state && c.has_state) {
        std::vector<double> rf, cf;
        if (decode_delta_state_flat(r.state, rf) &&
            decode_delta_state_flat(c.state, cf) &&
            rf.size() == cf.size()) {
            state_eval = true;
            for (size_t i = 0; i < rf.size(); ++i)
                state_drift = std::max(state_drift,
                                       std::abs(rf[i] - cf[i]));
        }
    }

    const bool pass = max_diff < 0.05 && mae < 0.02 && top1 &&
                      top5 >= 0.8 && gen_agree && router_agree >= 0.9;
    std::printf(
        "{\"ok\":%s,\"format\":\"star-precision-parity/v1\","
        "\"lane\":\"bundle-vs-bundle\","
        "\"resolved_precision\":\"%s\","
        "\"logit_mae\":%.6g,\"logit_max_abs_diff\":%.6g,"
        "\"top1_agreement\":%s,\"top5_agreement\":%.4g,"
        "\"generation_agreement\":%s,\"router_agreement\":%.4g,"
        "\"state_drift_evaluated\":%s,\"state_drift_max\":%.6g,"
        "\"ref_ttft_s\":%.4f,\"cand_ttft_s\":%.4f,"
        "\"ref_tps\":%.4f,\"cand_tps\":%.4f,"
        "\"capability_training_frozen\":true,"
        "\"threshold\":{\"logit_max_abs_diff\":0.05,"
        "\"logit_mae\":0.02,\"top5\":0.8,\"router\":0.9}}\n",
        pass ? "true" : "false",
        pass ? "CANDIDATE_PASSES" : "REFERENCE_FP64",
        mae, max_diff,
        top1 ? "true" : "false", top5,
        gen_agree ? "true" : "false", router_agree,
        state_eval ? "true" : "false", state_drift,
        r.ttft, c.ttft,
        r.gen_s > 0 ? 16.0 / r.gen_s : 0.0,
        c.gen_s > 0 ? 16.0 / c.gen_s : 0.0);
    return pass ? 0 : 1;
}

int mode_precision_parity(const Args& a) {
    std::string bundle = a.get("bundle");
    if (bundle.empty()) fail("PRECISION_ARGS_MISSING");
    const std::string ref_bundle = a.get("ref-bundle");
    if (!ref_bundle.empty()) {
        return parity_bundle_vs_bundle(ref_bundle, bundle);
    }
    JsonValue manifest =
        parse_json_file((fs::path(bundle) / "manifest.json").string());
    const JsonValue* mcfg = manifest.get("config");
    int64_t vocab = mcfg ? (int64_t)xct::j_num(mcfg, "vocab_size", 0) : 0;
    if (vocab < 4) fail("PRECISION_BAD_CONFIG");
    std::mt19937_64 rng(9);
    std::uniform_int_distribution<int64_t> tok(3, vocab - 1);
    std::vector<int64_t> ids(16);
    for (auto& t : ids) t = tok(rng);
    SamplingConfig sc;
    sc.temperature = 0.0;

    std::vector<double> ref;
    std::vector<int64_t> ref_gen;
    double ref_s = 0.0;
    {
        NativeInferenceEngine e;
        e.load(bundle);
        auto t0 = std::chrono::steady_clock::now();
        ref = e.logits(ids);
        ref_gen = e.generate(ids, 8, sc);
        ref_s = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t0).count();
    }

#ifdef _WIN32
    _putenv_s("XINGCHENG_CPP_CUDA", "1");
    _putenv_s("XINGCHENG_CPP_CUDA_BF16", "1");
#else
    setenv("XINGCHENG_CPP_CUDA", "1", 1);
    setenv("XINGCHENG_CPP_CUDA_BF16", "1", 1);
#endif
    std::vector<double> cand;
    std::vector<int64_t> cand_gen;
    double cand_s = 0.0;
    std::string cand_status;
    try {
        NativeInferenceEngine e;
        e.load(bundle);
        auto t0 = std::chrono::steady_clock::now();
        cand = e.logits(ids);
        cand_gen = e.generate(ids, 8, sc);
        cand_s = std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t0).count();
        cand_status = "CANDIDATE";
    } catch (const std::exception&) {
        cand_status = "UNAVAILABLE";   // e.g. CUDA_BF16_UNAVAILABLE
    }
#ifdef _WIN32
    _putenv_s("XINGCHENG_CPP_CUDA", "");
    _putenv_s("XINGCHENG_CPP_CUDA_BF16", "");
#endif

    if (cand_status == "UNAVAILABLE") {
        std::printf(
            "{\"ok\":true,\"format\":\"star-precision-parity/v1\","
            "\"candidate\":\"PRODUCTION_BF16\",\"status\":\"UNAVAILABLE\","
            "\"resolved_precision\":\"REFERENCE_FP64\","
            "\"fail_closed\":true}\n");
        return 0;
    }
    double max_diff = 0.0;
    int64_t top1_agree = 0;
    for (size_t i = 0; i < ref.size() && i < cand.size(); ++i) {
        max_diff = std::max(max_diff, std::abs(ref[i] - cand[i]));
    }
    if (!ref.empty() && !cand.empty()) {
        auto argmax = [](const std::vector<double>& v) {
            return (int64_t)std::distance(
                v.begin(), std::max_element(v.begin(), v.end()));
        };
        top1_agree = argmax(ref) == argmax(cand) ? 1 : 0;
    }
    const bool gen_agree = ref_gen == cand_gen;
    const bool pass = max_diff < 0.05 && top1_agree == 1 && gen_agree;
    std::printf(
        "{\"ok\":%s,\"format\":\"star-precision-parity/v1\","
        "\"candidate\":\"PRODUCTION_BF16\",\"status\":\"%s\","
        "\"resolved_precision\":\"%s\","
        "\"logit_max_abs_diff\":%.6g,\"top1_agreement\":%lld,"
        "\"generation_agreement\":%s,"
        "\"ref_time_s\":%.4f,\"cand_time_s\":%.4f,"
        "\"threshold\":{\"logit_max_abs_diff\":0.05}}\n",
        pass ? "true" : "false", cand_status.c_str(),
        pass ? "PRODUCTION_BF16" : "REFERENCE_FP64",
        max_diff, (long long)top1_agree,
        gen_agree ? "true" : "false", ref_s, cand_s);
    return pass ? 0 : 1;
}
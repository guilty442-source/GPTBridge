// xcm_capability.h — native capability contracts for xc_modeltool
// (architecture-convergence P3/P5/P6/P7).
//
// Included by xc_modeltool.cpp inside the anonymous namespace, after the
// shared helpers (fail/sha256_bytes/jget_str/manifest_field) it uses.
//
//   §7  InferenceMemoryPlanner   — weight/kv/prefix/recurrent/vision/
//       workspace byte budgets → prefill_peak / decode_peak.
//   §23 DeltaStateSnapshot       — versioned, hashed, generation-bound
//       envelope over engine.delta_state_save/restore.
//   §27 NativeStateRef           — type-tagged state registry contract;
//       generation/model-hash bound, STATE_*_MISMATCH fail-closed.
//   §20 VisionBudgetController   — FULL|BALANCED|COMPACT patch budgets;
//       adaptive compression EXPERIMENTAL, falls back to FULL without
//       parity evidence.
//   §6  SpeculativeDecoder       — Draft/Verify/Accept/Reject/Reset ABI
//       + synthetic-drafter probe. enabled=false: the exported bundle
//       drops the MTP heads, so no real drafter exists.
//   §8  MoERoutingAnalyzer       — P01..P99 expert-load quantiles and
//       ROUTER_COLLAPSE/HOTSPOT/STARVATION/INSTABILITY diagnostics.
//   §23 SequenceStateBenchmark   — state_bytes_session/_token,
//       kv_bytes_token, prefill/decode tps, stream duration, restore
//       time.
//   §24 ParameterReuseProbe      — simulated cross-layer block sharing;
//       evidence for FutureArchitectureResearch only.
//   §26 PrecisionParity          — REFERENCE_FP64 vs PRODUCTION_BF16
//       candidate: logit error, top-k agreement, generation agreement,
//       fail-back to FP64 when unavailable or non-parity.

#pragma once

// ------------------------------------------------- §7 memory planner ----

struct MemoryPlan {
    int64_t weight_bytes = 0;
    int64_t kv_bytes = 0;
    int64_t prefix_cache_bytes = 0;
    int64_t recurrent_state_bytes = 0;
    int64_t vision_bytes = 0;
    int64_t workspace_bytes = 0;
    int64_t prefill_peak_bytes = 0;
    int64_t decode_peak_bytes = 0;
    int64_t context_tokens = 0;
    int64_t batch = 1;
    std::string kv_mode = "fp64";
    bool hybrid = false;
    // §7 honesty: a hybrid model's prefix cache cannot reconstruct the
    // DeltaNet recurrence — it stores K/V only, so its coverage is
    // reported separately and never credited as full-prefix reuse.
    bool prefix_reconstructs_state = true;
};

// kv_mode: fp64 | int8 | paged. Paged changes allocation granularity,
// not the per-token footprint.
inline int64_t cap_kv_elem_bytes(const xingcheng::inference::ModelConfig& c,
                                 const std::string& mode) {
    const int64_t hd = c.head_dim > 0 ? c.head_dim : 1;
    if (mode == "int8") return ((hd + 7) & ~int64_t{7}) + 8;
    return hd * 8;   // fp64 (also the paged element size)
}

inline MemoryPlan plan_memory(
    const xingcheng::inference::WeightBundle& bundle,
    int64_t context, int64_t batch, const std::string& kv_mode,
    int64_t vision_patches) {
    const auto& c = bundle.config();
    if (batch < 1) fail("MEMORY_PLAN_BAD_BATCH");
    if (context <= 0) context = c.max_position_embeddings;
    if (kv_mode != "fp64" && kv_mode != "int8" && kv_mode != "paged") {
        fail("MEMORY_PLAN_BAD_KV_MODE");
    }
    MemoryPlan p;
    p.context_tokens = context;
    p.batch = batch;
    p.kv_mode = kv_mode;
    p.hybrid = c.has_linear_layers();
    p.prefix_reconstructs_state = !p.hybrid;
    p.weight_bytes = bundle.weights_bytes();

    int64_t full_layers = 0;
    for (int64_t l = 0; l < c.num_hidden_layers; ++l) {
        if (!c.is_linear_layer(l)) ++full_layers;
    }
    const int64_t elem = cap_kv_elem_bytes(c, kv_mode);
    p.kv_bytes = full_layers * 2 * c.num_key_value_heads * elem *
                 context * batch;
    // Prefix cache is bounded by the engine's governed limit; plan
    // against one full-context entry per sequence, not the unbounded
    // theoretical cache.
    p.prefix_cache_bytes =
        p.prefix_reconstructs_state
            ? full_layers * 2 * c.num_key_value_heads * elem *
                  context + c.vocab_size * 8
            : 0;

    if (p.hybrid) {
        const int64_t conv_dim =
            2 * c.linear_num_key_heads * c.linear_key_head_dim +
            c.linear_num_value_heads * c.linear_value_head_dim;
        const int64_t per_layer =
            (c.linear_conv_kernel_dim - 1) * conv_dim +
            c.linear_num_value_heads * c.linear_key_head_dim *
                c.linear_value_head_dim;
        p.recurrent_state_bytes =
            (c.num_hidden_layers - full_layers) * per_layer * 8 * batch;
    }
    if (c.use_vision && vision_patches > 0) {
        p.vision_bytes = vision_patches *
                         (c.vision_patch_dim + c.hidden_size) * 8;
    }
    // Workspace: packed activation scratch — a few hidden-size rows per
    // token on prefill, a constant few rows on decode.
    const int64_t row = c.hidden_size * 8;
    const int64_t prefill_ws = context * row * 4;
    const int64_t decode_ws = batch * row * 8;
    p.workspace_bytes = std::max(prefill_ws, decode_ws);
    p.prefill_peak_bytes = p.weight_bytes + p.kv_bytes +
                           p.recurrent_state_bytes + p.vision_bytes +
                           prefill_ws;
    p.decode_peak_bytes = p.weight_bytes + p.kv_bytes +
                          p.recurrent_state_bytes +
                          p.prefix_cache_bytes + decode_ws;
    return p;
}

// ----------------------------------- §27 typed-state registry contract ----

inline const char* kNativeStateTypes[] = {
    "attn_kv", "int8_kv", "paged_kv", "prefix_cache",
    "deltanet_state", "vision_prefill"};

struct NativeStateRef {
    std::string type;
    std::string generation;
    std::string model_hash;
    int64_t version = 1;
    int64_t bytes = 0;
    std::string sha256;
};

// §27: every state is type-tagged, generation-bound and model-hash-bound.
// A raw state must never cross generations.
inline void validate_state_ref(const NativeStateRef& r,
                               const std::string& generation,
                               const std::string& model_hash) {
    bool known = false;
    for (const char* t : kNativeStateTypes)
        if (r.type == t) known = true;
    if (!known) fail("STATE_TYPE_UNKNOWN");
    if (!generation.empty() && r.generation != generation) {
        fail("STATE_GENERATION_MISMATCH");
    }
    if (!model_hash.empty() && r.model_hash != model_hash) {
        fail("STATE_MODEL_MISMATCH");
    }
}

// -------------------------------------- §23 delta-state snapshot envelope ----

struct DeltaStateSnapshot {
    static constexpr const char* kFormat =
        "star-delta-state-snapshot/v1";
    static constexpr int64_t kVersion = 1;
    std::string generation;
    std::string model_hash;
    std::string architecture;
    int64_t slot = 0;
    int64_t state_bytes = 0;
    std::string state_sha256;
    std::vector<char> blob;
};

inline std::string cap_sha256(const char* data, size_t n) {
    return sha256_bytes(reinterpret_cast<const unsigned char*>(data), n);
}

inline DeltaStateSnapshot delta_snapshot_save(
    xingcheng::inference::NativeInferenceEngine& engine, int64_t slot,
    const std::string& architecture) {
    DeltaStateSnapshot s;
    s.generation = engine.bundle()
        ? engine.bundle()->architecture_generation() : "";
    s.model_hash = engine.bundle()
        ? engine.bundle()->weights_sha256() : "";
    s.architecture = architecture;
    s.slot = slot;
    if (!engine.delta_state_save(slot, s.blob)) {
        fail("STATE_SNAPSHOT_UNSUPPORTED");   // dense model — no linear state
    }
    s.state_bytes = static_cast<int64_t>(s.blob.size());
    s.state_sha256 = cap_sha256(s.blob.data(), s.blob.size());
    return s;
}

// Returns nullptr on success, else the fail-closed error code. A return
// code (not fail()) keeps negative tests exercisable inside one process.
inline const char* delta_snapshot_restore(
    xingcheng::inference::NativeInferenceEngine& engine,
    const DeltaStateSnapshot& s) {
    // Verify before touching the live state — a bad blob is an error,
    // never a partial restore.
    if (cap_sha256(s.blob.data(), s.blob.size()) != s.state_sha256) {
        return "STATE_SNAPSHOT_HASH_MISMATCH";
    }
    const std::string gen = engine.bundle()
        ? engine.bundle()->architecture_generation() : "";
    // Generation binding is unconditional: a snapshot claiming a
    // different generation (or claiming one an unlabeled bundle cannot
    // prove) never loads.
    if (s.generation != gen) {
        return "STATE_GENERATION_MISMATCH";
    }
    const std::string wh = engine.bundle()
        ? engine.bundle()->weights_sha256() : "";
    if (s.model_hash != wh) {
        return "STATE_MODEL_MISMATCH";
    }
    if (!engine.delta_state_restore(s.slot, s.blob.data(),
                                    static_cast<int64_t>(s.blob.size()))) {
        return "STATE_MODEL_MISMATCH";
    }
    return nullptr;
}

// --------------------------------------- §20 vision budget controller ----

struct VisionBudgetPlan {
    std::string profile = "FULL";        // FULL|BALANCED|COMPACT
    int64_t raw_patch_count = 0;
    int64_t selected_patch_count = 0;
    std::string compression_mode = "none";   // none|pool2|pool4
    std::string task_class;
    bool experimental = true;
    std::string parity_status = "not_evaluated";
    std::string fallback_reason;
};

// §20: adaptive visual compression is EXPERIMENTAL only — without the
// OCR/chart/document/general/small-object parity benchmark every
// request resolves to FULL. parity_ok is the evidence latch.
inline VisionBudgetPlan plan_vision_budget(
    const std::string& profile, int64_t raw_patches, int64_t max_patches,
    const std::string& task_class, int64_t memory_budget_bytes,
    int64_t patch_bytes, bool parity_ok) {
    VisionBudgetPlan v;
    v.raw_patch_count = raw_patches;
    v.task_class = task_class;
    v.parity_status = parity_ok ? "passed" : "not_evaluated";
    std::string p = profile.empty() ? "FULL" : profile;
    for (auto& ch : p) ch = (char)std::toupper((unsigned char)ch);
    if (p != "FULL" && p != "BALANCED" && p != "COMPACT") {
        fail("VISION_BUDGET_BAD_PROFILE");
    }
    if (!parity_ok) {
        // Fail-back: no parity evidence → FULL, whatever was requested.
        v.profile = "FULL";
        v.compression_mode = "none";
        v.fallback_reason =
            "VISION_BUDGET_PARITY_FAILED:no_evidence";
    } else {
        v.profile = p;
        if (p == "BALANCED") v.compression_mode = "pool2";
        else if (p == "COMPACT") v.compression_mode = "pool4";
    }
    int64_t div = v.compression_mode == "pool4" ? 4 :
                  v.compression_mode == "pool2" ? 2 : 1;
    int64_t sel = raw_patches / div;
    if (max_patches > 0 && sel > max_patches) sel = max_patches;
    if (memory_budget_bytes > 0 && patch_bytes > 0) {
        const int64_t cap = memory_budget_bytes / patch_bytes;
        if (cap < sel) sel = cap;
    }
    if (sel < 1 && raw_patches > 0) sel = 1;
    v.selected_patch_count = sel;
    return v;
}

// ------------------------------------ §6 speculative decoder ABI contract ----
//
// enabled=false — the export path discards the MTP training heads, so no
// real drafter exists; this is the ABI + verification algorithm +
// synthetic probe only (never claims speculative speedup).

class SpeculativeDrafter {
public:
    virtual ~SpeculativeDrafter() = default;
    virtual std::vector<int64_t> Draft(
        const std::vector<int64_t>& ctx, int64_t k) = 0;
    virtual void Reset() = 0;
};

// Verification algorithm: acceptance = longest prefix where the target
// distribution's greedy token matches the draft token; everything after
// is rejected and the boundary token is the target's own sample.
inline int64_t spec_acceptance_prefix(
    const std::vector<int64_t>& draft,
    const std::vector<int64_t>& target_greedy) {
    int64_t n = 0;
    const int64_t lim = std::min<int64_t>(
        (int64_t)draft.size(), (int64_t)target_greedy.size());
    while (n < lim && draft[(size_t)n] == target_greedy[(size_t)n]) ++n;
    return n;
}

// Synthetic drafter for the probe: echoes the trailing token — not a
// model, a deterministic contract exerciser.
class SyntheticDrafter final : public SpeculativeDrafter {
public:
    std::vector<int64_t> Draft(
        const std::vector<int64_t>& ctx, int64_t k) override {
        std::vector<int64_t> d;
        if (ctx.empty() || k <= 0) return d;
        for (int64_t i = 0; i < k; ++i)
            d.push_back(ctx[(ctx.size() - 1 - (size_t)i % ctx.size())]);
        return d;
    }
    void Reset() override {}
};

// §11/§12/§13 NativeSpeculativeDecoder — the formal runtime contract.
// The decoder is enabled only while a drafter is bound; production
// never binds one (XCN10 exports drop the MTP heads, and no speculative
// claim may be made until the format carries a governed drafter), so
// `enabled` is structurally false outside the synthetic probe lane.
// Every entry point fails closed with SPECULATIVE_DECODER_DISABLED when
// unbound. Metrics follow §13: only net_tps_gain>0 AND generation
// parity could ever justify a later enablement review.
class NativeSpeculativeDecoder {
public:
    struct Metrics {
        int64_t draft_tokens = 0;
        int64_t accepted_tokens = 0;
        int64_t rejected_tokens = 0;
        int64_t rollback_count = 0;
        double acceptance_rate = 0;
        double draft_latency_ms = 0;
        double verify_latency_ms = 0;
        double net_tps_gain = 0;
        double net_latency_gain = 0;
    };

    /// No production ctor path binds a drafter — probes construct one
    /// with a SyntheticDrafter explicitly.
    void BindDrafter(SpeculativeDrafter* d) { drafter_ = d; }
    bool enabled() const { return drafter_ != nullptr; }
    const Metrics& metrics() const { return m_; }

    /// Arm a draft round over `ctx` for `depth` tokens. Returns nullptr
    /// on success or a fail-closed code.
    const char* PrepareDraft(const std::vector<int64_t>& ctx,
                             int64_t depth) {
        if (!enabled()) return "SPECULATIVE_DECODER_DISABLED";
        if (depth < 1 || depth > 16) return "SPEC_DRAFT_DEPTH_BOUNDS";
        committed_ = ctx;
        pending_.clear();
        draft_depth_ = depth;
        prepared_ = true;
        return nullptr;
    }

    std::vector<int64_t> DraftTokens() {
        if (!prepared_) return {};
        auto t0 = std::chrono::steady_clock::now();
        pending_ = drafter_->Draft(committed_, draft_depth_);
        m_.draft_latency_ms += 1e3 * std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t0).count();
        m_.draft_tokens += (int64_t)pending_.size();
        return pending_;
    }

    /// Verify against the target model's greedy continuation; returns
    /// the accepted prefix length. Caller supplies the greedy tokens
    /// the target produced for the same window.
    int64_t VerifyTokens(const std::vector<int64_t>& target_greedy) {
        auto t0 = std::chrono::steady_clock::now();
        int64_t acc = spec_acceptance_prefix(pending_, target_greedy);
        m_.verify_latency_ms += 1e3 * std::chrono::duration<double>(
            std::chrono::steady_clock::now() - t0).count();
        verified_prefix_ = acc;
        return acc;
    }

    /// Commit `n` accepted draft tokens (n <= verified_prefix_).
    std::vector<int64_t> AcceptPrefix(int64_t n) {
        n = std::min<int64_t>(n, verified_prefix_);
        std::vector<int64_t> acc(pending_.begin(),
                                 pending_.begin() + n);
        committed_.insert(committed_.end(), acc.begin(), acc.end());
        m_.accepted_tokens += n;
        return acc;
    }

    /// Drop pending tokens from position `pos`; the dropped tail counts
    /// as rejected and the round rolls back to committed_.
    void RejectFrom(int64_t pos) {
        if (pos < 0) pos = 0;
        if (pos < (int64_t)pending_.size()) {
            m_.rejected_tokens += (int64_t)pending_.size() - pos;
            pending_.resize((size_t)pos);
        }
        m_.rollback_count++;
    }

    /// Close the round: refresh acceptance_rate and the net-gain
    /// estimate (accepted tokens per verify ms vs a 1-token baseline).
    void CommitState() {
        if (m_.draft_tokens > 0)
            m_.acceptance_rate =
                (double)m_.accepted_tokens / (double)m_.draft_tokens;
        if (m_.verify_latency_ms > 0)
            m_.net_tps_gain =
                m_.accepted_tokens / m_.verify_latency_ms
                - 1.0 / m_.verify_latency_ms;
        m_.net_latency_gain =
            -m_.draft_latency_ms;   // draft cost is pure overhead when
                                    // verification rejects
        prepared_ = false;
        pending_.clear();
        verified_prefix_ = 0;
    }

    /// Abort the round: drop pending, count the rollback, restore the
    /// committed context. Engine-side KV/Delta rollback binds via the
    /// star-native-state/v2 envelope (state_type=SPECULATIVE_TEMP).
    void RollbackState() {
        m_.rejected_tokens += (int64_t)pending_.size();
        m_.rollback_count++;
        pending_.clear();
        verified_prefix_ = 0;
        prepared_ = false;
        if (drafter_) drafter_->Reset();
    }

    const std::vector<int64_t>& context() const { return committed_; }

private:
    SpeculativeDrafter* drafter_ = nullptr;
    Metrics m_;
    std::vector<int64_t> committed_;
    std::vector<int64_t> pending_;
    int64_t draft_depth_ = 0;
    int64_t verified_prefix_ = 0;
    bool prepared_ = false;
};

// ----------------------------------------- §8 MoE routing analyzer ----

struct Quantiles {
    double p01 = 0, p05 = 0, p25 = 0, p50 = 0, p75 = 0, p95 = 0, p99 = 0;
};

inline double qtile(const std::vector<double>& s, double q) {
    if (s.empty()) return 0.0;
    const double pos = q * static_cast<double>(s.size() - 1);
    const size_t lo = static_cast<size_t>(pos);
    const size_t hi = std::min(lo + 1, s.size() - 1);
    return s[lo] + (s[hi] - s[lo]) * (pos - static_cast<double>(lo));
}

inline Quantiles quantiles_of(std::vector<int64_t> counts) {
    std::vector<double> s(counts.begin(), counts.end());
    std::sort(s.begin(), s.end());
    Quantiles q;
    q.p01 = qtile(s, .01); q.p05 = qtile(s, .05); q.p25 = qtile(s, .25);
    q.p50 = qtile(s, .50); q.p75 = qtile(s, .75); q.p95 = qtile(s, .95);
    q.p99 = qtile(s, .99);
    return q;
}

struct RouterDiagnosis {
    std::vector<std::string> flags;   // ROUTER_* diagnostics
    double top_share = 0;             // largest single-expert share
    int64_t starved = 0;              // experts with zero routed tokens
    double instability = 0;           // top-expert flip rate (tokens sample)
};

// §8: quantile routing analysis — mean utilization alone hides
// collapse/hotspots; per-layer expert-count quantiles plus selection
// stability over the sampled tokens.
inline RouterDiagnosis analyze_router(
    const xingcheng::inference::MoeTraceLayer& layer,
    Quantiles& q) {
    RouterDiagnosis d;
    q = quantiles_of(layer.expert_counts);
    const int64_t total = std::accumulate(
        layer.expert_counts.begin(), layer.expert_counts.end(),
        int64_t{0});
    const int64_t mx = layer.expert_counts.empty()
        ? 0 : *std::max_element(layer.expert_counts.begin(),
                                layer.expert_counts.end());
    d.top_share = total > 0 ? static_cast<double>(mx) / total : 0.0;
    const double uniform = layer.expert_counts.empty()
        ? 0.0 : 1.0 / layer.expert_counts.size();
    for (int64_t c : layer.expert_counts) if (c == 0) ++d.starved;
    if (d.top_share > 0.5 || (total > 0 && q.p50 == 0.0)) {
        d.flags.emplace_back("ROUTER_COLLAPSE");
    }
    if (uniform > 0 && d.top_share > 3.0 * uniform) {
        d.flags.emplace_back("ROUTER_HOTSPOT");
    }
    if (d.starved > 0) d.flags.emplace_back("ROUTER_STARVATION");
    // Instability: fraction of sampled tokens whose argmax expert differs
    // from the layer-modal expert — a proxy for routing flip-flops.
    if (!layer.selected.empty()) {
        int64_t flips = 0;
        const int64_t modal = (int64_t)std::distance(
            layer.expert_counts.begin(),
            std::max_element(layer.expert_counts.begin(),
                             layer.expert_counts.end()));
        for (const auto& sel : layer.selected) {
            if (sel.empty()) continue;
            if (sel[0] != modal) ++flips;
        }
        d.instability =
            static_cast<double>(flips) / layer.selected.size();
        if (d.instability > 0.5) d.flags.emplace_back("ROUTER_INSTABILITY");
    }
    return d;
}

// ----------------------------------- §24 parameter-reuse research probe ----

struct ReuseProbeResult {
    int64_t weights_bytes = 0;
    int64_t weights_saved_bytes = 0;
    int64_t cache_bytes_saved = 0;
    double weights_saved_frac = 0.0;
    std::string quality_risk;
    std::string routing_complexity;
    std::string checkpoint_complexity;
};

// Simulation only — output feeds FutureArchitectureResearch; xc-fused-1
// is never modified by this probe.
inline ReuseProbeResult probe_parameter_reuse(
    const xingcheng::inference::WeightBundle& bundle) {
    const auto& c = bundle.config();
    ReuseProbeResult r;
    r.weights_bytes = bundle.weights_bytes();
    if (c.num_hidden_layers > 1) {
        // Per-layer block share ≈ weights/(layers) minus the fixed
        // embedding/head share; share all-but-one block.
        const double per_layer =
            static_cast<double>(r.weights_bytes) /
            static_cast<double>(c.num_hidden_layers);
        r.weights_saved_bytes = static_cast<int64_t>(
            per_layer * (c.num_hidden_layers - 1));
        r.weights_saved_frac =
            static_cast<double>(r.weights_saved_bytes) /
            static_cast<double>(r.weights_bytes);
    }
    r.quality_risk =
        c.use_moe
            ? "high: shared MoE block collapses depth-wise routing "
              "specialization; router load balancing must be re-derived"
            : "moderate: shared dense block removes per-layer "
              "representation stages";
    r.routing_complexity =
        c.use_moe ? "high: per-depth router tables merge into one"
                  : "low";
    r.checkpoint_complexity =
        "medium: XCN tensor names carry layer ordinals — a shared-block "
        "layout is a new checkpoint revision (deferred)";
    return r;
}

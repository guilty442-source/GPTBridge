// xcm_integration.h — multi-model advantages integration (native side).
//
// Included once by xc_modeltool.cpp inside the anonymous namespace.
// Each block converts one external family's worthwhile design into a
// xingcheng-owned runtime/telemetry/eval component. Nothing here adds
// an architecture axis, trains weights, or creates a new checkpoint
// revision — xc-fused-1 / XCN1 v10 stay canonical.

// ----------------------------------------------------------- §8 MoE --
// MoERoutingAnalyzer: quantile routing analysis (P01..P99) plus
// collapse/hotspot/starvation/instability diagnostics — never just the
// mean utilization.

static std::vector<double> xcm_quantiles(std::vector<double> v) {
    std::sort(v.begin(), v.end());
    static const double ps[] = {0.01, 0.05, 0.25, 0.50, 0.75, 0.95, 0.99};
    std::vector<double> out;
    if (v.empty()) return out;
    for (double p : ps) {
        size_t idx = static_cast<size_t>(p * (v.size() - 1) + 0.5);
        if (idx >= v.size()) idx = v.size() - 1;
        out.push_back(v[idx]);
    }
    return out;
}

static std::string xcm_moe_analyze_json(
    const std::vector<NativeInferenceEngine::RouterLayerTrace>& trace) {
    std::ostringstream o;
    o << "{\"format\":\"star-moe-routing-analysis/v1\",\"layers\":[";
    std::vector<std::string> diagnoses;
    bool first = true;
    std::unordered_map<int64_t, int64_t> load;
    for (const auto& tr : trace) {
        // per-expert load distribution + mixing-weight quantiles.
        std::unordered_map<int64_t, int64_t> local;
        for (int64_t e : tr.expert_ids) {
            ++local[e];
            ++load[e];
        }
        std::vector<double> w = tr.weights;
        std::vector<double> lq;
        for (const auto& kv : local)
            lq.push_back(static_cast<double>(kv.second));
        const auto wq = xcm_quantiles(w);
        const auto eq = xcm_quantiles(lq);
        if (!first) o << ',';
        first = false;
        o << "{\"layer_id\":" << tr.layer_id
          << ",\"router_type\":\"" << tr.router_type << "\""
          << ",\"top_k\":" << tr.top_k
          << ",\"token_count\":" << tr.token_count
          << ",\"experts_seen\":" << (int64_t)local.size()
          << ",\"shared_expert_ratio\":"
          << (tr.token_count > 0
                  ? (double)tr.shared_experts / tr.token_count : 0.0)
          << ",\"moe_router_quantiles\":[";
        for (size_t i = 0; i < wq.size(); ++i)
            o << (i ? "," : "") << wq[i];
        o << "],\"expert_load_quantiles\":[";
        for (size_t i = 0; i < eq.size(); ++i)
            o << (i ? "," : "") << eq[i];
        o << "]}";
        // diagnostics
        if (!local.empty() && tr.token_count > 0) {
            int64_t mx = 0, mn = std::numeric_limits<int64_t>::max();
            for (const auto& kv : local) {
                mx = std::max(mx, kv.second);
                mn = std::min(mn, kv.second);
            }
            double hotspot = (double)mx / tr.token_count;
            if (hotspot > 0.5)
                diagnoses.push_back("ROUTER_HOTSPOT:L" +
                                    std::to_string(tr.layer_id));
            if (local.size() == 1)
                diagnoses.push_back("ROUTER_COLLAPSE:L" +
                                    std::to_string(tr.layer_id));
            if (local.size() * tr.top_k <
                tr.token_count)  // fewer distinct experts than routed
                diagnoses.push_back("ROUTER_STARVATION:L" +
                                    std::to_string(tr.layer_id));
            if (!wq.empty() && wq.size() >= 7 &&
                wq[6] > 0.9 && tr.top_k > 1)
                diagnoses.push_back("ROUTER_INSTABILITY:L" +
                                    std::to_string(tr.layer_id));
        }
    }
    o << "],\"diagnoses\":[";
    for (size_t i = 0; i < diagnoses.size(); ++i)
        o << (i ? "," : "") << "\"" << diagnoses[i] << "\"";
    o << "]}";
    return o.str();
}

// ----------------------------------------------------------- §6 spec --

// SpeculativeDecoder: ABI + verification algorithm + acceptance mask +
// metrics + fallback. enabled stays false — export drops the MTP
// training head, so there is no usable drafter this phase. Any call
// without a bound drafter/verifier fails SPECULATIVE_DECODER_UNAVAILABLE.

class SpeculativeDecoder {
  public:
    using Drafter = std::function<std::vector<int64_t>(int64_t)>;
    // returns the target model's greedy ids for each drafted position
    // plus one bonus token.
    using Verifier =
        std::function<std::vector<int64_t>(const std::vector<int64_t>&)>;

    void set_drafter(Drafter d) { drafter_ = std::move(d); }
    void set_verifier(Verifier v) { verifier_ = std::move(v); }
    void set_enabled(bool on) { enabled_ = on; }
    bool enabled() const { return enabled_; }

    std::vector<int64_t> Draft(int64_t draft_len) {
        if (!enabled_ || !drafter_)
            throw xingcheng::inference::InferenceError(
                "SPECULATIVE_DECODER_UNAVAILABLE");
        if (draft_len <= 0 || draft_len > 64)
            throw xingcheng::inference::InferenceError(
                "SPEC_DRAFT_LEN_INVALID");
        ++draft_calls_;
        std::vector<int64_t> d = drafter_(draft_len);
        tokens_drafted_ += static_cast<int64_t>(d.size());
        pending_ = d;
        return d;
    }

    // Verification algorithm: accept the longest prefix where the
    // target model's greedy choice equals the drafted token; the first
    // mismatch terminates acceptance and the target's own token at
    // that position is emitted instead.
    std::vector<bool> Verify(const std::vector<int64_t>& drafted) {
        if (!verifier_)
            throw xingcheng::inference::InferenceError(
                "SPECULATIVE_DECODER_UNAVAILABLE");
        ++verify_calls_;
        const std::vector<int64_t> target = verifier_(drafted);
        std::vector<bool> mask(drafted.size(), false);
        accept_prefix_ = 0;
        for (size_t i = 0; i < drafted.size() &&
                            i < target.size(); ++i) {
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

// ---------------------------------------------------------- §11 FIM --

// star-fim/v1 runtime envelope: the model keeps a byte-level BPE with
// no dedicated FIM tokens, so the runtime materialises the envelope as
// literal control text (CodeGemma-style) — tokenizer assets untouched,
// no retraining.
static std::string xcm_fim_envelope(const std::string& prefix,
                                    const std::string& suffix) {
    return std::string("<|fim_prefix|>") + prefix +
           "<|fim_suffix|>" + suffix + "<|fim_middle|>";
}

// --------------------------------------------------------- §20 vision -

// VisionBudgetController (EXPERIMENTAL): picks a patch count under a
// memory budget. Any non-FULL plan is marked experimental; parity
// benchmarks (ocr/chart/document/general/small-object) must pass before
// a compressed profile may become a production default. Failure paths
// return a FULL plan rather than silently dropping visual context.
struct XcmVisionPlan {
    std::string profile = "FULL";
    int64_t raw_patch_count = 0;
    int64_t selected_patch_count = 0;
    std::string compression_mode = "none";
    bool experimental = false;
    bool fallback_full = false;
    std::string error;
};

static XcmVisionPlan xcm_vision_plan(const std::string& profile,
                                     int64_t raw_patches,
                                     int64_t vision_max_patches,
                                     int64_t memory_budget_bytes,
                                     const std::string& task_class) {
    XcmVisionPlan p;
    p.profile = profile;
    p.raw_patch_count = raw_patches;
    int64_t cap = vision_max_patches > 0 ? vision_max_patches : 64;
    int64_t patch_bytes = 0;  // caller-independent: resolved upstream

    if (profile == "FULL") {
        p.selected_patch_count = std::min(raw_patches, cap);
        p.compression_mode = "none";
        p.fallback_full = false;
        return p;
    }
    // BALANCED / COMPACT are experimental until the parity benchmark
    // suite (ocr|chart|document|general|small-object) exists.
    p.experimental = true;
    double keep = profile == "BALANCED" ? 0.5 : 0.25;
    int64_t want = std::max<int64_t>(
        1, (int64_t)std::llround(raw_patches * keep));
    want = std::min(want, cap);
    if (want >= raw_patches) {
        // nothing to compress — fall back to FULL selection.
        p.profile = "FULL";
        p.selected_patch_count = std::min(raw_patches, cap);
        p.compression_mode = "none";
        p.fallback_full = true;
        return p;
    }
    p.selected_patch_count = want;
    p.compression_mode =
        profile == "BALANCED" ? "grid-thin" : "pool-2x2";
    (void)memory_budget_bytes;
    (void)task_class;
    (void)patch_bytes;
    return p;
}

// ---------------------------------------------------- §24 reuse probe -

// ParameterReuseProbe: simulate cross-layer block sharing (Zamba-2
// lesson) WITHOUT touching xc-fused-1 — savings vs. risk land in a
// FutureArchitectureResearch report only.
static std::string xcm_reuse_probe_report(const fs::path& bundle,
                                        int64_t group) {
    JsonValue manifest =
        parse_json_file((bundle / "manifest.json").string());
    const JsonValue* tensors = manifest.get("tensors");
    if (!tensors || tensors->type != JsonValue::Type::Array)
        fail("REUSE_PROBE_MANIFEST_INVALID");
    int64_t total_bytes = 0;
    int64_t layer_bytes = 0;   // per-layer (layers.N.*) tensor bytes
    for (const auto& t : tensors->array) {
        const JsonValue* name = t.get("name");
        const JsonValue* nbytes = t.get("nbytes");
        int64_t nb = nbytes && nbytes->type == JsonValue::Type::Number
                         ? (int64_t)nbytes->number : 0;
        total_bytes += nb;
        if (name && name->type == JsonValue::Type::String &&
            name->string.rfind("layers.", 0) == 0)
            layer_bytes += nb;
    }
    if (group < 1) group = 1;
    double share = 1.0 - 1.0 / group;
    int64_t saved = (int64_t)(layer_bytes * share);
    std::ostringstream o;
    o << "{\"format\":\"star-parameter-reuse-probe/v1\","
      << "\"scope\":\"FutureArchitectureResearch\","
      << "\"simulated_group_size\":" << group
      << ",\"weights_total_bytes\":" << total_bytes
      << ",\"per_layer_weights_bytes\":" << layer_bytes
      << ",\"estimated_weights_saved_bytes\":" << saved
      << ",\"estimated_load_time_saved_ratio\":"
      << (total_bytes > 0 ? (double)saved / total_bytes : 0.0)
      << ",\"cache_saved_bytes\":0"  // KV is per-position; sharing
                                    // blocks changes nothing here.
      << ",\"quality_risk\":\"unmeasured_requires_ablation\""
      << ",\"routing_complexity\":\"router indices would alias shared "
         "blocks\""
      << ",\"checkpoint_complexity\":\"requires XCN revision; "
         "out of scope\""
      << ",\"architecture_change_required\":false"
      << ",\"note\":\"simulation only; xc-fused-1 unchanged\"}";
    return o.str();
}

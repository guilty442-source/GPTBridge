// xcm_runtime.h — shared runtime-contract helpers for xc_modeltool.
// Included once by xc_modeltool.cpp inside the anonymous namespace
// (before xcm_integration.h, whose probe modes consume these).
//
//   xcm_moe_analyze_json  §8/§29 MoERoutingAnalyzer — quantile routing
//                         analysis + ROUTER_* diagnoses over the
//                         engine's two-level router trace.
//   xcm_fim_envelope      §11 star-fim/v1 runtime envelope — literal
//                         control markers; tokenizer untouched.
#pragma once

std::string xcm_moe_analyze_json(
    const std::vector<NativeInferenceEngine::RouterLayerTrace>&
        layers) {
    auto esc = [](const std::string& s) {
        return gptbridge::jsonlite::json_escape(s);
    };
    std::ostringstream o;
    o << "{\"layers\":[";
    bool first = true;
    std::vector<std::string> router_types;
    for (const auto& tr : layers) {
        if (std::find(router_types.begin(), router_types.end(),
                      tr.router_type) == router_types.end())
            router_types.push_back(tr.router_type);
        // per-expert selection counts -> quantiles (never just a mean)
        std::vector<int64_t> vals(256, 0);
        int64_t max_id = -1;
        for (int64_t t = 0; t < tr.token_count; ++t)
            for (int64_t k = 0; k < tr.top_k; ++k) {
                int64_t e = tr.expert_ids[(size_t)(t * tr.top_k + k)];
                if (e >= (int64_t)vals.size())
                    vals.resize((size_t)e + 1, 0);
                vals[(size_t)e]++;
                max_id = std::max(max_id, e);
            }
        vals.resize((size_t)max_id + 1);
        std::sort(vals.begin(), vals.end());
        auto q = [&](double p) -> double {
            if (vals.empty()) return 0.0;
            double idx = p * (vals.size() - 1);
            size_t lo = (size_t)idx,
                   hi = std::min(lo + 1, vals.size() - 1);
            return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo);
        };
        double p50 = q(0.50), p99 = q(0.99);
        int64_t total = 0, zero_e = 0, max_e = 0;
        for (int64_t v : vals) {
            total += v;
            if (v == 0) ++zero_e;
            max_e = std::max(max_e, v);
        }
        std::vector<std::string> diag;
        if (!vals.empty()) {
            if (max_e >= total * 9 / 10 && vals.size() > 1)
                diag.push_back("ROUTER_COLLAPSE");
            if (p50 > 0 && p99 > p50 * 8)
                diag.push_back("ROUTER_HOTSPOT");
            if (zero_e > (int64_t)vals.size() / 2)
                diag.push_back("ROUTER_STARVATION");
            double hhi = 0.0;
            for (int64_t v : vals) {
                double share = v / (double)std::max<int64_t>(1, total);
                hhi += share * share;
            }
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

std::string xcm_fim_envelope(const std::string& prefix,
                             const std::string& suffix) {
    return "<|fim_prefix|>" + prefix + "<|fim_suffix|>" + suffix +
           "<|fim_middle|>";
}

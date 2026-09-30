// xcm_integration.h — integration modes not already covered by
// xcm_runtime.h (which owns memplan / statebench / spec-probe /
// vision-budget / context-probe / reuse-probe / moe-analyze helpers).
// Included once by xc_modeltool.cpp after xcm_runtime.h.
//
//   depth-probe       --bundle <dir>           §8 depth telemetry
//   moe-analyze       --bundle <dir>           §8/§29 quantiles+diagnoses
//   provenance-check  --bundle <dir>           §25 bundle provenance
#pragma once

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
        if (!t || t->type != JsonValue::Type::Array ||
            t->array.empty())
            failures.push_back("tensors missing");
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

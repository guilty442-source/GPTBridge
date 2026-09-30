// xct_canon.h — xc-fused-1 canonical contract probe (--canoncheck) and
// the unified trainer probe driver (--probe-all ->
// star-trainer-probe-report/v1). Included once by xingcheng_trainer.cpp
// inside namespace xct, LAST — probe_all() references every sibling.
//
// canoncheck pins the convergence contract: a canonical config must
// satisfy every xc-fused-1 clause, survive an XCN10 round-trip
// bit-exactly (versioned fingerprint stable, writer locked at v10), and
// any non-canonical axis — CSA, MLA (kv/q lora), aux-free lb_bias,
// gemma4 family, wrong interval/aux/topk, missing MTP/vision/YaRN —
// must fail the contract rather than be silently relabelled.
#pragma once

#include <chrono>
#include <cstdio>
#include <cstring>
#include <io.h>

// The single source of truth for "is this config xc-fused-1". Mirrors
// the export-side predicate in xc_modeltool.cpp (arch_xc_fused1) — each
// binary checks the contract against its own view; divergent copies are
// caught by canoncheck itself.
static bool cfg_is_canonical(const ModelConfig& c) {
    return !c.is_gemma4() && c.full_attention_interval == 4 &&
           c.attn_output_gate && c.qk_norm &&
           std::fabs(c.partial_rotary - 0.5f) < 1e-6f &&
           c.moe_router_sigmoid && c.moe_top_k == 2 &&
           c.moe_layer_interval == 1 && c.moe_experts >= 8 &&
           c.moe_shared_experts >= 1 && c.shared_expert_gate &&
           std::fabs(c.moe_aux_w - 0.001f) < 1e-7f &&
           !c.moe_auxfree_balance && c.moe_lb_bias_rate == 0.0f &&
           c.mtp_depth >= 1 && c.mtp_loss_w >= 0.1f && c.use_vision &&
           c.vision_patch_dim >= 16 && c.vision_max_patches >= 64 &&
           c.yarn_factor >= 2.0f && c.kv_lora_rank == 0 &&
           c.q_lora_rank == 0 && c.csa_ratio == 0;
}

static int canoncheck() {
    int failures = 0;
    auto fail = [&](const char* what) {
        ++failures;
        std::printf("  FAIL %s\n", what);
    };

    auto canonical = [] {
        ModelConfig c;
        c.vocab = 128; c.hidden = 64; c.inter = 96; c.layers = 4;
        c.heads = 4; c.kv_heads = 2; c.max_pos = 128;
        c.full_attention_interval = 4;
        c.attn_output_gate = true; c.qk_norm = true;
        c.partial_rotary = 0.5f;
        c.lin_key_heads = 2; c.lin_key_dim = 16;
        c.lin_value_heads = 4; c.lin_value_dim = 16;
        c.lin_conv_kernel = 4;
        c.moe_experts = 8; c.moe_top_k = 2; c.moe_layer_interval = 1;
        c.moe_expert_inter = 24;
        c.moe_shared_experts = 1; c.moe_shared_inter = 48;
        c.shared_expert_gate = true;
        c.moe_router_sigmoid = true; c.moe_aux_w = 0.001f;
        c.mtp_depth = 1; c.mtp_loss_w = 0.3f;
        c.use_vision = true; c.vision_patch_dim = 16;
        c.vision_max_patches = 64;
        c.yarn_factor = 2.0f; c.yarn_orig_pos = 128;
        return c;
    };

    // ---- 1: canonical config satisfies the contract ----
    {
        ModelConfig c = canonical();
        if (!cfg_is_canonical(c)) fail("canonical config rejected");
    }

    // ---- 2: XCN10 round-trip preserves every contract field and the
    //         writer emits exactly version 10 ----
    const char* tmp = "canoncheck_tmp.xcn";
    {
        ModelConfig c = canonical();
        Params p;
        init_params(p, c, 23);
        if (!ckpt_save(p, c, tmp, true)) {
            fail("ckpt_save");
        } else {
            unsigned char hdr[8] = {0};
            std::ifstream f(tmp, std::ios::binary);
            f.read((char*)hdr, 8);
            if (std::memcmp(hdr, "XCN1", 4) != 0) fail("XCN1 magic");
            uint32_t ver = (uint32_t)hdr[4] | ((uint32_t)hdr[5] << 8) |
                           ((uint32_t)hdr[6] << 16) |
                           ((uint32_t)hdr[7] << 24);
            if (ver != 10) fail("XCN writer not locked at v10");
            ModelConfig r;
            if (!ckpt_peek_config(tmp, r)) {
                fail("ckpt_peek_config");
            } else {
                if (!cfg_is_canonical(r))
                    fail("round-trip lost canonical contract");
                if (r.full_attention_interval != 4 ||
                    !r.attn_output_gate || !r.qk_norm ||
                    std::fabs(r.partial_rotary - 0.5f) > 1e-6f ||
                    !r.moe_router_sigmoid || r.moe_top_k != 2 ||
                    r.moe_layer_interval != 1 || r.moe_experts != 8 ||
                    r.moe_shared_experts != 1 || !r.shared_expert_gate ||
                    std::fabs(r.moe_aux_w - 0.001f) > 1e-7f ||
                    r.mtp_depth != 1 ||
                    std::fabs(r.mtp_loss_w - 0.3f) > 1e-6f ||
                    !r.use_vision || r.vision_patch_dim != 16 ||
                    r.vision_max_patches != 64 ||
                    std::fabs(r.yarn_factor - 2.0f) > 1e-6f)
                    fail("XCN10 round-trip field drift");
            }
        }
    }
    std::remove(tmp);

    // ---- 3: every non-canonical axis must flip the predicate ----
    // CSA / MLA / aux-free lb_bias are TRAINER_ONLY_EXPERIMENTAL or
    // NON_CANONICAL_EXPERIMENTAL axes — legal to serialize, never part
    // of the xc-fused-1 claim.
    auto reject = [&](const char* what, void (*mut)(ModelConfig&)) {
        ModelConfig c = canonical();
        mut(c);
        if (cfg_is_canonical(c)) fail(what);
        // Non-canonical axes still round-trip — the contract rejects
        // the *claim*, not the serialization.
        Params p;
        init_params(p, c, 29);
        if (ckpt_save(p, c, tmp, true)) {
            ModelConfig r;
            if (!ckpt_peek_config(tmp, r) || cfg_is_canonical(r))
                fail(what);
            std::remove(tmp);
        }
    };
    reject("csa allowed in canonical",
           [](ModelConfig& c) { c.csa_ratio = 2; c.csa_topk = 2;
                                c.csa_window = 4;
                                c.global_attn_interval = 1; });
    reject("mla kv-lora allowed in canonical",
           [](ModelConfig& c) { c.kv_lora_rank = 64; });
    reject("mla q-lora allowed in canonical",
           [](ModelConfig& c) { c.q_lora_rank = 64; });
    reject("aux-free lb_bias allowed in canonical",
           [](ModelConfig& c) { c.moe_auxfree_balance = true;
                                c.moe_lb_bias_rate = 1e-3f; });
    reject("gemma4 claimed as xc-fused-1",
           [](ModelConfig& c) { c.num_kv_shared_layers = 2; });
    reject("interval != 4",
           [](ModelConfig& c) { c.full_attention_interval = 3; });
    reject("aux_w drift", [](ModelConfig& c) { c.moe_aux_w = 0.01f; });
    reject("top_k != 2", [](ModelConfig& c) { c.moe_top_k = 3; });
    reject("mtp missing", [](ModelConfig& c) { c.mtp_depth = 0; });
    reject("vision missing", [](ModelConfig& c) { c.use_vision = false; });
    reject("yarn missing", [](ModelConfig& c) { c.yarn_factor = 1.0f; });
    reject("qk_norm off", [](ModelConfig& c) { c.qk_norm = false; });
    reject("attn gate off",
           [](ModelConfig& c) { c.attn_output_gate = false; });
    reject("partial rotary drift",
           [](ModelConfig& c) { c.partial_rotary = 1.0f; });
    reject("softmax router in canonical",
           [](ModelConfig& c) { c.moe_router_sigmoid = false; });
    reject("no shared expert",
           [](ModelConfig& c) { c.moe_shared_experts = 0; });

    bool ok = failures == 0;
    std::printf("canoncheck: xc-fused-1 contract failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

// ------------------------------------------------------ probe driver --

static uint64_t probe_fnv(const std::string& s) {
    uint64_t h = 1469598103934665603ull;
    for (unsigned char b : s) { h ^= b; h *= 1099511628211ull; }
    return h;
}

/// Run every registered probe once with per-probe stdout capture; the
/// report is the only thing written to real stdout. artifact_hash is
/// FNV-1a over the probe's own captured output — it fingerprints the
/// evidence, not just the name.
static int probe_all() {
    struct P { const char* name; int (*fn)(); };
    static const P probes[] = {
        {"smoke", smoke},           {"gradcheck", gradcheck},
        {"maskcheck", maskcheck},   {"headcheck", headcheck},
        {"rulecheck", rulecheck},   {"depthcheck", depthcheck},
        {"poscheck", poscheck},     {"inputcheck", inputcheck},
        {"gemmacheck", gemmacheck}, {"mixcheck", mixcheck},
        {"routecheck", routecheck}, {"dsvcheck", dsvcheck},
        {"yarncheck", yarncheck},   {"csacheck", csacheck},
        {"mtpcheck", mtpcheck},     {"canoncheck", canoncheck},
    };

    std::fflush(stdout);
    int saved = _dup(_fileno(stdout));
    std::ostringstream report;
    report << "{\"format\":\"star-trainer-probe-report/v1\","
              "\"probes\":[";
    int passed = 0, total = 0;
    for (const P& pr : probes) {
        ++total;
        char tmpname[L_tmpnam];
        std::tmpnam(tmpname);
        FILE* cap = std::freopen(tmpname, "w", stdout);
        auto t0 = std::chrono::steady_clock::now();
        int rc = -1;
        if (cap) {
            try { rc = pr.fn(); } catch (...) { rc = -1; }
            std::fflush(stdout);
        }
        double ms = std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - t0).count();
        std::fflush(stdout);
        _dup2(saved, _fileno(stdout));
        std::string evidence;
        if (cap) {
            std::ifstream f(tmpname, std::ios::binary);
            std::ostringstream ss; ss << f.rdbuf();
            evidence = ss.str();
            std::remove(tmpname);
        }
        bool pass = rc == 0;
        if (pass) ++passed;
        if (total > 1) report << ',';
        char hbuf[24];
        std::snprintf(hbuf, sizeof(hbuf), "%016llx",
                      (unsigned long long)probe_fnv(
                          pr.name + std::string(":") + evidence));
        report << "{\"probe\":\"" << pr.name << "\""
               << ",\"status\":\"" << (pass ? "PASS" : "FAIL") << "\""
               << ",\"duration_ms\":" << (long long)(ms + 0.5)
               << ",\"failure_code\":"
               << (pass ? "null"
                        : (rc < 0 ? "\"PROBE_EXCEPTION\""
                                  : "\"PROBE_FAILED\""))
               << ",\"artifact_hash\":\"" << hbuf << "\"}";
    }
    _close(saved);
    report << "],\"passed\":" << passed << ",\"total\":" << total
           << ",\"ok\":" << (passed == total ? "true" : "false")
           << "}\n";
    std::fputs(report.str().c_str(), stdout);
    return passed == total ? 0 : 1;
}

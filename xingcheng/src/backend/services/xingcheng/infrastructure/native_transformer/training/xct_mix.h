// xct_mix.h — B94 fragment of xingcheng_trainer.cpp (dense/sparse MoE probe).
// Included once by xingcheng_trainer.cpp inside namespace xct, after
// xct_pos.h.
//
// MoE mixing contract: every FFN layer is exactly one of two kinds —
//   dense  (l % moe_layer_interval != 0): one SwiGLU FFN processes every
//          token — params w1/w3/w2, no router
//   sparse (l % moe_layer_interval == 0): a softmax router picks top-K of
//          E experts per token; the layer output is the renormalized
//          weighted sum of only those experts plus the always-on shared
//          experts (optionally sigmoid-gated)
// --mixcheck proves executably:
//   topology     — sparse layers carry gate+experts(+shared) and no dense
//                  w1; dense layers carry w1/w3/w2 and no gate
//   routing      — exactly K distinct experts per token, weights
//                  renormalized to 1, selection = top-K of gate_probs
//   recompute    — the layer output equals Σ_s w_s·expert_s(n2) + shared
//                  (sparse) resp. w2·SwiGLU(n2) (dense), rebuilt from the
//                  forward caches only
//   isolation    — an expert no token routed to may be perturbed without
//                  moving the logits (sparsity is real compute-skipping);
//                  a routed expert's perturbation moves them
//   token-choice — routing is per-row, not a global switch
#pragma once

static int mixcheck() {
    int failures = 0;
    auto fail = [&](const char* what) {
        ++failures;
        std::printf("  FAIL %s\n", what);
    };
    const float TOL = 5e-3f;

    // Stack: layer 0 sparse (interval 2), layer 1 dense.
    ModelConfig c;
    c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 2;
    c.heads = 2; c.kv_heads = 1; c.max_pos = 64;
    c.attn_output_gate = true;
    c.qk_norm = true;
    c.moe_experts = 4; c.moe_top_k = 2; c.moe_layer_interval = 2;
    c.moe_expert_inter = 24; c.moe_shared_experts = 1;
    c.moe_shared_inter = 24; c.shared_expert_gate = true;
    Params p;
    init_params(p, c, 43);
    std::vector<int> ids = {3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41};
    const int T = (int)ids.size();
    const int H = c.hidden;
    const int E = c.moe_experts, K = c.moe_top_k;
    const int EI = c.expert_inter(), SI = c.shared_inter();

    // ---- topology: each layer is exactly one kind -------------------------
    for (int l = 0; l < c.layers; ++l) {
        const bool sparse = c.moe_experts > 0 &&
                            (l % c.moe_layer_interval == 0);
        const bool has_gate = p.w.count(ln(l, "gate")) != 0;
        const bool has_dense = p.w.count(ln(l, "w1")) != 0;
        if (sparse != has_gate || sparse == has_dense)
            fail("topology: layer kind");
        if (sparse) {
            for (int e = 0; e < E; ++e)
                for (const char* s : {"w1", "w3", "w2"})
                    if (!p.w.count(ln(l, "experts.") +
                                   std::to_string(e) + "." + s))
                        fail("topology: missing expert");
            for (int se = 0; se < c.moe_shared_experts; ++se)
                for (const char* s : {"w1", "w3", "w2"})
                    if (!p.w.count(ln(l, "shared.") +
                                   std::to_string(se) + "." + s))
                        fail("topology: missing shared expert");
        }
    }

    Fwd fw;
    fwd(p, c, ids, fw);

    // Layer output recovery: proj = next-layer x_in − x_res (x_fin for the
    // last layer) — the FFN branch result for either kind.
    auto proj_row = [&](int l, int t) {
        const LayerCache& L = fw.layers[(size_t)l];
        const float* nxt = (l + 1 < c.layers)
            ? fw.layers[(size_t)l + 1].x_in.data() : fw.x_fin.data();
        std::vector<float> pr((size_t)H);
        for (int i = 0; i < H; ++i)
            pr[(size_t)i] = nxt[(size_t)t * H + i] -
                            L.x_res[(size_t)t * H + i];
        return pr;
    };

    for (int l = 0; l < c.layers; ++l) {
        const LayerCache& L = fw.layers[(size_t)l];
        const bool sparse = c.moe_experts > 0 &&
                            (l % c.moe_layer_interval == 0);
        if (sparse) {
            // ---- routing: top-K of gate_probs, distinct, renormalized --
            for (int t = 0; t < T; ++t) {
                const float* gp = L.gate_probs.data() + (size_t)t * E;
                std::vector<int> order((size_t)E);
                std::iota(order.begin(), order.end(), 0);
                std::partial_sort(order.begin(), order.begin() + K,
                                  order.end(), [&](int a, int b) {
                                      if (gp[a] != gp[b]) return gp[a] > gp[b];
                                      return a < b;
                                  });
                float wsum = 0.0f;
                std::unordered_map<int, bool> seen;
                for (int s = 0; s < K; ++s) {
                    int e = L.moe_idx[(size_t)t * K + s];
                    if (e != order[(size_t)s]) fail("routing: not top-K");
                    if (seen[e]) fail("routing: duplicate expert");
                    seen[e] = true;
                    wsum += L.moe_w[(size_t)t * K + s];
                }
                if (std::fabs(wsum - 1.0f) > 1e-4f)
                    fail("routing: weights not renormalized");
            }
            // ---- token-choice: at least two rows route differently ----
            {
                int first = L.moe_idx[0];
                bool differ = false;
                for (int t = 1; t < T && !differ; ++t)
                    for (int s = 0; s < K; ++s)
                        if (L.moe_idx[(size_t)t * K + s] != first)
                            differ = true;
                if (!differ) fail("routing: global not per-token");
            }
            // ---- recompute: proj = Σ w_s·expert_s + shared ---------------
            for (int t = 0; t < T; ++t) {
                std::vector<float> expect((size_t)H, 0.0f);
                for (int s = 0; s < K; ++s) {
                    int e = L.moe_idx[(size_t)t * K + s];
                    std::string b = ln(l, "experts.") + std::to_string(e) + ".";
                    std::vector<float> eo((size_t)H);
                    linear_fwd(L.mfh[(size_t)t * K + s].data(),
                               p.w.at(b + "w2"), eo.data(), 1, EI, H);
                    float w = L.moe_w[(size_t)t * K + s];
                    for (int i = 0; i < H; ++i)
                        expect[(size_t)i] += w * eo[(size_t)i];
                }
                for (int se = 0; se < c.moe_shared_experts; ++se) {
                    std::string b = ln(l, "shared.") + std::to_string(se) + ".";
                    std::vector<float> fh((size_t)SI);
                    for (int i = 0; i < SI; ++i)
                        fh[(size_t)i] =
                            silu_f(L.sfa[(size_t)se][(size_t)t * SI + i]) *
                            L.sfb[(size_t)se][(size_t)t * SI + i];
                    std::vector<float> so((size_t)H);
                    linear_fwd(fh.data(), p.w.at(b + "w2"), so.data(),
                               1, SI, H);
                    float g = L.shared_gate_sig.empty()
                                  ? 1.0f : L.shared_gate_sig[(size_t)t];
                    for (int i = 0; i < H; ++i)
                        expect[(size_t)i] += g * so[(size_t)i];
                }
                std::vector<float> got = proj_row(l, t);
                for (int i = 0; i < H; ++i)
                    if (std::fabs(got[(size_t)i] - expect[(size_t)i]) > TOL)
                        fail("recompute: sparse output");
            }
        } else {
            // ---- recompute: dense output = w2·SwiGLU(n2) ------------------
            for (int t = 0; t < T; ++t) {
                std::vector<float> fh((size_t)c.inter);
                for (int i = 0; i < c.inter; ++i)
                    fh[(size_t)i] =
                        silu_f(L.fa[(size_t)t * c.inter + i]) *
                        L.fb[(size_t)t * c.inter + i];
                std::vector<float> expect((size_t)H);
                linear_fwd(fh.data(), p.w.at(ln(l, "w2")), expect.data(),
                           1, c.inter, H);
                std::vector<float> got = proj_row(l, t);
                for (int i = 0; i < H; ++i)
                    if (std::fabs(got[(size_t)i] - expect[(size_t)i]) > TOL)
                        fail("recompute: dense output");
            }
        }
    }

    // ---- isolation: unrouted expert perturbation is invisible ------------
    // Dedicated wide-E config (E > T·K guarantees unrouted experts).
    {
        ModelConfig c2;
        c2.vocab = 64; c2.hidden = 32; c2.inter = 48; c2.layers = 1;
        c2.heads = 2; c2.kv_heads = 1; c2.max_pos = 64;
        c2.moe_experts = 8; c2.moe_top_k = 1; c2.moe_layer_interval = 1;
        c2.moe_expert_inter = 24;
        Params p2;
        init_params(p2, c2, 47);
        std::vector<int> ids2 = {3, 5, 7, 11};
        Fwd f0;
        fwd(p2, c2, ids2, f0);
        const LayerCache& L0 = f0.layers[0];
        bool routed[8] = {};
        for (size_t a = 0; a < L0.moe_idx.size(); ++a)
            routed[L0.moe_idx[a]] = true;
        int free_e = -1, used_e = -1;
        for (int e = 0; e < 8; ++e) {
            if (!routed[e] && free_e < 0) free_e = e;
            if (routed[e] && used_e < 0) used_e = e;
        }
        if (free_e < 0 || used_e < 0)
            fail("isolation: no unrouted expert");
        else {
            Params pu = p2;
            for (auto& x : pu.w.at(ln(0, "experts.") +
                                 std::to_string(free_e) + ".w2").d)
                x += 0.5f;
            Fwd fu;
            fwd(pu, c2, ids2, fu);
            if (std::memcmp(f0.logits.data(), fu.logits.data(),
                            f0.logits.size() * sizeof(float)) != 0)
                fail("isolation: unrouted expert leaks");
            Params pr = p2;
            for (auto& x : pr.w.at(ln(0, "experts.") +
                                 std::to_string(used_e) + ".w2").d)
                x += 0.5f;
            Fwd fr;
            fwd(pr, c2, ids2, fr);
            if (std::memcmp(f0.logits.data(), fr.logits.data(),
                            f0.logits.size() * sizeof(float)) == 0)
                fail("isolation: routed expert inert");
        }
    }

    bool ok = failures == 0;
    std::printf("mixcheck: dense/sparse MoE mixing failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

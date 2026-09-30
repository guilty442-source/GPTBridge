// xct_route.h — B94 fragment of xingcheng_trainer.cpp (fused router probe).
// Included once by xingcheng_trainer.cpp inside namespace xct, after
// xct_mix.h.
//
// --routecheck: the fused MoE router contract. The fusion keeps
// Qwen3-A3B's deterministic top-k + renormalized mixing (probed by
// --mixcheck) and adds Qwen3.5's per-expert sigmoid scoring —
// moe_router_sigmoid swaps the scoring function without touching the
// weight or top-k contract:
//   scores   — softmax mode: probs are a partition of unity per token;
//              sigmoid mode: probs == sigmoid(logits) bitwise, stay in
//              (0,1) and are expert-independent (no shared denominator)
//   select   — both modes pick the top-K by score (deterministic index
//              tiebreak) and renorm mixing weights over the selected
//   modeswap — flipping the flag alone changes the routing decision
//   reach    — boosting a non-selected expert's gate column reroutes it
//   grads    — backward delivers finite, nonzero router grads under
//              sigmoid scoring
//   geometry — fine-grained experts use moe_expert_inter, not the dense
//              intermediate size
#pragma once

static int routecheck() {
    int failures = 0;
    auto fail = [&](const char* what, int mode) {
        ++failures;
        std::printf("  FAIL %s mode=%s\n", what, mode ? "sigmoid" : "softmax");
    };

    Fwd cached[2];
    Params base;
    for (int sig = 0; sig < 2; ++sig) {
        ModelConfig c;
        c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 2;
        c.heads = 2; c.kv_heads = 1; c.max_pos = 64;
        c.attn_output_gate = true;
        c.qk_norm = true;
        c.partial_rotary = 0.5f;
        c.moe_experts = 8; c.moe_top_k = 3; c.moe_layer_interval = 1;
        c.moe_expert_inter = 24; c.moe_shared_experts = 1;
        c.moe_shared_inter = 24; c.shared_expert_gate = true;
        c.moe_router_sigmoid = sig != 0;
        Params p;
        init_params(p, c, 41);
        if (sig == 0) base = p;
        std::vector<int> ids = {3, 5, 7, 11, 13, 17, 19, 23};
        const int T = (int)ids.size();
        const int E = c.moe_experts, K = c.moe_top_k;
        const int EI = c.expert_inter();
        Fwd& fw = cached[sig];
        fwd(p, c, ids, fw);

        // ---- geometry: fine-grained experts ---------------------------
        if (EI == c.inter) fail("geometry: not fine-grained", sig);
        for (int l = 0; l < c.layers; ++l)
            for (int e = 0; e < E; ++e) {
                const Tensor& w1 = p.w.at(ln(l, "experts.")
                                          + std::to_string(e) + ".w1");
                if (w1.shape.size() != 2 || w1.shape[0] != EI ||
                    w1.shape[1] != c.hidden)
                    fail("geometry: expert shape", sig);
            }

        for (int l = 0; l < c.layers; ++l) {
            const LayerCache& L = fw.layers[l];
            const std::vector<float>& gp = L.gate_probs;
            const std::vector<float>& gl = L.gate_logits;
            for (int t = 0; t < T; ++t) {
                // ---- scores -------------------------------------------
                float sum = 0.0f;
                for (int e = 0; e < E; ++e) {
                    float s = gp[(size_t)t * E + e];
                    sum += s;
                    if (!(s > 0.0f && s < 1.0f))
                        fail("scores: range", sig);
                    if (sig &&
                        s != sigmoid_f(gl[(size_t)t * E + e]))
                        fail("scores: sigmoid cache", sig);
                }
                if (!sig && std::fabs(sum - 1.0f) > 1e-5f)
                    fail("scores: softmax unity", sig);
                // ---- select: top-K by score + renorm -------------------
                std::vector<int> ord(E);
                std::iota(ord.begin(), ord.end(), 0);
                std::partial_sort(ord.begin(), ord.begin() + K, ord.end(),
                                  [&](int a, int b) {
                                      float pa = gp[(size_t)t * E + a],
                                            pb = gp[(size_t)t * E + b];
                                      return pa != pb ? pa > pb : a < b;
                                  });
                float wsum = 0.0f, wacc = 0.0f;
                for (int k = 0; k < K; ++k)
                    wsum += gp[(size_t)t * E + ord[k]];
                for (int k = 0; k < K; ++k) {
                    int e = L.moe_idx[(size_t)t * K + k];
                    if (e != ord[k]) fail("select: top-k order", sig);
                    float w = L.moe_w[(size_t)t * K + k];
                    wacc += w;
                    if (std::fabs(w - gp[(size_t)t * E + e] / wsum) > 1e-6f)
                        fail("select: renorm weight", sig);
                }
                if (std::fabs(wacc - 1.0f) > 1e-5f)
                    fail("select: mixing unity", sig);
            }
        }

        // ---- grads: finite everywhere, router signal nonzero ----------
        std::vector<int> lab = ids;
        shift_labels(lab);
        p.zero_grad();
        Fwd f;
        fwd(p, c, ids, f);
        std::vector<float> dl;
        ce_loss(f.logits, lab, T, c.vocab, dl);
        bwd(p, c, ids, f, dl, 1.0f);
        for (auto& n : p.order)
            for (float x : p.g[n].d)
                if (!std::isfinite(x)) fail("grads: non-finite", sig);
        bool any = false;
        for (float x : p.g.at(ln(0, "gate")).d)
            if (x != 0.0f) { any = true; break; }
        if (!any) fail("grads: zero router grad", sig);

        // ---- ckpt: the flag survives the XCN6 header round-trip -------
        {
            const char* tmp = "xct_routecheck_ckpt.tmp";
            if (!ckpt_save(p, c, tmp, true)) fail("ckpt: save", sig);
            ModelConfig c2;
            if (!ckpt_peek_config(tmp, c2) ||
                c2.moe_router_sigmoid != c.moe_router_sigmoid)
                fail("ckpt: router flag", sig);
            std::remove(tmp);
        }

        if (sig) {
            // ---- reach: boosting a cold expert's gate column reroutes --
            Params p2 = base;
            const LayerCache& L0 = cached[1].layers[0];
            int cold = -1;
            float worst = 2.0f;
            for (int e = 0; e < E; ++e)
                if (L0.gate_probs[e] < worst) {
                    worst = L0.gate_probs[e];
                    cold = e;
                }
            bool selected0 = false;
            for (int k = 0; k < K; ++k)
                if (L0.moe_idx[k] == cold) selected0 = true;
            if (selected0) fail("reach: fixture", sig);
            float* gw = p2.w.at(ln(0, "gate")).d.data();
            for (int i = 0; i < c.hidden; ++i)
                gw[(size_t)cold * c.hidden + i] += 1.0f;
            Fwd fw2;
            fwd(p2, c, ids, fw2);
            if (std::memcmp(L0.moe_idx.data(), fw2.layers[0].moe_idx.data(),
                            (size_t)T * K * sizeof(int)) == 0 &&
                std::memcmp(L0.moe_w.data(), fw2.layers[0].moe_w.data(),
                            (size_t)T * K * sizeof(float)) == 0)
                fail("reach: gate column vacuous", sig);
            if (std::memcmp(cached[1].logits.data(), fw2.logits.data(),
                            cached[1].logits.size() * sizeof(float)) == 0)
                fail("reach: logits unchanged", sig);
        }
    }

    // ---- modeswap: the flag alone changes the routing decision --------
    if (std::memcmp(cached[0].layers[0].gate_probs.data(),
                    cached[1].layers[0].gate_probs.data(),
                    cached[0].layers[0].gate_probs.size() *
                        sizeof(float)) == 0)
        fail("modeswap: flag inert", 1);

    bool ok = failures == 0;
    std::printf("routecheck: fused-router failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

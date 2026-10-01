// xct_depth.h — B94 fragment of xingcheng_trainer.cpp (multi-layer probe).
// Included once by xingcheng_trainer.cpp inside namespace xct, after
// xct_job.h.
//
// --depthcheck: the fused multi-layer network probed at depth. The other
// probes exercise layers=2 slices; this one stacks 8 hybrid layers
// (deltanet / gated-attention interleave with MoE FFN on alternating
// layers) and asserts the depth contracts executably:
//   topology  — per-layer params match the layer kind at depth and the
//               linear/full interleave count is exact
//   residual  — each layer moves the residual stream (x_in[l] !=
//               x_in[l-1]); no degenerate pass-through layer
//   live      — perturbing each layer's norm1 moves the logits (every
//               layer is wired into the forward path)
//   reach     — backward delivers finite grads everywhere and nonzero
//               signal to layer-0 weights (the deepest path)
//   learnable — a few AdamW steps reduce the loss on the deep stack
#pragma once

static int depthcheck() {
    int failures = 0;
    auto fail = [&](const char* what) {
        ++failures;
        std::printf("  FAIL %s\n", what);
    };
    // 8-layer fused stack: layers 0,1 deltanet | 2 attn | 3,4 deltanet |
    // 5 attn | 6,7 deltanet (6 linear + 2 full); MoE FFN on layers
    // 0,2,4,6 with a gated shared expert — every layer kind appears
    // multiple times at depth.
    ModelConfig c;
    c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 8;
    c.heads = 2; c.kv_heads = 1; c.max_pos = 64;
    c.full_attention_interval = 3;
    c.attn_output_gate = true;
    c.qk_norm = true;
    c.partial_rotary = 0.5f;
    c.lin_key_heads = 1; c.lin_key_dim = 32;
    c.lin_value_heads = 2; c.lin_value_dim = 32;
    c.lin_conv_kernel = 4;
    c.moe_experts = 2; c.moe_top_k = 1; c.moe_layer_interval = 2;
    c.moe_expert_inter = 24; c.moe_shared_experts = 1;
    c.moe_shared_inter = 24; c.shared_expert_gate = true;
    Params p;
    init_params(p, c, 23);
    std::vector<int> ids = {3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41};
    const int T = (int)ids.size();

    // ---- topology at depth ----------------------------------------------
    int nlin = 0, nattn = 0;
    for (int l = 0; l < c.layers; ++l) {
        if (c.is_linear(l)) {
            ++nlin;
            for (const char* s : {"lin.in_proj_qkv", "lin.in_proj_z",
                                  "lin.in_proj_a", "lin.in_proj_b",
                                  "lin.conv1d", "lin.A_log", "lin.dt_bias",
                                  "lin.norm", "lin.out_proj"})
                if (!p.w.count(ln(l, s))) fail("topology: missing lin param");
        } else {
            ++nattn;
            for (const char* s : {"wq", "wk", "wv", "wo",
                                  "q_norm", "k_norm"})
                if (!p.w.count(ln(l, s))) fail("topology: missing attn param");
        }
        if (l % c.moe_layer_interval == 0 && !p.w.count(ln(l, "gate")))
            fail("topology: missing moe gate");
    }
    if (nlin != 6 || nattn != 2) fail("topology: hybrid interleave count");

    // ---- baseline forward ------------------------------------------------
    Fwd fw0;
    fwd(p, c, ids, fw0);

    // ---- residual chain: every layer transforms the stream --------------
    for (int l = 1; l < c.layers; ++l) {
        const std::vector<float>& a = fw0.layers[(size_t)l - 1].x_in;
        const std::vector<float>& b = fw0.layers[(size_t)l].x_in;
        if (a.size() != b.size() ||
            std::memcmp(a.data(), b.data(), a.size() * sizeof(float)) == 0)
            fail("residual: degenerate layer");
    }

    // ---- live: perturbing each layer's norm1 moves the logits -----------
    for (int l = 0; l < c.layers; ++l) {
        Params p2 = p;
        float* w = p2.w.at(ln(l, "norm1")).d.data();
        for (int i = 0; i < c.hidden; ++i) w[i] += 0.05f;
        Fwd fw2;
        fwd(p2, c, ids, fw2);
        if (std::memcmp(fw0.logits.data(), fw2.logits.data(),
                        fw0.logits.size() * sizeof(float)) == 0)
            fail("live: vacuous layer");
    }

    // ---- reach: grads finite everywhere, nonzero at the deepest layer ---
    std::vector<int> lab = ids;
    shift_labels(lab);
    {
        p.zero_grad();
        Fwd f;
        fwd(p, c, ids, f);
        std::vector<float> dl;
        ce_loss(f.logits, lab, T, c.vocab, dl);
        bwd(p, c, ids, f, dl, 1.0f);
        for (auto& n : p.order)
            for (float x : p.g[n].d)
                if (!std::isfinite(x)) fail("reach: non-finite grad");
        auto nonzero = [&](const std::string& n) {
            if (!p.g.count(n)) { fail("reach: missing grad buffer"); return; }
            bool any = false;
            for (float x : p.g[n].d) if (x != 0.0f) { any = true; break; }
            if (!any) fail("reach: zero grad at layer-0 weight");
        };
        nonzero(ln(0, "norm1"));
        nonzero(c.is_linear(0) ? ln(0, "lin.in_proj_qkv") : ln(0, "wq"));
        nonzero(ln(0, "gate"));          // MoE router on layer 0
        // every MoE layer routes to at least one expert with signal
        for (int l = 0; l < c.layers; ++l) {
            if (l % c.moe_layer_interval != 0) continue;
            bool any = false;
            for (int e = 0; e < c.moe_experts && !any; ++e)
                for (float x : p.g.at(ln(l, "experts.")
                                    + std::to_string(e) + ".w1").d)
                    if (x != 0.0f) { any = true; break; }
            if (!any) fail("reach: no expert grad on moe layer");
        }
    }

    // ---- learnable: AdamW steps reduce the loss at depth ----------------
    {
        float first = 0.0f, last = 0.0f;
        for (int s = 0; s < 4; ++s) {
            p.zero_grad();
            Fwd f;
            fwd(p, c, ids, f);
            std::vector<float> dl;
            float l = ce_loss(f.logits, lab, T, c.vocab, dl) + f.moe_aux;
            if (s == 0) first = l;
            last = l;
            bwd(p, c, ids, f, dl, 1.0f);
            adamw_step(p, 1.0f, 0.03f, 0.0f, s);
        }
        if (!(std::isfinite(last) && last < first))
            fail("learnable: loss did not decrease");
    }

    bool ok = failures == 0;
    std::printf("depthcheck: layers=%d lin=%d attn=%d failures=%d -> %s\n",
                c.layers, nlin, nattn, failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

// xct_csa.h — DeepSeek-V4.1-Flash CSA2 (compressed sparse attention)
// executable contract probe. Included once by xingcheng_trainer.cpp inside
// namespace xct, after xct_backward.h.
//
// CSA contract under test (global attention layers):
//   raw coverage = sliding window; the far past is reached through top-K
//   compressed r-to-1 latents chosen by a shared indexer per query;
//   csa_share_group>1 forms Full/Reindex/Reuse groups that share the
//   compressed stream (and, in Reuse, the top-K indices) across layers.
// Probes:
//   1. sparsity: raw attention probs are bitwise 0 beyond the window —
//      distant context flows only through compressed latents;
//   2. causality seal: perturbing token j leaves logits[0..j) bitwise
//      identical — compressed chunks only aggregate complete past spans;
//   3. far-past reach: perturbing a token inside a selected compressed
//      chunk beyond the window moves later logits (non-vacuous path);
//   4. selection bounds: nsel[t] <= topk and every id is a candidate;
//   5. CSA2 sharing: followers own no compressor (wck absent), Reuse
//      copies the head's selection verbatim, Reindex carries its own wiq;
//   6. indexer training: one bwd leaves nonzero wiq/wik grads and a
//      finite csa_idx CE (alignment of index logits to attention mass).
#pragma once

static int csacheck() {
    int failures = 0;
    auto fail = [&](const char* what, int a, int b) {
        ++failures;
        std::printf("  FAIL %s (a=%d b=%d)\n", what, a, b);
    };

    // ---- geometry: 4 global CSA layers, r=2, K=2, window=4 ----
    auto make_cfg = [&](int group, bool reindex, bool indexer) {
        ModelConfig c;
        c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 4;
        c.heads = 2; c.kv_heads = 1; c.max_pos = 64;
        c.full_attention_interval = 0;   // all layers full attention
        c.global_attn_interval = 1;      // all of them global
        c.sliding_window = 4;
        c.attn_output_gate = true;
        c.qk_norm = true;
        c.partial_rotary = 0.5f;
        c.csa_ratio = 2; c.csa_topk = 2; c.csa_window = 4;
        c.csa_rope_theta = 40000.0f;
        c.csa_group = group;
        c.csa_reindex = reindex;
        c.csa_indexer = indexer;
        return c;
    };
    const std::vector<int> ids = {3, 5, 7, 11, 13, 17, 19, 23,
                                  29, 31, 37, 41, 43, 47, 53, 59};
    const int T = (int)ids.size();

    // ---- 1+2+3+4: sparsity, causality, reach, bounds (recency indexer
    // makes the selected set deterministic for the reach probe) ----
    {
        ModelConfig c = make_cfg(0, false, false);
        Params p;
        init_params(p, c, 17);
        Fwd f0;
        fwd(p, c, ids, f0);
        for (int l = 0; l < c.layers; ++l) {
            if (!c.use_csa(l)) { fail("csa not enabled", l, 0); continue; }
            const LayerCache& L = f0.layers[l];
            const int nc = T / c.csa_ratio;
            if (L.csa_nc != nc) fail("nc", l, L.csa_nc);
            for (int t = 0; t < T; ++t) {
                const int s0 = std::max(0, t - c.csa_window + 1);
                for (int s = 0; s < s0; ++s)
                    for (int h = 0; h < c.heads; ++h)
                        if (L.probs[((size_t)h * T + t) * T + s] != 0.0f)
                            fail("sparse window", l, t * 100 + s);
                const int nsel = L.csa_nsel[(size_t)t];
                if (nsel > c.csa_topk) fail("topk bound", l, t);
                for (int j = 0; j < nsel; ++j) {
                    const int cc = L.csa_sel[(size_t)t * c.csa_topk + j];
                    if (cc < 0 || cc >= nc ||
                        !(cc * c.csa_ratio < s0 &&
                          (cc + 1) * c.csa_ratio <= t + 1))
                        fail("candidate", l, t * 10 + cc);
                }
            }
        }
        // causality seal: perturb ids[j], logits[0..j) must be bitwise
        for (int j : {T - 1, T / 2, 3}) {
            std::vector<int> ids2 = ids;
            ids2[j] = (ids2[j] + 13) % c.vocab;
            Fwd f1;
            fwd(p, c, ids2, f1);
            if (std::memcmp(f0.logits.data(), f1.logits.data(),
                            (size_t)j * c.vocab * sizeof(float)) != 0)
                fail("causal seal", j, 0);
            if (std::memcmp(f0.logits.data() + (size_t)j * c.vocab,
                            f1.logits.data() + (size_t)j * c.vocab,
                            ((size_t)T - j) * c.vocab * sizeof(float)) == 0)
                fail("causal vacuous", j, 0);
        }
        // far-past reach: the last query's selected compressed chunks
        // tile spans beyond its raw window — perturb a token inside the
        // earliest selected chunk and logits[T-1] must move through the
        // compressed path alone.
        {
            const LayerCache& LL = f0.layers[c.layers - 1];
            const int nsel = LL.csa_nsel[(size_t)T - 1];
            int minc = -1;
            for (int j = 0; j < nsel; ++j) {
                const int cc = LL.csa_sel[(size_t)(T - 1) * c.csa_topk + j];
                if (minc < 0 || cc < minc) minc = cc;
            }
            // first token of chunk (`far`/`near` are windows.h legacy
            // macros — never name a variable after them)
            const int far_tok = minc * c.csa_ratio;
            if (far_tok < 0 || far_tok >= T - c.csa_window)
                fail("reach geometry", minc, far_tok);
            else {
                std::vector<int> ids2 = ids;
                ids2[far_tok] = (ids2[far_tok] + 7) % c.vocab;
                Fwd f1;
                fwd(p, c, ids2, f1);
                if (std::memcmp(f0.logits.data() + (size_t)(T - 1) * c.vocab,
                                f1.logits.data() + (size_t)(T - 1) * c.vocab,
                                (size_t)c.vocab * sizeof(float)) == 0)
                    fail("compressed reach", far_tok, T - 1);
            }
        }
    }

    // ---- 5: CSA2 cross-layer sharing ----
    {
        ModelConfig c = make_cfg(2, false, true);   // Reuse followers
        Params p;
        init_params(p, c, 19);
        // CSA layers = 0..3 (all global); group=2 -> 0,2 Full; 1,3 Reuse
        for (int l = 0; l < c.layers; ++l) {
            const int role = c.csa_role(l, nullptr);
            const bool has_ck = p.w.count(ln(l, "wck")) > 0;
            const bool has_iq = p.w.count(ln(l, "wiq")) > 0;
            if (role == 0 && (!has_ck || !has_iq))
                fail("full params", l, role);
            if (role == 1 && (has_ck || has_iq))
                fail("reuse params", l, role);
        }
        Fwd f0;
        fwd(p, c, ids, f0);
        // Reuse copies the head's top-K verbatim
        for (int l = 1; l < c.layers; l += 2) {
            const LayerCache& FL = f0.layers[l];
            const LayerCache& HL = f0.layers[l - 1];
            if (FL.csa_src != l - 1) fail("reuse src", l, FL.csa_src);
            if (FL.csa_sel != HL.csa_sel || FL.csa_nsel != HL.csa_nsel)
                fail("reuse indices", l, l - 1);
        }
        ModelConfig cr = make_cfg(2, true, true);   // Reindex followers
        Params pr;
        init_params(pr, cr, 19);
        for (int l = 0; l < cr.layers; ++l) {
            const int role = cr.csa_role(l, nullptr);
            const bool has_ck = pr.w.count(ln(l, "wck")) > 0;
            const bool has_iq = pr.w.count(ln(l, "wiq")) > 0;
            if (role == 2 && (has_ck || !has_iq))
                fail("reindex params", l, role);
        }
    }

    // ---- 6: indexer grads ----
    {
        ModelConfig c = make_cfg(0, false, true);
        Params p;
        init_params(p, c, 23);
        p.zero_grad();
        Fwd f;
        fwd(p, c, ids, f);
        if (!std::isfinite(f.csa_idx) || f.csa_idx <= 0.0f)
            fail("csa_idx finite", 0, 0);
        std::vector<int> lab = ids;
        shift_labels(lab);
        std::vector<float> dl;
        (void)ce_loss(f.logits, lab, T, c.vocab, dl);
        bwd(p, c, ids, f, dl, 1.0f);
        auto nz = [&](const char* name) {
            if (!p.w.count(name)) return false;
            const Tensor& g = p.g[name];
            for (float x : g.d) if (x != 0.0f) return true;
            return false;
        };
        if (!nz("layers.0.wiq")) fail("wiq grad", 0, 0);
        if (!nz("layers.0.wik")) fail("wik grad", 0, 0);
        if (!nz("layers.0.wck")) fail("wck grad", 0, 0);
        if (!nz("layers.0.wcv")) fail("wcv grad", 0, 0);
    }

    const bool ok = failures == 0;
    std::printf("csacheck: compressed-sparse-attention failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

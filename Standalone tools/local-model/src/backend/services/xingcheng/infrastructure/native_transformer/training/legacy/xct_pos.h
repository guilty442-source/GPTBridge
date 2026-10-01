// xct_pos.h — B94 fragment of xingcheng_trainer.cpp (positional probe).
// Included once by xingcheng_trainer.cpp inside namespace xct, after
// xct_depth.h.
//
// Positional encoding (parallel-processing contract): a Transformer drops
// the RNN's sequential input order and attends to all tokens in parallel,
// so position information must be fused into the stream explicitly — the
// role an additive embedding-side PE serves in the classic stack. The
// native lane fuses it through two channels, both probed executably:
//   RoPE   — q/k rotary tables rotate each channel pair by t·theta^{-2j/d};
//            checked at the exact-angle level (unit basis + inverse
//            roundtrip) and at the contract level (attention score is
//            shift-invariant: R_t q · R_s k depends only on t−s, verified
//            for both the interleaved-pairs and HF rotate_half layouts)
//   order  — implicit position: the deltanet scan accumulates state
//            strictly forward and the causal conv reads only x[t-j]
//   e2e    — a repeated-token sequence must yield position-diverse logits
//            in an all-attention stack (RoPE is the only position source)
//            and in an all-linear stack (scan order only); permuting the
//            token order must move the logits (the net is not a bag of
//            words)
#pragma once

static int poscheck() {
    int failures = 0;
    auto fail = [&](const char* what) {
        ++failures;
        std::printf("  FAIL %s\n", what);
    };
    const float TOL = 2e-4f;

    // ---- RoPE unit rotation: pair j turns by t·theta^{-2j/hd} ----------
    {
        const int hd = 8, T = 4;
        std::vector<float> v((size_t)T * hd, 0.0f);
        v[(size_t)3 * hd + 0] = 1.0f;      // pair 0: angle t·1 = 3
        v[(size_t)3 * hd + 2] = 1.0f;      // pair 1: angle 3·theta^{-2/8}
        rope(v.data(), T, 1, hd, 10000.0f, false);
        const float a0 = 3.0f;
        const float a1 = 3.0f * std::pow(10000.0f, -2.0f / 8.0f);
        if (std::fabs(v[3 * hd + 0] - std::cos(a0)) > TOL ||
            std::fabs(v[3 * hd + 1] - std::sin(a0)) > TOL ||
            std::fabs(v[3 * hd + 2] - std::cos(a1)) > TOL ||
            std::fabs(v[3 * hd + 3] - std::sin(a1)) > TOL)
            fail("rope: pair angle");
        rope(v.data(), T, 1, hd, 10000.0f, true);
        if (std::fabs(v[3 * hd + 0] - 1.0f) > TOL ||
            std::fabs(v[3 * hd + 1]) > TOL ||
            std::fabs(v[3 * hd + 2] - 1.0f) > TOL ||
            std::fabs(v[3 * hd + 3]) > TOL)
            fail("rope: inverse roundtrip");
    }

    // ---- RoPE contract: score depends only on the relative offset ------
    auto rel_score = [&](bool hf, int rd, int t, int s,
                         const std::vector<float>& q,
                         const std::vector<float>& k, int hd, float theta) {
        const int T = std::max(t, s) + 1;
        std::vector<float> buf((size_t)T * hd, 0.0f);
        std::copy(q.begin(), q.end(), buf.begin() + (size_t)t * hd);
        std::copy(k.begin(), k.end(), buf.begin() + (size_t)s * hd);
        if (hf) rope_hf_partial(buf.data(), T, 1, hd, rd, theta, false);
        else    rope(buf.data(), T, 1, hd, theta, false);
        const float* qr = buf.data() + (size_t)t * hd;
        const float* kr = buf.data() + (size_t)s * hd;
        float d = 0.0f;
        for (int i = 0; i < hd; ++i) d += qr[i] * kr[i];
        return d;
    };
    {
        const int hd = 8;
        std::vector<float> q((size_t)hd), k((size_t)hd);
        std::mt19937 rng(31);
        std::uniform_real_distribution<float> ud(-1.0f, 1.0f);
        for (auto& x : q) x = ud(rng);
        for (auto& x : k) x = ud(rng);
        for (int t : {1, 5, 9})
            for (int s : {0, 3}) {
                if (std::fabs(rel_score(false, 0, t, s, q, k, hd, 10000.0f) -
                              rel_score(false, 0, t + 7, s + 7, q, k, hd,
                                        10000.0f)) > TOL)
                    fail("rope: relative shift (interleaved)");
                if (std::fabs(rel_score(true, 6, t, s, q, k, hd, 10000.0f) -
                              rel_score(true, 6, t + 7, s + 7, q, k, hd,
                                        10000.0f)) > TOL)
                    fail("rope: relative shift (rotate_half)");
            }
    }

    // ---- e2e: position signal reaches the logits ------------------------
    auto repeat_probe = [&](int interval) {
        ModelConfig c;
        c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 2;
        c.heads = 2; c.kv_heads = 1; c.max_pos = 64;
        c.full_attention_interval = interval;
        c.attn_output_gate = true;
        c.qk_norm = true;
        c.partial_rotary = 0.5f;
        c.lin_key_heads = 1; c.lin_key_dim = 32;
        c.lin_value_heads = 2; c.lin_value_dim = 32;
        c.lin_conv_kernel = 4;
        Params p;
        init_params(p, c, 41);
        std::vector<int> ids(12, 7);
        Fwd fw;
        fwd(p, c, ids, fw);
        for (int t = 1; t < (int)ids.size(); ++t)
            if (std::memcmp(fw.logits.data(),
                            fw.logits.data() + (size_t)t * c.vocab,
                            (size_t)c.vocab * sizeof(float)) != 0)
                return true;
        return false;
    };
    if (!repeat_probe(0))
        fail("pos: RoPE carries no position signal (all-attention)");
    if (!repeat_probe(100))
        fail("pos: scan carries no position signal (all-linear)");

    // ---- e2e: token order matters ----------------------------------------
    {
        ModelConfig c;
        c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 2;
        c.heads = 2; c.kv_heads = 1; c.max_pos = 64;
        c.attn_output_gate = true;
        c.qk_norm = true;
        Params p;
        init_params(p, c, 41);
        std::vector<int> a = {3, 5, 7, 11, 13, 17, 19, 23},
                        b = {23, 19, 17, 13, 11, 7, 5, 3};
        Fwd fa, fb;
        fwd(p, c, a, fa);
        fwd(p, c, b, fb);
        if (std::memcmp(fa.logits.data(), fb.logits.data(),
                        fa.logits.size() * sizeof(float)) == 0)
            fail("pos: permutation-invariant logits");
    }

    bool ok = failures == 0;
    std::printf("poscheck: rope+scan positional fusion failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

// xct_yarn.h — B94 fragment of xingcheng_trainer.cpp (YaRN probe).
// Included once by xingcheng_trainer.cpp inside namespace xct, after
// xct_route.h.
//
// --yarncheck: the Qwen3-Coder-480B YaRN context-extension contract.
// The fusion keeps the existing rope pairing (partial rotate-half /
// interleaved full, per-layer theta) and adds per-channel frequency
// blending plus an attention-factor mscale:
//   table    — channels below the fast boundary keep raw inv-freqs;
//              channels past the slow boundary divide by the factor;
//              all rotated entries gain the mscale
//   off      — use_yarn()==false produces the raw pow() table bitwise
//   extend   — forward stays finite with positions beyond orig_pos
//   causal   — the extension changes no earlier-position logits
//   ckpt     — all five yarn fields survive the XCN8 header round-trip
#pragma once

static int yarncheck() {
    int failures = 0;
    auto fail = [&](const char* what) {
        ++failures;
        std::printf("  FAIL %s\n", what);
    };

    ModelConfig c;
    c.vocab = 64; c.hidden = 32; c.inter = 48; c.layers = 2;
    c.heads = 2; c.kv_heads = 1; c.max_pos = 64;
    c.attn_output_gate = true;
    c.qk_norm = true;
    c.partial_rotary = 0.5f;          // rd = 8 -> table dim 8 (half=4)
    c.yarn_factor = 4.0f;             // extended context x4
    c.yarn_orig_pos = 16;             // original context length
    c.yarn_beta_fast = 32.0f;         // Qwen3-Coder defaults
    c.yarn_beta_slow = 1.0f;
    if (!c.use_yarn()) fail("config: use_yarn");

    // ---- table: independently re-derived blended frequencies --------
    const int rd = c.rotary_dim();    // 8
    const int half = rd / 2;
    const int T = 24;                 // > orig_pos exercises extension
    const float ms = yarn_mscale(c);
    if (!(ms > 1.0f)) fail("table: mscale <= 1");
    const RopeCs& raw = rope_cs(T, rd, c.rope_theta, nullptr);
    const RopeCs& ext = rope_cs(T, rd, c.rope_theta, &c);
    const float logb = std::log(c.rope_theta);
    auto corr = [&](float beta) {
        return (float)rd *
               std::log((float)c.yarn_orig_pos / (beta * 6.28318530718f)) /
               (2.0f * logb);
    };
    const float lo = std::max(0.0f, std::floor(corr(c.yarn_beta_fast)));
    const float hi =
        std::min((float)(half - 1), std::ceil(corr(c.yarn_beta_slow)));
    for (int i = 0; i < half; ++i) {
        const float fraw =
            std::pow(c.rope_theta, -(float)(2 * i) / (float)rd);
        float frx = fraw;
        if (i <= (int)lo) frx = fraw;                  // raw band
        else if (i >= (int)hi) frx = fraw / c.yarn_factor;  // ext band
        for (int t = 0; t < T; ++t) {
            const float ec = std::cos(t * frx) * ms;
            const float es = std::sin(t * frx) * ms;
            if (std::fabs(ext.c[(size_t)t * half + i] - ec) > 1e-5f ||
                std::fabs(ext.s[(size_t)t * half + i] - es) > 1e-5f)
                fail("table: blended freq/mscale");
        }
    }
    // ---- off: nullptr / disabled yarn yields the raw table ----------
    for (size_t i = 0; i < raw.c.size(); ++i) {
        const float fraw =
            std::pow(c.rope_theta,
                     -(float)(2 * (i % half)) / (float)rd);
        const int t = (int)(i / half);
        if (raw.c[i] != std::cos(t * fraw) ||
            raw.s[i] != std::sin(t * fraw))
            fail("off: raw table drift");
        if (ext.c[i] == raw.c[i])
            fail("off: yarn table identical to raw");
    }

    // ---- forward: finite logits, extension changes the output -------
    Params p;
    init_params(p, c, 53);
    std::vector<int> ids(T);
    for (int t = 0; t < T; ++t) ids[t] = 3 + (t * 5) % (c.vocab - 4);
    Fwd fy;
    fwd(p, c, ids, fy);
    for (float x : fy.logits)
        if (!std::isfinite(x)) fail("extend: non-finite logit");
    ModelConfig cof = c;
    cof.yarn_factor = 0.0f;
    cof.yarn_orig_pos = 0;
    Fwd fo;
    fwd(p, cof, ids, fo);
    bool same = true;
    for (size_t i = 0; i < fy.logits.size(); ++i)
        if (fy.logits[i] != fo.logits[i]) { same = false; break; }
    if (same) fail("extend: flag inert");

    // ---- causal: positions < k see no change from later tokens ------
    std::vector<int> ids2 = ids;
    ids2[T - 1] = (ids2[T - 1] + 1) % c.vocab;
    Fwd fz;
    fwd(p, c, ids2, fz);
    for (int t = 0; t < T - 1; ++t)
        for (int v = 0; v < c.vocab; ++v)
            if (fy.logits[(size_t)t * c.vocab + v] !=
                fz.logits[(size_t)t * c.vocab + v])
                fail("causal: future leaked");

    // ---- ckpt: all five yarn fields survive the XCN8 round-trip -----
    {
        const char* tmp = "xct_yarncheck_ckpt.tmp";
        if (!ckpt_save(p, c, tmp, true)) fail("ckpt: save");
        ModelConfig c2;
        if (!ckpt_peek_config(tmp, c2)) fail("ckpt: peek");
        else {
            if (c2.yarn_factor != c.yarn_factor ||
                c2.yarn_orig_pos != c.yarn_orig_pos ||
                c2.yarn_beta_fast != c.yarn_beta_fast ||
                c2.yarn_beta_slow != c.yarn_beta_slow ||
                c2.yarn_attn_factor != c.yarn_attn_factor)
                fail("ckpt: yarn fields");
        }
        std::remove(tmp);
    }

    bool ok = failures == 0;
    std::printf("yarncheck: yarn-extension failures=%d -> %s\n",
                failures, ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}

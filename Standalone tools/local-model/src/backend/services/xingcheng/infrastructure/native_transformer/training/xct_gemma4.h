// xct_gemma4.h — B94 fragment of xingcheng_trainer.cpp (Gemma4 fwd/bwd).
// Included once by xingcheng_trainer.cpp inside namespace xct.
//
// Single-sequence training mirror of the engine's Gemma4 path
// (HF modeling_gemma4 semantics):
//   x0 = embed*sqrt(H); ple_in = mix*(ple_norm(H^-0.5*ple_model(x0)) +
//                                    sqrt(ple)*embed_ple)
//   per layer: x += attn_norm(attn(QK-norm, dual/partial rope, windowed,
//                                  shared-KV owners))
//              x += ffn_norm(glu(pre_ffn(x)))         [gelu-tanh]
//              x += ple_norm(ple_proj(gelu(ple_gate(x))*ple_in_l))
//   logits = cap*tanh(W(x_f)/cap); tied embed = no lm_head param.
// Shared layers own no wk/wv/k_norm — their attention reuses the type
// anchor's post-norm/post-rope K/V, and their dk/dv accumulate into the
// owner's gradient buffers before the owner's un-rope/un-norm backward.
#pragma once

// Gemma4 workspace: same contract as BwdWs — per-call and per-layer
// temporaries held in one thread_local struct so capacity persists
// across layers/examples; every field is re-initialized at its use site.
// fwd_g4/bwd_g4 run on the caller thread only.
struct G4Ws {
    std::vector<float> x, ctx, ctxn, normed;                    // fwd
    std::vector<float> dz, dh, dx, dple_in, buf, dp, dga, dg,   // bwd
                       dxb, dproj, dfh, dfa, dfb, dn2, dao, dq,
                       dn1, dqp, dkp, dvp, dx0, dctxn, dctx;
    std::vector<std::vector<float>> dk_acc, dv_acc;
    std::vector<float> ones_buf;
};

// ------------------------------------------------------- gemma4 forward --

static void fwd_g4(const Params& p, const ModelConfig& c,
                   const std::vector<int>& ids, Fwd& o) {
    const int T = (int)ids.size();
    const int H = c.hidden;
    const int ple = c.ple_hidden;
    const float emb_scale = std::sqrt((float)H);
    static thread_local G4Ws ws;
    auto& x = ws.x;
    x.assign((size_t)T * H, 0.0f);
    const float* er0 = p.w.at("embed").d.data();
    parallel_for(T, [&](int64_t b, int64_t e) {
        for (int64_t t = b; t < e; ++t)
            tpu_scale_copy(x.data() + (size_t)t * H,
                           er0 + (size_t)ids[(size_t)t] * H, emb_scale, H);
    });
    o.g4_x0 = x;

    // PLE inputs: identity (embed_ple*sqrt(ple)) + context branch.
    if (ple > 0) {
        const int L = c.layers;
        o.g4_ple_in.assign((size_t)T * L * ple, 0.0f);
        const float tok_scale = std::sqrt((float)ple);
        const float* epl = p.w.at("embed_ple").d.data();
        parallel_for(T, [&](int64_t b, int64_t e) {
            for (int64_t t = b; t < e; ++t)
                tpu_scale_copy(
                    o.g4_ple_in.data() + (size_t)t * L * ple,
                    epl + (size_t)ids[(size_t)t] * L * ple,
                    tok_scale, (int64_t)L * ple);
        });
        auto& ctx = ws.ctx;
        ctx.assign((size_t)T * L * ple, 0.0f);
        linear_fwd(x.data(), p.w.at("ple_model_proj"), ctx.data(),
                   T, H, L * ple);
        const float ctx_scale = 1.0f / std::sqrt((float)H);
        tpu_scale(ctx.data(), ctx_scale, (int64_t)ctx.size());
        o.g4_ctx_in = ctx;                       // post-scale, pre-norm
        o.g4_ctx_rms.resize((size_t)T * L);
        auto& ctxn = ws.ctxn;
        ctxn.assign(ctx.size(), 0.0f);
        rmsnorm_fwd(ctx.data(), p.w.at("ple_proj_norm").d.data(),
                    ctxn.data(), o.g4_ctx_rms.data(), T * L, ple,
                    c.rms_eps);
        const float mix = std::pow(2.0f, -0.5f);
        tpu_elementwise((int64_t)o.g4_ple_in.size(), [&](int64_t i) {
            o.g4_ple_in[(size_t)i] =
                (ctxn[(size_t)i] + o.g4_ple_in[(size_t)i]) * mix;
        });
    }

    o.g4l.resize(c.layers);
    auto& normed = ws.normed;
    for (int l = 0; l < c.layers; ++l) {
        G4Layer& L = o.g4l[l];
        const int hd = c.hd_at(l);
        const int Hq = c.heads * hd, Hkv = c.kv_heads * hd;
        const bool sliding = c.sliding_at(l);
        const bool shared = c.kv_shared(l);
        const int owner = c.kv_owner(l);
        const float theta = c.theta_at(l);
        const int rotary = c.rotary_at(l);
        const float scale = c.attention_scale > 0.0f
            ? c.attention_scale : 1.0f / std::sqrt((float)hd);
        const int win = sliding ? c.sliding_window : INT32_MAX;

        L.x_in = x;
        L.n1.resize((size_t)T * H); L.rms1.resize(T);
        rmsnorm_fwd(x.data(), p.w.at(ln(l, "norm1")).d.data(),
                    L.n1.data(), L.rms1.data(), T, H, c.rms_eps);

        L.q_proj.resize((size_t)T * Hq);
        linear_fwd(L.n1.data(), p.w.at(ln(l, "wq")), L.q_proj.data(),
                   T, H, Hq);
        L.rms_q.resize((size_t)T * c.heads);
        L.q.resize((size_t)T * Hq);
        rmsnorm_fwd(L.q_proj.data(), p.w.at(ln(l, "q_norm")).d.data(),
                    L.q.data(), L.rms_q.data(), T * c.heads, hd,
                    c.rms_eps);
        rope_ex(L.q.data(), T, c.heads, hd, theta, rotary, false);

        if (!shared) {
            L.k_proj.resize((size_t)T * Hkv);
            L.v_proj.resize((size_t)T * Hkv);
            linear_fwd(L.n1.data(), p.w.at(ln(l, "wk")),
                       L.k_proj.data(), T, H, Hkv);
            linear_fwd(L.n1.data(), p.w.at(ln(l, "wv")),
                       L.v_proj.data(), T, H, Hkv);
            L.rms_k.resize((size_t)T * c.kv_heads);
            L.k.resize((size_t)T * Hkv);
            rmsnorm_fwd(L.k_proj.data(), p.w.at(ln(l, "k_norm")).d.data(),
                        L.k.data(), L.rms_k.data(), T * c.kv_heads, hd,
                        c.rms_eps);
            rope_ex(L.k.data(), T, c.kv_heads, hd, theta, rotary, false);
            // v_norm: scale-free RMSNorm (with_scale=False).
            auto& ones_buf = ws.ones_buf;
            if ((int)ones_buf.size() < hd) ones_buf.assign(hd, 1.0f);
            L.rms_v.resize((size_t)T * c.kv_heads);
            L.v.resize((size_t)T * Hkv);
            rmsnorm_fwd(L.v_proj.data(), ones_buf.data(), L.v.data(),
                        L.rms_v.data(), T * c.kv_heads, hd, c.rms_eps);
        }
        const G4Layer& KV = o.g4l[owner];

        const int group = c.heads / c.kv_heads;
        L.probs.assign((size_t)c.heads * T * T, 0.0f);
        L.attn_out.assign((size_t)T * Hq, 0.0f);
        // TPU lanes: heads are disjoint lanes (probs per-h slice, attn_out
        // per-h column slice); 4-row blocks share each streamed k/v row —
        // per-element accumulation order is unchanged.
        parallel_for(c.heads, (int64_t)T * T * hd,
                     [&](int64_t hb, int64_t he) {
            for (int64_t h = hb; h < he; ++h) {
                const int kh = (int)h / group;
                tpu_attn_fwd(
                    L.probs.data() + (size_t)h * T * T,
                    L.attn_out.data() + (size_t)h * hd,
                    (int64_t)c.heads * hd, T, hd,
                    sliding ? win : 0, scale,
                    [&](int t) {
                        return L.q.data() +
                               ((size_t)t * c.heads + h) * hd;
                    },
                    [&](int s) {
                        return KV.k.data() +
                               ((size_t)s * c.kv_heads + kh) * hd;
                    },
                    [&](int s) {
                        return KV.v.data() +
                               ((size_t)s * c.kv_heads + kh) * hd;
                    });
            }
        });
        L.o_pre.resize((size_t)T * H);
        linear_fwd(L.attn_out.data(), p.w.at(ln(l, "wo")),
                   L.o_pre.data(), T, Hq, H);
        L.rms_attn.resize(T);
        normed.assign((size_t)T * H, 0.0f);
        rmsnorm_fwd(L.o_pre.data(), p.w.at(ln(l, "norm_attn")).d.data(),
                    normed.data(), L.rms_attn.data(), T, H, c.rms_eps);
        L.x_mid.resize((size_t)T * H);
        tpu_elementwise((int64_t)T * H, [&](int64_t i) {
            L.x_mid[(size_t)i] = L.x_in[(size_t)i] + normed[(size_t)i];
        });

        L.n2.resize((size_t)T * H); L.rms2.resize(T);
        rmsnorm_fwd(L.x_mid.data(), p.w.at(ln(l, "norm2")).d.data(),
                    L.n2.data(), L.rms2.data(), T, H, c.rms_eps);
        const int inter = c.inter_at(l);
        L.fa.resize((size_t)T * inter);
        L.fb.resize((size_t)T * inter);
        L.fh.resize((size_t)T * inter);
        linear_fwd(L.n2.data(), p.w.at(ln(l, "w1")), L.fa.data(),
                   T, H, inter);
        linear_fwd(L.n2.data(), p.w.at(ln(l, "w3")), L.fb.data(),
                   T, H, inter);
        tpu_elementwise((int64_t)L.fh.size(), [&](int64_t i) {
            L.fh[(size_t)i] = act_f(c, L.fa[(size_t)i]) * L.fb[(size_t)i];
        });
        L.ffn_pre.resize((size_t)T * H);
        linear_fwd(L.fh.data(), p.w.at(ln(l, "w2")), L.ffn_pre.data(),
                   T, inter, H);
        L.rms_ffn.resize(T);
        normed.assign((size_t)T * H, 0.0f);
        rmsnorm_fwd(L.ffn_pre.data(), p.w.at(ln(l, "norm_ffn")).d.data(),
                    normed.data(), L.rms_ffn.data(), T, H, c.rms_eps);
        L.x_ple.resize((size_t)T * H);
        tpu_elementwise((int64_t)T * H, [&](int64_t i) {
            L.x_ple[(size_t)i] = L.x_mid[(size_t)i] + normed[(size_t)i];
        });

        if (ple > 0) {
            L.ple_g.resize((size_t)T * ple);
            linear_fwd(L.x_ple.data(), p.w.at(ln(l, "ple_gate")),
                       L.ple_g.data(), T, H, ple);
            L.ple_ga.resize((size_t)T * ple);
            tpu_elementwise((int64_t)T * ple, [&](int64_t i) {
                L.ple_ga[(size_t)i] =
                    gelu_tanh_f(L.ple_g[(size_t)i]) *
                    o.g4_ple_in[((size_t)(i / ple) * c.layers + l) * ple +
                                (size_t)(i % ple)];
            });
            L.ple_p.resize((size_t)T * H);
            linear_fwd(L.ple_ga.data(), p.w.at(ln(l, "ple_proj")),
                       L.ple_p.data(), T, ple, H);
            L.rms_ple.resize(T);
            normed.assign((size_t)T * H, 0.0f);
            rmsnorm_fwd(L.ple_p.data(), p.w.at(ln(l, "ple_post")).d.data(),
                        normed.data(), L.rms_ple.data(), T, H, c.rms_eps);
            tpu_elementwise((int64_t)T * H, [&](int64_t i) {
                x[(size_t)i] = L.x_ple[(size_t)i] + normed[(size_t)i];
            });
        } else {
            x = L.x_ple;
        }
    }

    o.x_fin = x;
    o.hidden.resize((size_t)T * H); o.rmsf.resize(T);
    rmsnorm_fwd(x.data(), p.w.at("norm_f").d.data(),
                o.hidden.data(), o.rmsf.data(), T, H, c.rms_eps);
    const Tensor& lm = c.tie_embed ? p.w.at("embed") : p.w.at("lm_head");
    o.logits_pre.resize((size_t)T * c.vocab);
    linear_fwd(o.hidden.data(), lm, o.logits_pre.data(), T, H, c.vocab);
    o.logits = o.logits_pre;
    const float cap = c.final_logit_softcapping;
    if (cap > 0.0f)
        tpu_elementwise((int64_t)o.logits.size(), [&](int64_t i) {
            o.logits[(size_t)i] =
                cap * std::tanh(o.logits[(size_t)i] / cap);
        });
}

// ------------------------------------------------------ gemma4 backward --

static void bwd_g4(Params& p, const ModelConfig& c,
                   const std::vector<int>& ids, Fwd& o,
                   const std::vector<float>& dlogits) {
    const int T = (int)ids.size();
    const int H = c.hidden;
    const int ple = c.ple_hidden;
    const float emb_scale = std::sqrt((float)H);
    const char* lm_name = c.tie_embed ? "embed" : "lm_head";

    static thread_local G4Ws ws;
    auto& dz = ws.dz;
    dz = dlogits;
    const float cap = c.final_logit_softcapping;
    if (cap > 0.0f) {
        tpu_elementwise((int64_t)dz.size(), [&](int64_t i) {
            const float t = o.logits[(size_t)i] / cap;   // tanh(pre/cap)
            dz[(size_t)i] *= 1.0f - t * t;
        });
    }
    auto& dh = ws.dh;
    dh.assign((size_t)T * H, 0.0f);
    linear_bwd(dz.data(), o.hidden.data(), p.w.at(lm_name),
               dh.data(), p.dw(lm_name), T, H, c.vocab);
    auto& dx = ws.dx;
    dx.assign((size_t)T * H, 0.0f);
    rmsnorm_bwd(dh.data(), o.x_fin.data(), p.w.at("norm_f").d.data(),
                o.rmsf.data(), dx.data(), p.dw("norm_f"), T, H);

    // dk/dv contributions land in the owner's post-rope/post-norm
    // buffers; shared layers add theirs before the owner un-does its
    // norm/rope once — one gradient site per anchor, like HF.
    // clear() keeps each owner's capacity across examples; the first
    // touch per call re-zeroes through assign, as before.
    auto& dk_acc = ws.dk_acc;
    auto& dv_acc = ws.dv_acc;
    dk_acc.resize((size_t)c.layers);
    dv_acc.resize((size_t)c.layers);
    for (auto& v : dk_acc) v.clear();
    for (auto& v : dv_acc) v.clear();
    auto& dple_in = ws.dple_in;
    if (ple > 0) dple_in.assign((size_t)T * c.layers * ple, 0.0f);
    else dple_in.clear();

    auto& buf = ws.buf;
    buf.assign((size_t)T * H, 0.0f);
    for (int l = c.layers - 1; l >= 0; --l) {
        G4Layer& L = o.g4l[l];
        const int hd = c.hd_at(l);
        const int Hq = c.heads * hd, Hkv = c.kv_heads * hd;
        const bool sliding = c.sliding_at(l);
        const bool shared = c.kv_shared(l);
        const int owner = c.kv_owner(l);
        const float theta = c.theta_at(l);
        const int rotary = c.rotary_at(l);
        const float scale = c.attention_scale > 0.0f
            ? c.attention_scale : 1.0f / std::sqrt((float)hd);
        const int win = sliding ? c.sliding_window : INT32_MAX;
        const int inter = c.inter_at(l);
        const G4Layer& KV = o.g4l[owner];

        if (ple > 0) {
            // x = x_ple + ple_norm(ple_p); ple_p = ple_ga @ ple_proj
            auto& dp = ws.dp;
            dp.assign((size_t)T * H, 0.0f);
            rmsnorm_bwd(dx.data(), L.ple_p.data(),
                        p.w.at(ln(l, "ple_post")).d.data(),
                        L.rms_ple.data(), dp.data(),
                        p.dw(ln(l, "ple_post")), T, H);
            auto& dga = ws.dga;
            dga.assign((size_t)T * ple, 0.0f);
            linear_bwd(dp.data(), L.ple_ga.data(),
                       p.w.at(ln(l, "ple_proj")), dga.data(),
                       p.dw(ln(l, "ple_proj")), T, ple, H);
            auto& dg = ws.dg;
            dg.assign((size_t)T * ple, 0.0f);
            tpu_elementwise((int64_t)T * ple, [&](int64_t i) {
                const size_t pl_i =
                    ((size_t)(i / ple) * c.layers + l) * ple +
                    (size_t)(i % ple);
                const float g = L.ple_g[(size_t)i];
                const float du = dga[(size_t)i];
                dg[(size_t)i] = du * o.g4_ple_in[pl_i] * gelu_tanh_d(g);
                dple_in[pl_i] += du * gelu_tanh_f(g);
            });
            auto& dxb = ws.dxb;
            dxb.assign((size_t)T * H, 0.0f);
            linear_bwd(dg.data(), L.x_ple.data(),
                       p.w.at(ln(l, "ple_gate")), dxb.data(),
                       p.dw(ln(l, "ple_gate")), T, H, ple);
            tpu_elementwise((int64_t)dx.size(), [&](int64_t i) {
                dx[(size_t)i] += dxb[(size_t)i];
            });
        }
        {
            // x_ple = x_mid + ffn_norm(ffn_pre)
            auto& dproj = ws.dproj;
            dproj.assign((size_t)T * H, 0.0f);
            rmsnorm_bwd(dx.data(), L.ffn_pre.data(),
                        p.w.at(ln(l, "norm_ffn")).d.data(),
                        L.rms_ffn.data(), dproj.data(),
                        p.dw(ln(l, "norm_ffn")), T, H);
            auto& dfh = ws.dfh;
            dfh.assign((size_t)T * inter, 0.0f);
            linear_bwd(dproj.data(), L.fh.data(), p.w.at(ln(l, "w2")),
                       dfh.data(), p.dw(ln(l, "w2")),
                       T, inter, H);
            auto& dfa = ws.dfa;
            auto& dfb = ws.dfb;
            dfa.assign((size_t)T * inter, 0.0f);
            dfb.assign((size_t)T * inter, 0.0f);
            tpu_elementwise((int64_t)L.fh.size(), [&](int64_t i) {
                const float a = L.fa[(size_t)i], b = L.fb[(size_t)i],
                            d = dfh[(size_t)i];
                dfa[(size_t)i] += d * b * act_d(c, a);
                dfb[(size_t)i] += d * act_f(c, a);
            });
            auto& dn2 = ws.dn2;
            dn2.assign((size_t)T * H, 0.0f);
            linear_bwd(dfa.data(), L.n2.data(), p.w.at(ln(l, "w1")),
                       dn2.data(), p.dw(ln(l, "w1")),
                       T, H, inter);
            linear_bwd(dfb.data(), L.n2.data(), p.w.at(ln(l, "w3")),
                       dn2.data(), p.dw(ln(l, "w3")),
                       T, H, inter);
            buf.assign((size_t)T * H, 0.0f);
            rmsnorm_bwd(dn2.data(), L.x_mid.data(),
                        p.w.at(ln(l, "norm2")).d.data(),
                        L.rms2.data(), buf.data(),
                        p.dw(ln(l, "norm2")), T, H);
            tpu_elementwise((int64_t)dx.size(), [&](int64_t i) {
                dx[(size_t)i] += buf[(size_t)i];
            });
        }
        {
            // x_mid = x_in + attn_norm(o_pre); o_pre = attn_out @ wo
            auto& dproj = ws.dproj;
            dproj.assign((size_t)T * H, 0.0f);
            rmsnorm_bwd(dx.data(), L.o_pre.data(),
                        p.w.at(ln(l, "norm_attn")).d.data(),
                        L.rms_attn.data(), dproj.data(),
                        p.dw(ln(l, "norm_attn")), T, H);
            auto& dao = ws.dao;
            dao.assign((size_t)T * Hq, 0.0f);
            linear_bwd(dproj.data(), L.attn_out.data(),
                       p.w.at(ln(l, "wo")), dao.data(),
                       p.dw(ln(l, "wo")), T, Hq, H);
            auto& dq = ws.dq;
            dq.assign((size_t)T * Hq, 0.0f);
            if (dk_acc[owner].empty()) {
                dk_acc[owner].assign((size_t)T * Hkv, 0.0f);
                dv_acc[owner].assign((size_t)T * Hkv, 0.0f);
            }
            std::vector<float>& dk = dk_acc[owner];
            std::vector<float>& dv = dv_acc[owner];
            const int group = c.heads / c.kv_heads;
            // TPU lanes: one lane per kv-head — the q-heads of a GQA group
            // share its dk/dv slices, so grouping heads by owner keeps
            // every shared-buffer write disjoint across lanes; per-element
            // accumulation order is unchanged.
            parallel_for(c.kv_heads, (int64_t)T * T * hd * group,
                         [&](int64_t gb, int64_t ge) {
                std::vector<float> dsc((size_t)4 * T);
                for (int64_t g = gb; g < ge; ++g)
                    for (int h = (int)g * group;
                         h < std::min((int)(g + 1) * group, c.heads); ++h) {
                        const int kh = h / group;
                        tpu_attn_bwd(
                            L.probs.data() + (size_t)h * T * T,
                            T, hd, sliding ? win : 0, scale,
                            [&](int t) {
                                return dao.data() +
                                       ((size_t)t * c.heads + h) * hd;
                            },
                            [&](int t) {
                                return L.q.data() +
                                       ((size_t)t * c.heads + h) * hd;
                            },
                            [&](int s) {
                                return KV.k.data() +
                                       ((size_t)s * c.kv_heads + kh) * hd;
                            },
                            [&](int s) {
                                return KV.v.data() +
                                       ((size_t)s * c.kv_heads + kh) * hd;
                            },
                            [&](int t) {
                                return dq.data() +
                                       ((size_t)t * c.heads + h) * hd;
                            },
                            [&](int s) {
                                return dk.data() +
                                       ((size_t)s * c.kv_heads + kh) * hd;
                            },
                            [&](int s) {
                                return dv.data() +
                                       ((size_t)s * c.kv_heads + kh) * hd;
                            },
                            dsc.data());
                    }
            });
            // un-rope + un-norm q (always own); k/v only at the owner.
            rope_ex(dq.data(), T, c.heads, hd, theta, rotary, true);
            auto& dn1 = ws.dn1;
            dn1.assign((size_t)T * H, 0.0f);
            auto& dqp = ws.dqp;
            dqp.assign((size_t)T * Hq, 0.0f);
            rmsnorm_bwd(dq.data(), L.q_proj.data(),
                        p.w.at(ln(l, "q_norm")).d.data(),
                        L.rms_q.data(), dqp.data(),
                        p.dw(ln(l, "q_norm")), T * c.heads, hd);
            linear_bwd(dqp.data(), L.n1.data(), p.w.at(ln(l, "wq")),
                       dn1.data(), p.dw(ln(l, "wq")), T, H, Hq);
            if (!shared) {
                auto& dkp = ws.dkp;
                dkp.assign((size_t)T * Hkv, 0.0f);
                rope_ex(dk_acc[l].data(), T, c.kv_heads, hd,
                        theta, rotary, true);
                rmsnorm_bwd(dk_acc[l].data(), L.k_proj.data(),
                            p.w.at(ln(l, "k_norm")).d.data(),
                            L.rms_k.data(), dkp.data(),
                            p.dw(ln(l, "k_norm")),
                            T * c.kv_heads, hd);
                linear_bwd(dkp.data(), L.n1.data(),
                           p.w.at(ln(l, "wk")), dn1.data(),
                           p.dw(ln(l, "wk")), T, H, Hkv);
                auto& ones_buf = ws.ones_buf;
                if ((int)ones_buf.size() < hd) ones_buf.assign(hd, 1.0f);
                auto& dvp = ws.dvp;
                dvp.assign((size_t)T * Hkv, 0.0f);
                rmsnorm_bwd(dv_acc[l].data(), L.v_proj.data(),
                            ones_buf.data(), L.rms_v.data(), dvp.data(),
                            nullptr, T * c.kv_heads, hd);
                linear_bwd(dvp.data(), L.n1.data(),
                           p.w.at(ln(l, "wv")), dn1.data(),
                           p.dw(ln(l, "wv")), T, H, Hkv);
            }
            buf.assign((size_t)T * H, 0.0f);
            rmsnorm_bwd(dn1.data(), L.x_in.data(),
                        p.w.at(ln(l, "norm1")).d.data(),
                        L.rms1.data(), buf.data(),
                        p.dw(ln(l, "norm1")), T, H);
            tpu_elementwise((int64_t)dx.size(), [&](int64_t i) {
                dx[(size_t)i] += buf[(size_t)i];
            });
        }
    }

    // PLE model-level backward: dple_in -> (ctx-norm chain, embed_ple).
    auto& dx0 = ws.dx0;
    dx0 = dx;                                      // grad wrt x0
    if (ple > 0) {
        const int L = c.layers;
        const float mix = std::pow(2.0f, -0.5f);
        const float tok_scale = std::sqrt((float)ple);
        const float ctx_scale = 1.0f / std::sqrt((float)H);
        auto& dctxn = ws.dctxn;
        dctxn.assign(dple_in.size(), 0.0f);
        float* gepl = p.dw("embed_ple");
        // Column lanes over L*ple: each worker owns a disjoint channel
        // range across all rows, so repeated-token embed_ple accumulations
        // stay in t-ascending order per element — bitwise identical.
        parallel_for((int64_t)L * ple, [&](int64_t b, int64_t e) {
            for (int64_t i = b; i < e; ++i)
                for (int64_t t = 0; t < T; ++t) {
                    const float dv = dple_in[(size_t)t * L * ple + i];
                    dctxn[(size_t)t * L * ple + i] = dv * mix;
                    if (gepl)
                        gepl[(size_t)ids[(size_t)t] * L * ple + i] +=
                            dv * mix * tok_scale;
                }
        });
        auto& dctx = ws.dctx;
        dctx.assign(dctxn.size(), 0.0f);
        rmsnorm_bwd(dctxn.data(), o.g4_ctx_in.data(),
                    p.w.at("ple_proj_norm").d.data(),
                    o.g4_ctx_rms.data(), dctx.data(),
                    p.dw("ple_proj_norm"), T * L, ple);
        tpu_scale(dctx.data(), ctx_scale, (int64_t)dctx.size());
        linear_bwd(dctx.data(), o.g4_x0.data(),
                   p.w.at("ple_model_proj"), dx0.data(),
                   p.dw("ple_model_proj"), T, H, L * ple);
    }
    // embed backward (x0 = embed*emb_scale); tied lm_head already routed.
    // Column lanes over H keep repeated-token accumulation t-ascending
    // per element — bitwise identical to the serial row order.
    float* gemb = p.dw("embed");
    if (gemb)
        parallel_for(H, (int64_t)T, [&](int64_t b, int64_t e) {
            for (int64_t i = b; i < e; ++i)
                for (int64_t t = 0; t < T; ++t)
                    gemb[(size_t)ids[(size_t)t] * H + i] +=
                        dx0[(size_t)t * H + i] * emb_scale;
        });
}

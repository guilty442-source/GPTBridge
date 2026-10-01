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

// ------------------------------------------------------- gemma4 forward --

static void fwd_g4(const Params& p, const ModelConfig& c,
                   const std::vector<int>& ids, Fwd& o) {
    const int T = (int)ids.size();
    const int H = c.hidden;
    const int ple = c.ple_hidden;
    const float emb_scale = std::sqrt((float)H);
    std::vector<float> x((size_t)T * H);
    const float* er0 = p.w.at("embed").d.data();
    for (int t = 0; t < T; ++t) {
        const float* er = er0 + (size_t)ids[t] * H;
        float* xr = x.data() + (size_t)t * H;
        for (int i = 0; i < H; ++i) xr[i] = er[i] * emb_scale;
    }
    o.g4_x0 = x;

    // PLE inputs: identity (embed_ple*sqrt(ple)) + context branch.
    if (ple > 0) {
        const int L = c.layers;
        o.g4_ple_in.assign((size_t)T * L * ple, 0.0f);
        const float tok_scale = std::sqrt((float)ple);
        const float* epl = p.w.at("embed_ple").d.data();
        for (int t = 0; t < T; ++t) {
            const float* src = epl + (size_t)ids[t] * L * ple;
            float* dst = o.g4_ple_in.data() + (size_t)t * L * ple;
            for (int i = 0; i < L * ple; ++i) dst[i] = src[i] * tok_scale;
        }
        std::vector<float> ctx((size_t)T * L * ple);
        linear_fwd(x.data(), p.w.at("ple_model_proj"), ctx.data(),
                   T, H, L * ple);
        const float ctx_scale = 1.0f / std::sqrt((float)H);
        for (float& v : ctx) v *= ctx_scale;
        o.g4_ctx_in = ctx;                       // post-scale, pre-norm
        o.g4_ctx_rms.resize((size_t)T * L);
        std::vector<float> ctxn(ctx.size());
        rmsnorm_fwd(ctx.data(), p.w.at("ple_proj_norm").d.data(),
                    ctxn.data(), o.g4_ctx_rms.data(), T * L, ple,
                    c.rms_eps);
        const float mix = std::pow(2.0f, -0.5f);
        for (size_t i = 0; i < o.g4_ple_in.size(); ++i)
            o.g4_ple_in[i] = (ctxn[i] + o.g4_ple_in[i]) * mix;
    }

    o.g4l.resize(c.layers);
    std::vector<float> normed((size_t)T * H);
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
            static std::vector<float> ones_buf;
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
        for (int h = 0; h < c.heads; ++h) {
            const int kh = h / group;
            for (int t = 0; t < T; ++t) {
                const int lo = std::max(0, t - win + 1);
                float* pr = L.probs.data() + ((size_t)h * T + t) * T;
                float mx = -1e30f;
                const float* qr =
                    L.q.data() + ((size_t)t * c.heads + h) * hd;
                for (int s = lo; s <= t; ++s) {
                    const float* kr =
                        KV.k.data() + ((size_t)s * c.kv_heads + kh) * hd;
                    float dot = 0.0f;
                    for (int i = 0; i < hd; ++i) dot += qr[i] * kr[i];
                    pr[s] = dot * scale;
                    mx = std::max(mx, pr[s]);
                }
                float sum = 0.0f;
                for (int s = lo; s <= t; ++s) {
                    pr[s] = std::exp(pr[s] - mx); sum += pr[s];
                }
                float inv = 1.0f / sum;
                float* ao =
                    L.attn_out.data() + ((size_t)t * c.heads + h) * hd;
                for (int s = lo; s <= t; ++s) {
                    pr[s] *= inv;
                    const float* vr =
                        KV.v.data() + ((size_t)s * c.kv_heads + kh) * hd;
                    for (int i = 0; i < hd; ++i) ao[i] += pr[s] * vr[i];
                }
            }
        }
        L.o_pre.resize((size_t)T * H);
        linear_fwd(L.attn_out.data(), p.w.at(ln(l, "wo")),
                   L.o_pre.data(), T, Hq, H);
        L.rms_attn.resize(T);
        normed.assign((size_t)T * H, 0.0f);
        rmsnorm_fwd(L.o_pre.data(), p.w.at(ln(l, "norm_attn")).d.data(),
                    normed.data(), L.rms_attn.data(), T, H, c.rms_eps);
        L.x_mid.resize((size_t)T * H);
        for (size_t i = 0; i < (size_t)T * H; ++i)
            L.x_mid[i] = L.x_in[i] + normed[i];

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
        for (size_t i = 0; i < L.fh.size(); ++i)
            L.fh[i] = act_f(c, L.fa[i]) * L.fb[i];
        L.ffn_pre.resize((size_t)T * H);
        linear_fwd(L.fh.data(), p.w.at(ln(l, "w2")), L.ffn_pre.data(),
                   T, inter, H);
        L.rms_ffn.resize(T);
        normed.assign((size_t)T * H, 0.0f);
        rmsnorm_fwd(L.ffn_pre.data(), p.w.at(ln(l, "norm_ffn")).d.data(),
                    normed.data(), L.rms_ffn.data(), T, H, c.rms_eps);
        L.x_ple.resize((size_t)T * H);
        for (size_t i = 0; i < (size_t)T * H; ++i)
            L.x_ple[i] = L.x_mid[i] + normed[i];

        if (ple > 0) {
            L.ple_g.resize((size_t)T * ple);
            linear_fwd(L.x_ple.data(), p.w.at(ln(l, "ple_gate")),
                       L.ple_g.data(), T, H, ple);
            L.ple_ga.resize((size_t)T * ple);
            for (int t = 0; t < T; ++t) {
                const float* pl = o.g4_ple_in.data() +
                    ((size_t)t * c.layers + l) * ple;
                for (int d = 0; d < ple; ++d) {
                    L.ple_ga[(size_t)t * ple + d] =
                        gelu_tanh_f(L.ple_g[(size_t)t * ple + d]) * pl[d];
                }
            }
            L.ple_p.resize((size_t)T * H);
            linear_fwd(L.ple_ga.data(), p.w.at(ln(l, "ple_proj")),
                       L.ple_p.data(), T, ple, H);
            L.rms_ple.resize(T);
            normed.assign((size_t)T * H, 0.0f);
            rmsnorm_fwd(L.ple_p.data(), p.w.at(ln(l, "ple_post")).d.data(),
                        normed.data(), L.rms_ple.data(), T, H, c.rms_eps);
            for (size_t i = 0; i < (size_t)T * H; ++i)
                x[i] = L.x_ple[i] + normed[i];
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
        for (float& v : o.logits) v = cap * std::tanh(v / cap);
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

    std::vector<float> dz = dlogits;
    const float cap = c.final_logit_softcapping;
    if (cap > 0.0f) {
        for (size_t i = 0; i < dz.size(); ++i) {
            const float t = o.logits[i] / cap;   // tanh(pre/cap)
            dz[i] *= 1.0f - t * t;
        }
    }
    std::vector<float> dh((size_t)T * H, 0.0f);
    linear_bwd(dz.data(), o.hidden.data(), p.w.at(lm_name),
               dh.data(), p.dw(lm_name), T, H, c.vocab);
    std::vector<float> dx((size_t)T * H, 0.0f);
    rmsnorm_bwd(dh.data(), o.x_fin.data(), p.w.at("norm_f").d.data(),
                o.rmsf.data(), dx.data(), p.dw("norm_f"), T, H);

    // dk/dv contributions land in the owner's post-rope/post-norm
    // buffers; shared layers add theirs before the owner un-does its
    // norm/rope once — one gradient site per anchor, like HF.
    std::vector<std::vector<float>> dk_acc(c.layers), dv_acc(c.layers);
    std::vector<float> dple_in;
    if (ple > 0) dple_in.assign((size_t)T * c.layers * ple, 0.0f);

    std::vector<float> buf((size_t)T * H);
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
            std::vector<float> dp((size_t)T * H, 0.0f);
            rmsnorm_bwd(dx.data(), L.ple_p.data(),
                        p.w.at(ln(l, "ple_post")).d.data(),
                        L.rms_ple.data(), dp.data(),
                        p.dw(ln(l, "ple_post")), T, H);
            std::vector<float> dga((size_t)T * ple, 0.0f);
            linear_bwd(dp.data(), L.ple_ga.data(),
                       p.w.at(ln(l, "ple_proj")), dga.data(),
                       p.dw(ln(l, "ple_proj")), T, ple, H);
            std::vector<float> dg((size_t)T * ple, 0.0f);
            for (int t = 0; t < T; ++t) {
                const float* pl = o.g4_ple_in.data() +
                    ((size_t)t * c.layers + l) * ple;
                float* dpl = dple_in.data() +
                    ((size_t)t * c.layers + l) * ple;
                for (int d = 0; d < ple; ++d) {
                    const float g = L.ple_g[(size_t)t * ple + d];
                    const float du = dga[(size_t)t * ple + d];
                    dg[(size_t)t * ple + d] = du * pl[d] * gelu_tanh_d(g);
                    dpl[d] += du * gelu_tanh_f(g);
                }
            }
            std::vector<float> dxb((size_t)T * H, 0.0f);
            linear_bwd(dg.data(), L.x_ple.data(),
                       p.w.at(ln(l, "ple_gate")), dxb.data(),
                       p.dw(ln(l, "ple_gate")), T, H, ple);
            for (size_t i = 0; i < dx.size(); ++i) dx[i] += dxb[i];
        }
        {
            // x_ple = x_mid + ffn_norm(ffn_pre)
            std::vector<float> dproj((size_t)T * H, 0.0f);
            rmsnorm_bwd(dx.data(), L.ffn_pre.data(),
                        p.w.at(ln(l, "norm_ffn")).d.data(),
                        L.rms_ffn.data(), dproj.data(),
                        p.dw(ln(l, "norm_ffn")), T, H);
            std::vector<float> dfh((size_t)T * inter, 0.0f);
            linear_bwd(dproj.data(), L.fh.data(), p.w.at(ln(l, "w2")),
                       dfh.data(), p.dw(ln(l, "w2")),
                       T, inter, H);
            std::vector<float> dfa((size_t)T * inter, 0.0f);
            std::vector<float> dfb((size_t)T * inter, 0.0f);
            for (size_t i = 0; i < L.fh.size(); ++i) {
                const float a = L.fa[i], b = L.fb[i], d = dfh[i];
                dfa[i] += d * b * act_d(c, a);
                dfb[i] += d * act_f(c, a);
            }
            std::vector<float> dn2((size_t)T * H, 0.0f);
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
            for (size_t i = 0; i < dx.size(); ++i) dx[i] += buf[i];
        }
        {
            // x_mid = x_in + attn_norm(o_pre); o_pre = attn_out @ wo
            std::vector<float> dproj((size_t)T * H, 0.0f);
            rmsnorm_bwd(dx.data(), L.o_pre.data(),
                        p.w.at(ln(l, "norm_attn")).d.data(),
                        L.rms_attn.data(), dproj.data(),
                        p.dw(ln(l, "norm_attn")), T, H);
            std::vector<float> dao((size_t)T * Hq, 0.0f);
            linear_bwd(dproj.data(), L.attn_out.data(),
                       p.w.at(ln(l, "wo")), dao.data(),
                       p.dw(ln(l, "wo")), T, Hq, H);
            std::vector<float> dq((size_t)T * Hq, 0.0f);
            if (dk_acc[owner].empty()) {
                dk_acc[owner].assign((size_t)T * Hkv, 0.0f);
                dv_acc[owner].assign((size_t)T * Hkv, 0.0f);
            }
            std::vector<float>& dk = dk_acc[owner];
            std::vector<float>& dv = dv_acc[owner];
            const int group = c.heads / c.kv_heads;
            for (int h = 0; h < c.heads; ++h) {
                const int kh = h / group;
                for (int t = 0; t < T; ++t) {
                    const int lo = std::max(0, t - win + 1);
                    const float* pr =
                        L.probs.data() + ((size_t)h * T + t) * T;
                    const float* dao_r =
                        dao.data() + ((size_t)t * c.heads + h) * hd;
                    std::vector<float> dscore((size_t)t + 1, 0.0f);
                    for (int s = lo; s <= t; ++s) {
                        const float* vr = KV.v.data() +
                            ((size_t)s * c.kv_heads + kh) * hd;
                        float dotv = 0.0f;
                        for (int i = 0; i < hd; ++i)
                            dotv += dao_r[i] * vr[i];
                        dscore[s] = dotv;
                    }
                    float dsum = 0.0f;
                    for (int s = lo; s <= t; ++s)
                        dsum += dscore[s] * pr[s];
                    const float* qr =
                        L.q.data() + ((size_t)t * c.heads + h) * hd;
                    float* dqr =
                        dq.data() + ((size_t)t * c.heads + h) * hd;
                    for (int s = lo; s <= t; ++s) {
                        const float ds =
                            pr[s] * (dscore[s] - dsum) * scale;
                        const float* kr = KV.k.data() +
                            ((size_t)s * c.kv_heads + kh) * hd;
                        float* dkr =
                            dk.data() + ((size_t)s * c.kv_heads + kh) * hd;
                        float* dvr =
                            dv.data() + ((size_t)s * c.kv_heads + kh) * hd;
                        for (int i = 0; i < hd; ++i) {
                            dqr[i] += ds * kr[i];
                            dkr[i] += ds * qr[i];
                            dvr[i] += pr[s] * dao_r[i];
                        }
                    }
                }
            }
            // un-rope + un-norm q (always own); k/v only at the owner.
            rope_ex(dq.data(), T, c.heads, hd, theta, rotary, true);
            std::vector<float> dn1((size_t)T * H, 0.0f);
            std::vector<float> dqp((size_t)T * Hq, 0.0f);
            rmsnorm_bwd(dq.data(), L.q_proj.data(),
                        p.w.at(ln(l, "q_norm")).d.data(),
                        L.rms_q.data(), dqp.data(),
                        p.dw(ln(l, "q_norm")), T * c.heads, hd);
            linear_bwd(dqp.data(), L.n1.data(), p.w.at(ln(l, "wq")),
                       dn1.data(), p.dw(ln(l, "wq")), T, H, Hq);
            if (!shared) {
                std::vector<float> dkp((size_t)T * Hkv, 0.0f);
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
                static std::vector<float> ones_buf;
                if ((int)ones_buf.size() < hd) ones_buf.assign(hd, 1.0f);
                std::vector<float> dvp((size_t)T * Hkv, 0.0f);
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
            for (size_t i = 0; i < dx.size(); ++i) dx[i] += buf[i];
        }
    }

    // PLE model-level backward: dple_in -> (ctx-norm chain, embed_ple).
    std::vector<float> dx0 = dx;                   // grad wrt x0
    if (ple > 0) {
        const int L = c.layers;
        const float mix = std::pow(2.0f, -0.5f);
        const float tok_scale = std::sqrt((float)ple);
        const float ctx_scale = 1.0f / std::sqrt((float)H);
        std::vector<float> dctxn(dple_in.size());
        float* gepl = p.dw("embed_ple");
        for (int t = 0; t < T; ++t) {
            const float* din =
                dple_in.data() + (size_t)t * L * ple;
            float* dc = dctxn.data() + (size_t)t * L * ple;
            float* ger = gepl ? gepl + (size_t)ids[t] * L * ple : nullptr;
            for (int i = 0; i < L * ple; ++i) {
                dc[i] = din[i] * mix;
                if (ger) ger[i] += din[i] * mix * tok_scale;
            }
        }
        std::vector<float> dctx(dctxn.size(), 0.0f);
        rmsnorm_bwd(dctxn.data(), o.g4_ctx_in.data(),
                    p.w.at("ple_proj_norm").d.data(),
                    o.g4_ctx_rms.data(), dctx.data(),
                    p.dw("ple_proj_norm"), T * L, ple);
        for (float& v : dctx) v *= ctx_scale;
        linear_bwd(dctx.data(), o.g4_x0.data(),
                   p.w.at("ple_model_proj"), dx0.data(),
                   p.dw("ple_model_proj"), T, H, L * ple);
    }
    // embed backward (x0 = embed*emb_scale); tied lm_head already routed.
    float* gemb = p.dw("embed");
    if (gemb)
        for (int t = 0; t < T; ++t) {
            float* ger = gemb + (size_t)ids[t] * H;
            const float* dxr = dx0.data() + (size_t)t * H;
            for (int i = 0; i < H; ++i) ger[i] += dxr[i] * emb_scale;
        }
}

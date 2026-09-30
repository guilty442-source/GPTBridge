// engine_mtp.h — NativeMtpDrafter (XCN10): engine-side MTP head bound at
// load and dispatched from decode_continue on the greedy path.
//
// Contract:
//   * Manifest declares the head via num_nextn_predict_layers (>0, flat
//     model.mtp.<role>.weight set) or mtp_stack_depth (>0, stacked
//     model.mtp.{d}.<role>.weight modules — the engine binds depth 0).
//   * Declaration without tensors -> MTP_HEAD_MISSING; tensors without
//     declaration, wrong shapes, or stack depth > 1 ->
//     MTP_BUNDLE_MISMATCH. Both fail closed inside load().
//   * Draft-verify is greedy-only: verification is argmax equality, so
//     dispatch engages only when sampling cannot change the emitted
//     distribution (do_sample off / temperature<=0, repetition penalty
//     neutral). Every committed token is either the trunk argmax or a
//     draft proven identical to it — output parity is by construction,
//     not a measured approximation.
//   * The drafter's own causal KV is built from committed pairs only:
//     pair index p = (h_p, e_{p+1}). Prompt pairs are seeded at prefill
//     when the full hidden matrix is available; otherwise (prefill
//     artifact join, prefix-cache hit) the drafter starts from the first
//     decode position — draft quality degrades, parity never does.
#pragma once

namespace {

// Row-local helpers for the single-position MTP block: kept separate
// from the engine's batched kernels because the drafter always runs
// fp64 matvecs on a handful of rows.
void mtp_rmsnorm(const double* x, const double* w, double* y,
                 int64_t n, double eps) {
    double ss = 0.0;
    for (int64_t i = 0; i < n; ++i) ss += x[i] * x[i];
    const double inv = 1.0 / std::sqrt(ss / static_cast<double>(n) + eps);
    for (int64_t i = 0; i < n; ++i) y[i] = x[i] * w[i] * inv;
}

void mtp_matvec(const TensorView& W, const double* x, double* y,
                int64_t out, int64_t in) {
    for (int64_t o = 0; o < out; ++o) {
        const double* r = W.data + static_cast<size_t>(o) * in;
        double s = 0.0;
        for (int64_t i = 0; i < in; ++i) s += r[i] * x[i];
        y[o] = s;
    }
}

double mtp_gate_act(double x, bool gelu) {
    if (!gelu) return x / (1.0 + std::exp(-x));
    const double c0 = 0.7978845608028654, c1 = 0.044715;
    const double u = c0 * (x + c1 * x * x * x);
    return 0.5 * x * (1.0 + std::tanh(u));
}

// Interleaved-pair RoPE over the FULL head_dim with the YaRN-blended
// table — the trainer's MTP convention (deliberately not the trunk's
// rotate-half partial rope). `pos` is the pair index = trunk position
// of the hidden state.
void mtp_rope(const ModelConfig& cfg, double* v, int64_t nheads,
              int64_t pos) {
    const int64_t hd = cfg.head_dim;
    const int64_t half = hd / 2;
    const double theta = cfg.rope_theta;
    const bool yarn = cfg.use_yarn();
    const double ms = !yarn ? 1.0 :
        (cfg.yarn_attention_factor > 0.0
             ? cfg.yarn_attention_factor
             : 0.1 * std::log(cfg.yarn_factor) + 1.0);
    const double logb = std::log(theta);
    auto blend = [&](int64_t p) {
        if (!yarn) return 1.0;
        auto corr = [&](double beta) {
            return static_cast<double>(hd) *
                std::log(static_cast<double>(
                             cfg.yarn_original_max_position_embeddings) /
                         (beta * 6.28318530718)) / (2.0 * logb);
        };
        double lo = std::max(0.0, std::floor(corr(cfg.yarn_beta_fast)));
        double hi = std::min(static_cast<double>(half - 1),
                             std::ceil(corr(cfg.yarn_beta_slow)));
        if (hi == lo) hi = lo + 1e-3;
        double ext = 1.0 - std::min(1.0,
            std::max(0.0, (static_cast<double>(p) - lo) / (hi - lo)));
        return ext + (1.0 - ext) / cfg.yarn_factor;
    };
    for (int64_t h = 0; h < nheads; ++h) {
        double* r = v + static_cast<size_t>(h) * hd;
        for (int64_t p = 0; p < half; ++p) {
            double fr = std::pow(theta, -2.0 * (double)p / hd) * blend(p);
            double co = std::cos((double)pos * fr) * ms;
            double si = std::sin((double)pos * fr) * ms;
            double a = r[2 * p], bv = r[2 * p + 1];
            r[2 * p] = a * co - bv * si;
            r[2 * p + 1] = a * si + bv * co;
        }
    }
}

}  // namespace

void NativeInferenceEngine::bind_mtp_drafter() {
    const ModelConfig& cfg = bundle_->config();
    mtp_ = MtpDrafter{};
    // Declaration/tensor parity both directions — a bundle that carries
    // MTP tensors without declaring them, or declares without carrying,
    // is a contract breach (mirrors the export/import parity checks).
    bool tensors_present =
        bundle_->has_tensor("model.mtp.norm_h.weight") ||
        bundle_->has_tensor("model.mtp.0.eh.weight");
    if (!cfg.declares_mtp()) {
        if (tensors_present) {
            throw InferenceError("MTP_BUNDLE_MISMATCH:undeclared-tensors");
        }
        return;
    }
    if (cfg.num_nextn_predict_layers > 1 || cfg.mtp_stack_depth > 1) {
        throw InferenceError("MTP_BUNDLE_MISMATCH:stack-depth>1");
    }

    // Prefer the flat nextn naming; fall back to the XCN10 stack's
    // depth-0 module — never mix families.
    static const char* kFlat[13] = {
        "model.mtp.norm_h.weight", "model.mtp.norm_e.weight",
        "model.mtp.w_proj.weight", "model.mtp.norm1.weight",
        "model.mtp.wq.weight", "model.mtp.wk.weight",
        "model.mtp.wv.weight", "model.mtp.wo.weight",
        "model.mtp.norm2.weight", "model.mtp.w1.weight",
        "model.mtp.w3.weight", "model.mtp.w2.weight",
        "model.mtp.norm_out.weight"};
    static const char* kRole[13] = {
        "eh", "et", "proj", "norm1", "wq", "wk", "wv", "wo",
        "norm2", "w1", "w3", "w2", "norm_o"};
    std::string names[13];
    bool flat_ok = true;
    for (int i = 0; i < 13; ++i) {
        if (!bundle_->has_tensor(kFlat[i])) { flat_ok = false; break; }
        names[i] = kFlat[i];
    }
    if (!flat_ok) {
        for (int i = 0; i < 13; ++i)
            names[i] = "model.mtp.0." + std::string(kRole[i]) + ".weight";
    }
    TensorView* dst[13] = {
        &mtp_.norm_h, &mtp_.norm_e, &mtp_.w_proj, &mtp_.norm1,
        &mtp_.wq, &mtp_.wk, &mtp_.wv, &mtp_.wo,
        &mtp_.norm2, &mtp_.w1, &mtp_.w3, &mtp_.w2, &mtp_.norm_out};
    for (int i = 0; i < 13; ++i) {
        if (!bundle_->has_tensor(names[i])) {
            throw InferenceError(
                std::string("MTP_HEAD_MISSING:") + names[i]);
        }
        *dst[i] = bundle_->tensor(names[i]);
        if (dst[i]->data == nullptr || dst[i]->size() <= 0) {
            throw InferenceError(
                std::string("MTP_HEAD_MISSING:") + names[i]);
        }
    }
    // Shape validation against the trunk geometry.
    const int64_t H = cfg.hidden_size, hd = cfg.head_dim;
    const int64_t nh = cfg.num_attention_heads,
                  kvh = cfg.num_key_value_heads;
    const int64_t I = cfg.intermediate_size;
    const std::vector<int64_t> want[13] = {
        {H}, {H}, {H, 2 * H}, {H},
        {nh * hd, H}, {kvh * hd, H}, {kvh * hd, H}, {H, nh * hd},
        {H}, {I, H}, {I, H}, {H, I}, {H}};
    for (int i = 0; i < 13; ++i) {
        if (dst[i]->shape != want[i]) {
            throw InferenceError(
                std::string("MTP_BUNDLE_MISMATCH:") + names[i]);
        }
    }
    mtp_.family = flat_ok ? 0 : 1;
    mtp_.bound = true;
    mtp_.reset();
}

// z = w_proj [rmsnorm(h; norm_h) | rmsnorm(e_next; norm_e)] — the shared
// projection feeding every draft position.
std::vector<double> NativeInferenceEngine::mtp_z_of(
    const double* h_t, const double* e_next) const {
    const ModelConfig& cfg = bundle_->config();
    const int64_t H = cfg.hidden_size;
    std::vector<double> cin(static_cast<size_t>(2 * H));
    mtp_rmsnorm(h_t, mtp_.norm_h.data, cin.data(), H, cfg.rms_norm_eps);
    mtp_rmsnorm(e_next, mtp_.norm_e.data, cin.data() + H,
                H, cfg.rms_norm_eps);
    std::vector<double> z(static_cast<size_t>(H));
    mtp_matvec(mtp_.w_proj, cin.data(), z.data(), H, 2 * H);
    return z;
}

// Append pair (h_pos, e_{pos+1})'s K/V into the drafter cache. Called
// once per committed pair, in order — positions never regress.
void NativeInferenceEngine::mtp_append_kv(
    const std::vector<double>& z, int64_t pos) {
    const ModelConfig& cfg = bundle_->config();
    const int64_t H = cfg.hidden_size;
    const int64_t kvl = cfg.num_key_value_heads * cfg.head_dim;
    std::vector<double> n1(static_cast<size_t>(H));
    mtp_rmsnorm(z.data(), mtp_.norm1.data, n1.data(), H,
                cfg.rms_norm_eps);
    std::vector<double> k(static_cast<size_t>(kvl)),
        v(static_cast<size_t>(kvl));
    mtp_matvec(mtp_.wk, n1.data(), k.data(), kvl, H);
    mtp_matvec(mtp_.wv, n1.data(), v.data(), kvl, H);
    mtp_rope(cfg, k.data(), cfg.num_key_value_heads, pos);
    mtp_.kv_k.insert(mtp_.kv_k.end(), k.begin(), k.end());
    mtp_.kv_v.insert(mtp_.kv_v.end(), v.begin(), v.end());
    ++mtp_.positions;
}

// Commit a pair (h_pos, e_{pos+1}=next_token) without drafting — used
// for prompt seeding and for the second committed token of an accepted
// pair.
void NativeInferenceEngine::mtp_commit_pair(
    const double* h_pos, int64_t next_token, int64_t pos) {
    const ModelConfig& cfg = bundle_->config();
    mtp_append_kv(
        mtp_z_of(h_pos,
                 embedding_.data + static_cast<size_t>(next_token) *
                     cfg.hidden_size),
        pos);
}

// Draft the token at position pos+2 from the just-appended pair
// (h_pos, e_{pos+1}): full MTP block forward over the drafter's own
// causal KV (self-included), then lm_head argmax.
int64_t NativeInferenceEngine::mtp_draft_token(
    const double* h_last, int64_t next_token, int64_t pos) {
    const ModelConfig& cfg = bundle_->config();
    const int64_t H = cfg.hidden_size, hd = cfg.head_dim;
    const int64_t nh = cfg.num_attention_heads,
                  kvh = cfg.num_key_value_heads;
    const int64_t kvl = kvh * hd, Hq = nh * hd;
    const int64_t group = nh / kvh;

    std::vector<double> z = mtp_z_of(
        h_last, embedding_.data + static_cast<size_t>(next_token) * H);
    mtp_append_kv(z, pos);

    std::vector<double> n1(static_cast<size_t>(H));
    mtp_rmsnorm(z.data(), mtp_.norm1.data, n1.data(), H,
                cfg.rms_norm_eps);
    std::vector<double> q(static_cast<size_t>(Hq));
    mtp_matvec(mtp_.wq, n1.data(), q.data(), Hq, H);
    mtp_rope(cfg, q.data(), nh, pos);
    const double scale = 1.0 / std::sqrt(static_cast<double>(hd));
    std::vector<double> attn(static_cast<size_t>(Hq), 0.0);
    std::vector<double> scores(static_cast<size_t>(mtp_.positions));
    for (int64_t h = 0; h < nh; ++h) {
        const int64_t kh = h / group;
        const double* qr = q.data() + static_cast<size_t>(h) * hd;
        double mx = -1e300;
        for (int64_t s = 0; s < mtp_.positions; ++s) {
            const double* kr = mtp_.kv_k.data() +
                static_cast<size_t>(s) * kvl + static_cast<size_t>(kh) * hd;
            double d = 0.0;
            for (int64_t i = 0; i < hd; ++i) d += qr[i] * kr[i];
            scores[static_cast<size_t>(s)] = d * scale;
            mx = std::max(mx, scores[static_cast<size_t>(s)]);
        }
        double sum = 0.0;
        for (int64_t s = 0; s < mtp_.positions; ++s) {
            scores[static_cast<size_t>(s)] =
                std::exp(scores[static_cast<size_t>(s)] - mx);
            sum += scores[static_cast<size_t>(s)];
        }
        double* ao = attn.data() + static_cast<size_t>(h) * hd;
        for (int64_t s = 0; s < mtp_.positions; ++s) {
            const double p = scores[static_cast<size_t>(s)] / sum;
            const double* vr = mtp_.kv_v.data() +
                static_cast<size_t>(s) * kvl + static_cast<size_t>(kh) * hd;
            for (int64_t i = 0; i < hd; ++i) ao[i] += p * vr[i];
        }
    }
    std::vector<double> proj(static_cast<size_t>(H));
    mtp_matvec(mtp_.wo, attn.data(), proj.data(), H, Hq);
    std::vector<double> xres(static_cast<size_t>(H));
    for (int64_t i = 0; i < H; ++i) xres[static_cast<size_t>(i)] =
        z[static_cast<size_t>(i)] + proj[static_cast<size_t>(i)];
    std::vector<double> n2(static_cast<size_t>(H));
    mtp_rmsnorm(xres.data(), mtp_.norm2.data, n2.data(), H,
                cfg.rms_norm_eps);
    const bool gelu = cfg.hidden_act == "gelu" ||
                      cfg.hidden_act == "geglu" ||
                      cfg.hidden_act == "gelu_tanh";
    std::vector<double> fa(static_cast<size_t>(cfg.intermediate_size)),
        fb(static_cast<size_t>(cfg.intermediate_size)),
        fh(static_cast<size_t>(cfg.intermediate_size));
    mtp_matvec(mtp_.w1, n2.data(), fa.data(), cfg.intermediate_size, H);
    mtp_matvec(mtp_.w3, n2.data(), fb.data(), cfg.intermediate_size, H);
    for (size_t i = 0; i < fh.size(); ++i)
        fh[i] = mtp_gate_act(fa[i], gelu) * fb[i];
    mtp_matvec(mtp_.w2, fh.data(), proj.data(), H,
               cfg.intermediate_size);
    for (int64_t i = 0; i < H; ++i)
        xres[static_cast<size_t>(i)] += proj[static_cast<size_t>(i)];
    std::vector<double> out(static_cast<size_t>(H));
    mtp_rmsnorm(xres.data(), mtp_.norm_out.data, out.data(), H,
                cfg.rms_norm_eps);
    int64_t best = 0;
    double best_v = -std::numeric_limits<double>::infinity();
    const int64_t V = cfg.vocab_size;
    for (int64_t t = 0; t < V; ++t) {
        const double* r = lm_head_.data + static_cast<size_t>(t) * H;
        double s = 0.0;
        for (int64_t i = 0; i < H; ++i)
            s += r[i] * out[static_cast<size_t>(i)];
        if (s > best_v) { best_v = s; best = t; }
    }
    return best;
}

// ---- speculative decode loop (greedy fast path only) ------------------
//
// Per iteration: emit the trunk argmax `tok`; the drafter appends pair
// (h_{last}, e_tok) and predicts the NEXT token `d`. Forward {tok, d}
// through the trunk in one call — row0's logits verify d (trunk's own
// pick for the position after tok), row1 produces the logits for the
// step after that.
//
//   Accept (row0 argmax == d): emit d too — 2 tokens / 1 forward.
//   Reject: row1's KV/delta was built on a wrong token. Restore the
//     pre-window delta snapshot and roll kv_lens back past both rows,
//     then re-forward {tok, c} where c = row0 argmax — 2 tokens / 2
//     forwards, the same forward count as the plain loop plus the
//     wasted draft. §37: when acceptance is persistently low the
//     measured net gain is negative and mtp-speedup's AUTO-off keeps
//     the feature honest.
std::vector<int64_t> NativeInferenceEngine::decode_continue_spec(
    std::vector<double> next_logits, std::vector<double> last_hidden,
    int64_t max_new_tokens, std::vector<int64_t>& generated) {
    const ModelConfig& cfg = bundle_->config();
    const int64_t H = cfg.hidden_size;
    const bool hybrid = cfg.has_linear_layers();
    std::string turn_tail;
    turn_tail.reserve(64);
    auto argmax_of = [](const std::vector<double>& lg) {
        return static_cast<int64_t>(std::distance(
            lg.begin(), std::max_element(lg.begin(), lg.end())));
    };
    auto stop_now = [&]() {
        return (int64_t)generated.size() >= max_new_tokens ||
               (turn_tail.size() >= 7 &&
                turn_tail.compare(turn_tail.size() - 7, 7, "<|eot|>") ==
                    0);
    };
    auto emit = [&](int64_t tok) {
        generated.push_back(tok);
        sequence_.push_back(tok);
        turn_tail += tokenizer_->decode({tok}, false);
        if (turn_tail.size() > 64)
            turn_tail.erase(0, turn_tail.size() - 64);
        return tok == cfg.eos_token_id;
    };
    auto last_logits_from = [&](const double* hrow) {
        std::vector<double> lg = matmul(hrow, 1, H, lm_head_t_.data(),
                                        cfg.vocab_size);
        logit_softcap(lg, cfg.final_logit_softcapping);
        return lg;
    };

    while ((int64_t)generated.size() < max_new_tokens) {
        const int64_t tok = argmax_of(next_logits);
        if (emit(tok) || stop_now()) break;
        const int64_t hpos = static_cast<int64_t>(sequence_.size()) - 2;
        // hpos = trunk position of last_hidden (h_{N-1}); tok occupies
        // N, the draft predicts N+1.

        if (last_hidden.empty()) {
            // Bootstrap: no committed-position hidden yet (artifact
            // join / prefix replay). Plain forward — drafting engages
            // next iteration once a committed hidden exists.
            std::vector<double> hid =
                forward_hidden({tok}, kv_lens_[0], true);
            last_hidden.assign(hid.end() - H, hid.end());
            next_logits = last_logits_from(last_hidden.data());
            continue;
        }

        ++mtp_.proposed;
        const int64_t draft =
            mtp_draft_token(last_hidden.data(), tok, hpos);

        // Speculative window {tok, draft}: snapshot for the reject path.
        if (hybrid) mtp_lin_snapshot_ = lin_states_[0];
        std::vector<int64_t> win{tok, draft};
        std::vector<double> hid =
            forward_hidden(win, kv_lens_[0], true);
        ++mtp_.spec_forwards;
        const double* h_tok = hid.data();          // h after tok (pos N)
        const double* h_d = hid.data() + H;        // h after d  (pos N+1)
        std::vector<double> lg0 = last_logits_from(h_tok);
        const int64_t c = argmax_of(lg0);
        if (c == draft) {
            // Accept: tok + draft committed. The pair (h_N, e_d) is
            // committed too — append it so the drafter stays contiguous.
            ++mtp_.accepted;
            if (emit(draft) || stop_now()) break;
            mtp_commit_pair(h_tok, draft, hpos + 1);
            next_logits = last_logits_from(h_d);
            last_hidden.assign(h_d, h_d + H);
            continue;
        }
        // Reject: emit the trunk's own pick c for position N+1. KV row
        // N+1 and the delta update carry d — roll both back and
        // re-forward {tok, c} so every committed row is real.
        if (hybrid) lin_states_[0] = mtp_lin_snapshot_;
        kv_lens_[0] = static_cast<int64_t>(sequence_.size()) - 1;
        if (emit(c) || stop_now()) {
            // c commits but its KV/state never landed — harmless: the
            // generation is done and the cache dies with the request.
            break;
        }
        std::vector<int64_t> fix{tok, c};
        std::vector<double> hid2 =
            forward_hidden(fix, kv_lens_[0], true);
        ++mtp_.spec_forwards;
        const double* h_tok2 = hid2.data();
        const double* h_c = hid2.data() + H;
        mtp_commit_pair(h_tok2, c, hpos + 1);
        next_logits = last_logits_from(h_c);
        last_hidden.assign(h_c, h_c + H);
    }
    return generated;
}

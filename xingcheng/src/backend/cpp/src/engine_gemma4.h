// engine_gemma4.h — fragment of engine.cpp (Gemma4 hybrid-attention
// forward path). Included once by engine.cpp inside namespace
// xingcheng::inference.
//
// Mirrors the Gemma4 text decoder (HF modeling_gemma4 /
// Gemma4TextConfig semantics) for the dense E-family profile:
//   embed*sqrt(hidden) -> per layer:
//     x += post_attn_norm(attn(input_norm(x)))          [4-norm block]
//     x += post_ffn_norm(mlp(pre_ffn_norm(x)))          [gelu-tanh GLU]
//     x += ple_norm(ple_proj(gelu(ple_gate(x)) * ple_in))  [PLE, opt]
//   -> final norm; logits = cap*tanh(logits/cap)
// Attention: per-layer-type head_dim + RoPE (default theta/proportional
// p-RoPE), QK-RMSNorm + scale-free v-norm, scale=attention_scale (1.0),
// sliding-window masking on sliding layers, and tail layers that reuse
// their type anchor's full-length KV (num_kv_shared_layers).
#pragma once

namespace {
// gelu_pytorch_tanh — HF ACT2FN entry for Gemma4 hidden_activation.
double gelu_tanh_f(double x) {
    constexpr double c = 0.7978845608028654;  // sqrt(2/pi)
    const double inner = c * (x + 0.044715 * x * x * x);
    return 0.5 * x * (1.0 + std::tanh(inner));
}

// rmsnorm over the last dim of a row-major [rows x cols] buffer with an
// explicit weight pointer (nullptr -> scale-free normalization).
void rmsnorm_ptr(
    const double* in, int64_t rows, int64_t cols,
    const double* weight, double eps, double* out) {
    static const std::vector<double> ones(4096, 1.0);
    if (weight == nullptr) {
        if (cols > static_cast<int64_t>(ones.size())) {
            throw InferenceError("RMSNORM_PLAIN_DIM_UNSUPPORTED");
        }
        weight = ones.data();
    }
    checked_c_call(
        gptbridge_native_transformer_rmsnorm(
            in, rows, cols, weight, eps, out),
        "rmsnorm-ptr");
}

// cos/sin tables from an explicit inv_freq vector (half-split layout,
// same convention as rope_tables): cos[s][i] = cos(pos*inv[i]) and
// cos[s][i+half] shares the value. inv entries of 0 produce cos=1/sin=0
// — proportional-RoPE NoPE dims pass through untouched.
void rope_tables_inv(
    int64_t seq_len, int64_t offset, int64_t dim,
    const std::vector<double>& inv,
    std::vector<double>& cos_out, std::vector<double>& sin_out) {
    cos_out.assign(static_cast<size_t>(seq_len * dim), 0.0);
    sin_out.assign(static_cast<size_t>(seq_len * dim), 0.0);
    const int64_t half = dim / 2;
    for (int64_t s = 0; s < seq_len; ++s) {
        const double position = static_cast<double>(offset + s);
        for (int64_t i = 0; i < half; ++i) {
            const double angle = position * inv[static_cast<size_t>(i)];
            cos_out[static_cast<size_t>(s * dim + i)] = std::cos(angle);
            cos_out[static_cast<size_t>(s * dim + i + half)] =
                std::cos(angle);
            sin_out[static_cast<size_t>(s * dim + i)] = std::sin(angle);
            sin_out[static_cast<size_t>(s * dim + i + half)] =
                std::sin(angle);
        }
    }
}
}  // namespace

std::vector<double> NativeInferenceEngine::forward_batch_hidden_gemma4(
    const std::vector<BatchSpan>& spans,
    std::vector<double>* layer_rms,
    std::vector<double>* module_rms) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (spans.empty()) throw InferenceError("INPUT_EMPTY");
    const ModelConfig& cfg = bundle_->config();
    const int64_t hidden_size = cfg.hidden_size;
    const int64_t num_layers = cfg.num_hidden_layers;
    const int64_t ple = cfg.hidden_size_per_layer_input;
    std::vector<int64_t> starts(spans.size());
    int64_t total_tokens = 0;
    for (size_t i = 0; i < spans.size(); ++i) {
        const BatchSpan& span = spans[i];
        if (span.ids == nullptr || span.ids->empty()) {
            throw InferenceError("INPUT_EMPTY");
        }
        const int64_t seq = static_cast<int64_t>(span.ids->size());
        if (span.embed_override != nullptr &&
            static_cast<int64_t>(span.embed_override->size()) !=
                seq * hidden_size) {
            throw InferenceError("EMBED_OVERRIDE_DIM_MISMATCH");
        }
        if (span.position_offset < 0 ||
            span.position_offset + seq > cfg.max_position_embeddings) {
            throw InferenceError("SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS");
        }
        if (span.append_cache || span.position_offset > 0) {
            if (span.slot < 0 ||
                span.slot >= static_cast<int64_t>(kv_lens_.size()) ||
                !kv_slot_active_[static_cast<size_t>(span.slot)]) {
                throw InferenceError("KV_SLOT_INVALID");
            }
            if (span.append_cache &&
                span.position_offset != kv_lens_[static_cast<size_t>(span.slot)]) {
                throw InferenceError("KV_CACHE_POSITION_MISMATCH");
            }
        }
        starts[i] = total_tokens;
        total_tokens += seq;
    }

    const double emb_scale =
        cfg.embedding_scale != 0.0 ? cfg.embedding_scale : 1.0;
    std::vector<double> hidden(static_cast<size_t>(total_tokens * hidden_size));
    for (size_t i = 0; i < spans.size(); ++i) {
        const BatchSpan& span = spans[i];
        const int64_t seq = static_cast<int64_t>(span.ids->size());
        const int64_t base = starts[i];
        for (int64_t s = 0; s < seq; ++s) {
            double* dst =
                hidden.data() + static_cast<size_t>((base + s) * hidden_size);
            if (span.embed_override != nullptr) {
                // Latent rows arrive already normed — the Gemma embedding
                // scale applies to token embeddings only.
                std::copy_n(
                    span.embed_override->data() +
                        static_cast<size_t>(s * hidden_size),
                    hidden_size, dst);
                continue;
            }
            const int64_t token = (*span.ids)[static_cast<size_t>(s)];
            if (token < 0 || token >= cfg.vocab_size) {
                throw InferenceError("TOKEN_ID_OUT_OF_RANGE");
            }
            std::copy_n(
                embedding_.data + token * hidden_size, hidden_size, dst);
            if (emb_scale != 1.0) {
                for (int64_t d = 0; d < hidden_size; ++d) dst[d] *= emb_scale;
            }
        }
    }
    if (layer_rms != nullptr) {
        layer_rms->push_back(hidden_rms(hidden, total_tokens, hidden_size));
    }
    if (module_rms != nullptr) {
        module_rms->push_back(hidden_rms(hidden, total_tokens, hidden_size));
    }

    // PLE pipeline (HF get_per_layer_inputs + project_per_layer_inputs):
    //   identity  = embed_per_layer[token] * sqrt(ple)          [T,L,ple]
    //   context   = ple_proj_norm(ple_model_proj(hidden)*H^-0.5)[T,L,ple]
    //   ple_in    = (context + identity) * 2^-0.5
    std::vector<double> ple_in;
    if (ple > 0) {
        ple_in.assign(
            static_cast<size_t>(total_tokens * num_layers * ple), 0.0);
        const double tok_scale = std::sqrt(static_cast<double>(ple));
        for (size_t i = 0; i < spans.size(); ++i) {
            const BatchSpan& span = spans[i];
            const int64_t seq = static_cast<int64_t>(span.ids->size());
            const int64_t base = starts[i];
            for (int64_t s = 0; s < seq; ++s) {
                if (span.embed_override != nullptr) {
                    // Latent tokens have no vocab row: the PLE identity
                    // term stays zero and only the context term flows.
                    continue;
                }
                const int64_t token = (*span.ids)[static_cast<size_t>(s)];
                if (token >= cfg.vocab_size_per_layer_input) {
                    throw InferenceError("PLE_TOKEN_ID_OUT_OF_RANGE");
                }
                const double* src =
                    embed_per_layer_.data + token * num_layers * ple;
                double* dst = ple_in.data() +
                    static_cast<size_t>((base + s) * num_layers * ple);
                for (int64_t d = 0; d < num_layers * ple; ++d) {
                    dst[d] = src[d] * tok_scale;
                }
            }
        }
        std::vector<double> ctx = linear(
            hidden, total_tokens, hidden_size, ple_model_projection_t_,
            num_layers * ple);
        const double ctx_scale =
            1.0 / std::sqrt(static_cast<double>(hidden_size));
        for (double& value : ctx) value *= ctx_scale;
        const int64_t rows = total_tokens * num_layers;
        std::vector<double> ctxn(ctx.size());
        rmsnorm_ptr(
            ctx.data(), rows, ple, ple_projection_norm_.data,
            cfg.rms_norm_eps, ctxn.data());
        const double mix = std::pow(2.0, -0.5);
        for (size_t i = 0; i < ple_in.size(); ++i) {
            ple_in[i] = (ctxn[i] + ple_in[i]) * mix;
        }
    }

    // Per-span RoPE tables per layer_type. sliding: default rope over
    // head_dim; full: proportional rope over global_head_dim — inv_freq
    // entries are zero past rope_angles so the tail dims pass through.
    std::vector<double> inv_sliding, inv_full;
    {
        const int64_t half = cfg.head_dim / 2;
        inv_sliding.assign(static_cast<size_t>(half), 0.0);
        for (int64_t i = 0; i < half; ++i) {
            inv_sliding[static_cast<size_t>(i)] = std::pow(
                cfg.rope_theta,
                -2.0 * static_cast<double>(i) /
                    static_cast<double>(cfg.head_dim));
        }
        const int64_t ghalf = cfg.global_head_dim / 2;
        inv_full.assign(static_cast<size_t>(ghalf), 0.0);
        const int64_t rope_angles = static_cast<int64_t>(
            cfg.rope_partial_factor_full *
            static_cast<double>(cfg.global_head_dim) / 2.0);
        for (int64_t i = 0; i < rope_angles && i < ghalf; ++i) {
            inv_full[static_cast<size_t>(i)] = std::pow(
                cfg.rope_theta_full,
                -2.0 * static_cast<double>(i) /
                    static_cast<double>(cfg.global_head_dim));
        }
    }
    std::vector<std::vector<double>> rope_cos_sw(spans.size());
    std::vector<std::vector<double>> rope_sin_sw(spans.size());
    std::vector<std::vector<double>> rope_cos_gl(spans.size());
    std::vector<std::vector<double>> rope_sin_gl(spans.size());
    for (size_t i = 0; i < spans.size(); ++i) {
        const int64_t seq = static_cast<int64_t>(spans[i].ids->size());
        rope_tables_inv(
            seq, spans[i].position_offset, cfg.head_dim, inv_sliding,
            rope_cos_sw[i], rope_sin_sw[i]);
        rope_tables_inv(
            seq, spans[i].position_offset, cfg.global_head_dim, inv_full,
            rope_cos_gl[i], rope_sin_gl[i]);
    }

    // Shared-KV retention: anchor layers publish their current-step
    // post-RoPE/post-norm K and post-norm V per span under their
    // layer_type; tail (kv_shared) layers consume them and produce no
    // K/V of their own.
    std::unordered_map<std::string,
        std::vector<std::vector<double>>> shared_k, shared_v;

    for (int64_t layer_idx = 0; layer_idx < num_layers; ++layer_idx) {
        LayerWeights& layer = layers_[static_cast<size_t>(layer_idx)];
        const std::string& ltype =
            cfg.layer_types[static_cast<size_t>(layer_idx)];
        const bool sliding = ltype == "sliding_attention";
        const int64_t hd = layer.g4_head_dim;
        const int64_t q_dim = layer.g4_q_dim;
        const int64_t kv_dim = layer.g4_kv_dim;
        const int64_t kv_layer =
            layer.kv_shared ? layer.kv_anchor_layer : layer_idx;
        const std::vector<std::vector<double>>& rc =
            sliding ? rope_cos_sw : rope_cos_gl;
        const std::vector<std::vector<double>>& rs =
            sliding ? rope_sin_sw : rope_sin_gl;
        const double scale =
            cfg.attention_scale > 0.0
                ? cfg.attention_scale
                : 1.0 / std::sqrt(static_cast<double>(hd));
        // Every non-shared layer publishes; the last writer of each
        // type is its anchor, so tail layers always see the anchor's
        // retained heads.
        const bool store_kv_anchor =
            !layer.kv_shared && cfg.num_kv_shared_layers > 0;

        std::vector<double> normed = rmsnorm(
            hidden, total_tokens, hidden_size, layer.input_norm,
            cfg.rms_norm_eps);
        if (module_rms != nullptr) {
            module_rms->push_back(
                hidden_rms(normed, total_tokens, hidden_size));
        }

        std::vector<double> q_flat = linear(
            normed, total_tokens, hidden_size, layer.q_proj_t, q_dim);
        std::vector<double> k_flat;
        std::vector<double> v_flat;
        if (!layer.kv_shared) {
            k_flat = linear(
                normed, total_tokens, hidden_size, layer.k_proj_t,
                kv_dim);
            v_flat = linear(
                normed, total_tokens, hidden_size, layer.v_proj_t,
                kv_dim);
        }

        std::vector<double> attn_flat(
            static_cast<size_t>(total_tokens * q_dim), 0.0);
        const int64_t head_ratio =
            cfg.num_attention_heads / cfg.num_key_value_heads;
        double rope_q_sumsq = 0.0;
        int64_t rope_q_count = 0;
        for (size_t i = 0; i < spans.size(); ++i) {
            const BatchSpan& span = spans[i];
            const int64_t seq = static_cast<int64_t>(span.ids->size());
            const int64_t base = starts[i];
            const int64_t total_len = span.position_offset + seq;

            std::vector<double> q_heads(
                static_cast<size_t>(cfg.num_attention_heads * seq * hd));
            for (int64_t s = 0; s < seq; ++s) {
                for (int64_t h = 0; h < cfg.num_attention_heads; ++h) {
                    std::copy_n(
                        q_flat.data() +
                            static_cast<size_t>((base + s) * q_dim + h * hd),
                        hd,
                        q_heads.data() +
                            static_cast<size_t>((h * seq + s) * hd));
                }
            }
            // QK-norm: per-head RMSNorm over hd before RoPE.
            {
                std::vector<double> tmp(q_heads.size());
                rmsnorm_ptr(
                    q_heads.data(),
                    cfg.num_attention_heads * seq, hd, layer.q_norm.data,
                    cfg.rms_norm_eps, tmp.data());
                q_heads.swap(tmp);
            }
            std::vector<double> k_heads, v_heads;
            if (!layer.kv_shared) {
                k_heads.assign(
                    static_cast<size_t>(
                        cfg.num_key_value_heads * seq * hd), 0.0);
                v_heads.assign(k_heads.size(), 0.0);
                for (int64_t s = 0; s < seq; ++s) {
                    for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                        std::copy_n(
                            k_flat.data() +
                                static_cast<size_t>(
                                    (base + s) * kv_dim + h * hd),
                            hd,
                            k_heads.data() +
                                static_cast<size_t>((h * seq + s) * hd));
                        std::copy_n(
                            v_flat.data() +
                                static_cast<size_t>(
                                    (base + s) * kv_dim + h * hd),
                            hd,
                            v_heads.data() +
                                static_cast<size_t>((h * seq + s) * hd));
                    }
                }
                {
                    std::vector<double> tmp(k_heads.size());
                    rmsnorm_ptr(
                        k_heads.data(),
                        cfg.num_key_value_heads * seq, hd,
                        layer.k_norm.data, cfg.rms_norm_eps, tmp.data());
                    k_heads.swap(tmp);
                    rmsnorm_ptr(
                        v_heads.data(),
                        cfg.num_key_value_heads * seq, hd,
                        nullptr, cfg.rms_norm_eps, tmp.data());
                    v_heads.swap(tmp);
                }
            }
            // RoPE on q (always) and k (KV-producing layers only).
            {
                std::vector<double> q_rope(q_heads.size());
                checked_c_call(
                    gptbridge_native_transformer_rope(
                        q_heads.data(), 1, cfg.num_attention_heads, seq,
                        hd, rc[i].data(), rs[i].data(), q_rope.data()),
                    "rope-q");
                q_heads.swap(q_rope);
            }
            if (!layer.kv_shared) {
                std::vector<double> k_rope(k_heads.size());
                checked_c_call(
                    gptbridge_native_transformer_rope(
                        k_heads.data(), 1, cfg.num_key_value_heads, seq,
                        hd, rc[i].data(), rs[i].data(), k_rope.data()),
                    "rope-k");
                k_heads.swap(k_rope);
                if (span.append_cache) {
                    for (int64_t s = 0; s < seq; ++s) {
                        const int64_t position = span.position_offset + s;
                        kv_ensure_position(span.slot, position);
                        for (int64_t h = 0;
                             h < cfg.num_key_value_heads; ++h) {
                            kv_write(
                                span.slot, true, layer_idx, position, h,
                                k_heads.data() +
                                    static_cast<size_t>(
                                        (h * seq + s) * hd));
                            kv_write(
                                span.slot, false, layer_idx, position, h,
                                v_heads.data() +
                                    static_cast<size_t>(
                                        (h * seq + s) * hd));
                        }
                    }
                }
                if (store_kv_anchor) {
                    shared_k[ltype].resize(spans.size());
                    shared_v[ltype].resize(spans.size());
                    shared_k[ltype][i] = k_heads;
                    shared_v[ltype][i] = v_heads;
                }
            }
            if (module_rms != nullptr) {
                for (double value : q_heads) rope_q_sumsq += value * value;
                rope_q_count += static_cast<int64_t>(q_heads.size());
            }

            // Shared layers never touch the K/V projections: prefix
            // positions read the anchor's cached rows, current-step
            // positions read the anchor's retained heads — mirroring HF
            // shared_kv_states[layer_type].
            const std::vector<double>* cur_k = &k_heads;
            const std::vector<double>* cur_v = &v_heads;
            if (layer.kv_shared) {
                cur_k = &shared_k[ltype][i];
                cur_v = &shared_v[ltype][i];
            }

            constexpr int64_t kAttnTile = 64;
            std::vector<KvSrc> k_srcs(static_cast<size_t>(total_len));
            std::vector<KvSrc> v_srcs(static_cast<size_t>(total_len));
            std::vector<double> tile_scores(
                static_cast<size_t>(kAttnTile));
            std::vector<double> acc(static_cast<size_t>(hd));
            for (int64_t h = 0; h < cfg.num_attention_heads; ++h) {
                const int64_t kv_head = h / head_ratio;
                for (int64_t t = 0; t < total_len; ++t) {
                    if (t < span.position_offset) {
                        k_srcs[static_cast<size_t>(t)] =
                            kv_src(span.slot, true, kv_layer, t, kv_head);
                        v_srcs[static_cast<size_t>(t)] =
                            kv_src(span.slot, false, kv_layer, t, kv_head);
                    } else {
                        const int64_t s = t - span.position_offset;
                        k_srcs[static_cast<size_t>(t)].fp =
                            cur_k->data() +
                            static_cast<size_t>((kv_head * seq + s) * hd);
                        v_srcs[static_cast<size_t>(t)].fp =
                            cur_v->data() +
                            static_cast<size_t>((kv_head * seq + s) * hd);
                    }
                }
                const double* q_head =
                    q_heads.data() + static_cast<size_t>(h * seq * hd);
                for (int64_t s = 0; s < seq; ++s) {
                    const double* q_row = q_head + s * hd;
                    double* out = attn_flat.data() +
                        static_cast<size_t>((base + s) * q_dim + h * hd);
                    std::fill(acc.begin(), acc.end(), 0.0);
                    double m = -std::numeric_limits<double>::infinity();
                    double l = 0.0;
                    const int64_t last = span.position_offset + s;
                    const int64_t lo =
                        sliding
                            ? std::max<int64_t>(
                                  0, last - cfg.sliding_window + 1)
                            : 0;
                    for (int64_t t0 = lo; t0 <= last; t0 += kAttnTile) {
                        const int64_t tn =
                            std::min(kAttnTile, last - t0 + 1);
                        double tile_max =
                            -std::numeric_limits<double>::infinity();
                        for (int64_t j = 0; j < tn; ++j) {
                            const KvSrc& ksrc =
                                k_srcs[static_cast<size_t>(t0 + j)];
                            const double score =
                                (ksrc.q8 != nullptr
                                     ? dot_int8(q_row, ksrc.q8, hd) *
                                           ksrc.scale
                                     : dot_f64(q_row, ksrc.fp, hd)) *
                                scale;
                            tile_scores[static_cast<size_t>(j)] = score;
                            if (score > tile_max) tile_max = score;
                        }
                        const double m_new = std::max(m, tile_max);
                        const double rescale = std::exp(m - m_new);
                        if (rescale != 1.0) {
                            for (int64_t d = 0; d < hd; ++d) {
                                acc[static_cast<size_t>(d)] *= rescale;
                            }
                            l *= rescale;
                        }
                        for (int64_t j = 0; j < tn; ++j) {
                            const double w = std::exp(
                                tile_scores[static_cast<size_t>(j)] -
                                m_new);
                            l += w;
                            const KvSrc& vsrc =
                                v_srcs[static_cast<size_t>(t0 + j)];
                            if (vsrc.q8 != nullptr) {
                                axpy_int8(
                                    acc.data(), w * vsrc.scale,
                                    vsrc.q8, hd);
                            } else {
                                axpy_f64(acc.data(), w, vsrc.fp, hd);
                            }
                        }
                        m = m_new;
                    }
                    const double inv_l = 1.0 / l;
                    for (int64_t d = 0; d < hd; ++d) {
                        out[d] = acc[static_cast<size_t>(d)] * inv_l;
                    }
                }
            }
        }

        if (module_rms != nullptr) {
            module_rms->push_back(
                rope_q_count > 0
                    ? std::sqrt(rope_q_sumsq /
                                static_cast<double>(rope_q_count))
                    : 0.0);
        }
        std::vector<double> attn_out = linear(
            attn_flat, total_tokens, q_dim, layer.o_proj_t, hidden_size);
        if (module_rms != nullptr) {
            module_rms->push_back(
                hidden_rms(attn_out, total_tokens, hidden_size));
        }
        // post_attention_layernorm on the o_proj output, then residual.
        std::vector<double> attn_normed(attn_out.size());
        rmsnorm_ptr(
            attn_out.data(), total_tokens, hidden_size,
            layer.post_attn_norm.data, cfg.rms_norm_eps,
            attn_normed.data());
        axpy_f64(
            hidden.data(), 1.0, attn_normed.data(),
            static_cast<int64_t>(hidden.size()));

        normed = rmsnorm(
            hidden, total_tokens, hidden_size, layer.pre_ffn_norm,
            cfg.rms_norm_eps);
        if (module_rms != nullptr) {
            module_rms->push_back(
                hidden_rms(normed, total_tokens, hidden_size));
        }
        const int64_t inter =
            cfg.intermediate_size *
            (cfg.use_double_wide_mlp && layer.kv_shared ? 2 : 1);
        const bool gelu = cfg.hidden_act == "gelu_pytorch_tanh";
        std::vector<double> gate_up = linear(
            normed, total_tokens, hidden_size, layer.gate_up_t,
            2 * inter);
        std::vector<double> mlp_in(
            static_cast<size_t>(total_tokens * inter));
        for (int64_t r = 0; r < total_tokens; ++r) {
            const double* fused_row =
                gate_up.data() + static_cast<size_t>(r * 2 * inter);
            double* out_row =
                mlp_in.data() + static_cast<size_t>(r * inter);
            for (int64_t j = 0; j < inter; ++j) {
                const double g = fused_row[j];
                out_row[j] =
                    (gelu ? gelu_tanh_f(g)
                          : g / (1.0 + std::exp(-g))) *
                    fused_row[inter + j];
            }
        }
        std::vector<double> mlp_out = linear(
            mlp_in, total_tokens, inter, layer.down_proj_t, hidden_size);
        // post_feedforward_layernorm before the residual add.
        std::vector<double> ffn_normed(mlp_out.size());
        rmsnorm_ptr(
            mlp_out.data(), total_tokens, hidden_size,
            layer.post_ffn_norm.data, cfg.rms_norm_eps,
            ffn_normed.data());
        if (module_rms != nullptr) {
            module_rms->push_back(
                hidden_rms(ffn_normed, total_tokens, hidden_size));
        }
        axpy_f64(
            hidden.data(), 1.0, ffn_normed.data(),
            static_cast<int64_t>(hidden.size()));

        // PLE residual branch:
        //   x += ple_norm(ple_proj(gelu(ple_gate(x)) * ple_in[l]))
        if (ple > 0) {
            std::vector<double> residual = hidden;
            std::vector<double> g = linear(
                hidden, total_tokens, hidden_size, layer.ple_gate_t,
                ple);
            for (double& value : g) value = gelu_tanh_f(value);
            for (int64_t t = 0; t < total_tokens; ++t) {
                double* g_row = g.data() + static_cast<size_t>(t * ple);
                const double* pl = ple_in.data() +
                    static_cast<size_t>((t * num_layers + layer_idx) * ple);
                for (int64_t d = 0; d < ple; ++d) {
                    g_row[d] *= pl[d];
                }
            }
            std::vector<double> p = linear(
                g, total_tokens, ple, layer.ple_proj_t, hidden_size);
            std::vector<double> pn(p.size());
            rmsnorm_ptr(
                p.data(), total_tokens, hidden_size,
                layer.ple_post_norm.data, cfg.rms_norm_eps, pn.data());
            for (size_t i = 0; i < hidden.size(); ++i) {
                hidden[i] = residual[i] + pn[i];
            }
        }
        if (layer_rms != nullptr) {
            layer_rms->push_back(
                hidden_rms(hidden, total_tokens, hidden_size));
        }
    }

    for (const BatchSpan& span : spans) {
        if (span.append_cache) {
            kv_lens_[static_cast<size_t>(span.slot)] =
                span.position_offset +
                static_cast<int64_t>(span.ids->size());
        }
    }
    std::vector<double> normed = rmsnorm(
        hidden, total_tokens, hidden_size, final_norm_, cfg.rms_norm_eps);
    if (layer_rms != nullptr) {
        layer_rms->push_back(hidden_rms(normed, total_tokens, hidden_size));
    }
    if (module_rms != nullptr) {
        module_rms->push_back(hidden_rms(normed, total_tokens, hidden_size));
    }
    mem_note(total_tokens);
    return normed;
}

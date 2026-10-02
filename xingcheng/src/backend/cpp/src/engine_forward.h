// engine_forward.h — B94 fragment of engine.cpp (forward_batch_hidden).
// Included once by engine.cpp inside namespace xingcheng::inference.
#pragma once

std::vector<double> NativeInferenceEngine::forward_batch_hidden(
    const std::vector<BatchSpan>& spans,
    std::vector<double>* layer_rms,
    std::vector<double>* module_rms) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (spans.empty()) throw InferenceError("INPUT_EMPTY");
    // Gemma4 profile: hybrid per-layer-type attention plane.
    if (bundle_->config().is_gemma4()) {
        return forward_batch_hidden_gemma4(spans, layer_rms, module_rms);
    }
    const ModelConfig& cfg = bundle_->config();
    if (moe_trace_enabled_) ++moe_trace_.forwards;
    const int64_t hidden_size = cfg.hidden_size;
    std::vector<int64_t> starts(spans.size());
    std::vector<int64_t> vlens(spans.size());
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
        // Vision early-fusion prefix length (0 for text-only spans).
        const int64_t patches = span.vision_num_patches;
        if (patches < 0) throw InferenceError("VISION_SHAPE_MISMATCH");
        if (patches > 0) {
            if (!cfg.use_vision) throw InferenceError("VISION_NOT_ENABLED");
            if (span.vision_patches == nullptr ||
                static_cast<int64_t>(span.vision_patches->size()) !=
                    patches * cfg.vision_patch_dim)
                throw InferenceError("VISION_SHAPE_MISMATCH");
            if (cfg.vision_max_patches > 0 && patches > cfg.vision_max_patches)
                throw InferenceError("VISION_TOO_MANY_PATCHES");
        }
        const int64_t vlen = patches + seq;
        if (span.position_offset < 0 ||
            span.position_offset + vlen > cfg.max_position_embeddings) {
            throw InferenceError("SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS");
        }
        if (span.append_cache || span.position_offset > 0) {
            // Cached-KV access needs a live per-sequence namespace.
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
        vlens[i] = vlen;
        total_tokens += vlen;
    }

    std::vector<double>& hidden = fs_.hidden;
    hidden.resize(static_cast<size_t>(total_tokens * hidden_size));
    // Router trace accumulates across a request's decode steps; the
    // buffer is reset when tracing is (re)enabled via set_router_trace.
    for (size_t i = 0; i < spans.size(); ++i) {
        const BatchSpan& span = spans[i];
        const int64_t seq = static_cast<int64_t>(span.ids->size());
        const int64_t patches = span.vision_num_patches;
        const int64_t base = starts[i];
        if (patches > 0) {
            // Early fusion: linear patch projection occupies rows
            // [base, base+patches); text rows shift down by patches.
            std::vector<double>& prefix = fs_.vision_prefix;
            linear_into(
                *span.vision_patches, patches, cfg.vision_patch_dim,
                vision_patch_proj_t_, hidden_size, prefix);
            std::copy_n(
                prefix.data(),
                static_cast<size_t>(patches * hidden_size),
                hidden.data() + static_cast<size_t>(base * hidden_size));
        }
        for (int64_t s = 0; s < seq; ++s) {
            double* dst = hidden.data() +
                static_cast<size_t>((base + patches + s) * hidden_size);
            if (span.embed_override != nullptr) {
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
                embedding_.data + token * hidden_size,
                hidden_size, dst);
            if (cfg.position_embedding_type == "learned") {
                const TensorView& pos = bundle_->tensor("model.embeddings.position_embeddings.weight");
                const int64_t position = span.position_offset + patches + s;
                for (int64_t d = 0; d < hidden_size; ++d) {
                    hidden[static_cast<size_t>((base + patches + s) * hidden_size + d)] +=
                        pos.data[position * hidden_size + d];
                }
            }
        }
    }
    if (layer_rms != nullptr) {
        layer_rms->push_back(hidden_rms(hidden, total_tokens, hidden_size));
    }
    if (module_rms != nullptr) {
        module_rms->push_back(hidden_rms(hidden, total_tokens, hidden_size));
    }

    const int64_t q_dim = cfg.num_attention_heads * cfg.head_dim;
    const int64_t kv_dim = cfg.num_key_value_heads * cfg.head_dim;
    const int64_t q_mul = cfg.attn_output_gate ? 2 : 1;
    // Fused-hybrid DeltaNet geometry (unused on dense bundles).
    const int64_t lin_kh = cfg.linear_num_key_heads;
    const int64_t lin_kd = cfg.linear_key_head_dim;
    const int64_t lin_vh = cfg.linear_num_value_heads;
    const int64_t lin_vd = cfg.linear_value_head_dim;
    const int64_t lin_ratio = lin_kh > 0 ? lin_vh / lin_kh : 0;
    const int64_t lin_key_dim = lin_kh * lin_kd;
    const int64_t lin_val_dim = lin_vh * lin_vd;
    const int64_t lin_conv_dim = 2 * lin_key_dim + lin_val_dim;
    const int64_t lin_group_sz = 2 * lin_kd + lin_vd * lin_ratio;
    const int64_t lin_kernel = cfg.linear_conv_kernel_dim;
    const int64_t lin_fused_cols =
        lin_kh * lin_group_sz + lin_val_dim + 2 * lin_vh;
    const int64_t rotary_dim = cfg.rotary_dim();
    const bool fused_contract = cfg.fused_rope_contract();
    // Qwen3-Coder YaRN (v29): when configured, inv-freq bases are
    // per-channel blended (raw vs factor-interpolated) and the cos/sin
    // operands gain the attention-factor mscale — matching the
    // trainer's rope_cs.
    const bool yarn = cfg.use_yarn();
    const double yarn_ms =
        yarn ? yarn_mscale_d(cfg.yarn_factor, cfg.yarn_attention_factor)
             : 1.0;
    // Per-span RoPE tables (each sequence carries its own position offset);
    // the frequency bases are model-constant and cached once per engine.
    std::vector<std::vector<double>> rope_cos(spans.size());
    std::vector<std::vector<double>> rope_sin(spans.size());
    if (cfg.position_embedding_type == "rope" && !fused_contract) {
        const std::vector<double>& bases = rope_bases(
            cfg.head_dim, cfg.rope_theta,
            rope_base_, rope_base_dim_, rope_base_theta_);
        std::vector<double> yarn_adj;
        const std::vector<double>* eff_bases = &bases;
        if (yarn) {
            yarn_adj = yarn_adjust_bases(
                bases, cfg.head_dim, cfg.rope_theta, cfg.yarn_factor,
                cfg.yarn_original_max_position_embeddings,
                cfg.yarn_beta_fast, cfg.yarn_beta_slow);
            eff_bases = &yarn_adj;
        }
        for (size_t i = 0; i < spans.size(); ++i) {
            rope_tables(
                vlens[i],
                spans[i].position_offset, cfg.head_dim, *eff_bases,
                rope_cos[i], rope_sin[i]);
            for (double& x : rope_cos[i]) x *= yarn_ms;
            for (double& x : rope_sin[i]) x *= yarn_ms;
        }
    }
    // Fused-contract rope: partial rotary uses bases over rotary_dim,
    // full rotary (rd == head_dim) uses the interleaved pairing over
    // head_dim — both exactly as the trainer computes them.
    const std::vector<double>* fused_bases = nullptr;
    std::vector<double> fused_yarn_bases;
    if (fused_contract && cfg.position_embedding_type == "rope") {
        const int64_t base_dim =
            rotary_dim < cfg.head_dim ? rotary_dim : cfg.head_dim;
        fused_bases = &rope_bases(
            base_dim, cfg.rope_theta,
            rope_base_, rope_base_dim_, rope_base_theta_);
        if (yarn) {
            fused_yarn_bases = yarn_adjust_bases(
                *fused_bases, base_dim, cfg.rope_theta, cfg.yarn_factor,
                cfg.yarn_original_max_position_embeddings,
                cfg.yarn_beta_fast, cfg.yarn_beta_slow);
            fused_bases = &fused_yarn_bases;
        }
    }

    for (int64_t layer_idx = 0; layer_idx < cfg.num_hidden_layers; ++layer_idx) {
        LayerWeights& layer = layers_[static_cast<size_t>(layer_idx)];
        std::vector<double>& normed = fs_.normed;
        rmsnorm_into(
            hidden, total_tokens, hidden_size, layer.input_norm,
            cfg.rms_norm_eps, normed);
        if (module_rms != nullptr) {
            module_rms->push_back(
                hidden_rms(normed, total_tokens, hidden_size));
        }
        // RoPE probe accumulator: RMS of post-RoPE queries across all spans
        // (post-projection queries when the config is not rope; on DeltaNet
        // layers the post-normalization query stream plays that role).
        double rope_q_sumsq = 0.0;
        int64_t rope_q_count = 0;
        std::vector<double>& attn_out = fs_.attn_out;
        if (layer.is_linear) {
        // ── v27 gated DeltaNet (linear attention) ────────────────────
        // One fused GEMM produces [qkvz|z|a|b] per packed row; the causal
        // depthwise conv + per-value-head delta-rule scan then runs per
        // sequence over the slot's persistent recurrent state.
        {
            std::vector<double>& fused = fs_.lin_fused;
            std::vector<double>& qkvz = fs_.lin_qkvz;
            std::vector<double>& lz = fs_.lin_z;
            std::vector<double>& la = fs_.lin_a;
            std::vector<double>& lb = fs_.lin_b;
            linear_into(
                normed, total_tokens, hidden_size, layer.lin_fused_t,
                lin_fused_cols, fused);
            split_columns(
                fused, total_tokens,
                {{lin_kh * lin_group_sz, &qkvz},
                 {lin_val_dim, &lz}, {lin_vh, &la}, {lin_vh, &lb}});
            std::vector<double>& conv_in = fs_.lin_conv_in;
            std::vector<double>& conv_pad = fs_.lin_conv_pad;
            std::vector<double>& conv_out = fs_.lin_conv_out;
            std::vector<double>& lq = fs_.lin_qn;
            std::vector<double>& lk = fs_.lin_kn;
            std::vector<double>& lv = fs_.lin_v;
            std::vector<double>& lo = fs_.lin_o;
            std::vector<double>& lon = fs_.lin_on;
            std::vector<double>& decay = fs_.lin_decay;
            std::vector<double>& beta = fs_.lin_beta;
            conv_in.resize(static_cast<size_t>(total_tokens * lin_conv_dim));
            lq.resize(static_cast<size_t>(total_tokens * lin_vh * lin_kd));
            lk.resize(static_cast<size_t>(total_tokens * lin_vh * lin_kd));
            lv.resize(static_cast<size_t>(total_tokens * lin_val_dim));
            lo.resize(static_cast<size_t>(total_tokens * lin_val_dim));
            lon.resize(static_cast<size_t>(total_tokens * lin_val_dim));
            decay.resize(static_cast<size_t>(total_tokens * lin_vh));
            beta.resize(static_cast<size_t>(total_tokens * lin_vh));
            const int64_t s_sz = lin_kd * lin_vd;
            const double* a_log = layer.lin_a_log.data;
            const double* dt_bias = layer.lin_dt_bias.data;
            const double* conv_w = layer.lin_conv1d.data;
            const double* out_norm = layer.lin_out_norm.data;
            std::vector<double>& kvm = fs_.qk_tmp;
            std::vector<double> u(static_cast<size_t>(lin_vd));
            const double qscale =
                1.0 / std::sqrt(static_cast<double>(lin_kd));
            for (size_t i = 0; i < spans.size(); ++i) {
                const BatchSpan& span = spans[i];
                const int64_t seq = vlens[i];
                const int64_t base = starts[i];
                // State resolution: appended spans read+write the slot's
                // persistent state; probe spans (append_cache=false) run
                // on a local scratch state — fresh at offset 0, a copy of
                // the slot's saved state when resuming mid-sequence.
                LinLayerState local;
                LinLayerState* st;
                if (span.append_cache) {
                    st = &lin_states_[static_cast<size_t>(span.slot)]
                                     [static_cast<size_t>(layer_idx)];
                    if (span.position_offset == 0) {
                        st->tokens = 0;
                    } else if (st->tokens != span.position_offset) {
                        throw InferenceError("LIN_STATE_MISMATCH");
                    }
                } else if (span.position_offset == 0) {
                    st = &local;
                } else {
                    const LinLayerState& saved =
                        lin_states_[static_cast<size_t>(span.slot)]
                                   [static_cast<size_t>(layer_idx)];
                    if (saved.tokens != span.position_offset) {
                        throw InferenceError("LIN_STATE_MISMATCH");
                    }
                    local = saved;
                    st = &local;
                }
                if (st->conv_tail.empty()) {
                    st->conv_tail.assign(static_cast<size_t>(
                        (lin_kernel - 1) * lin_conv_dim), 0.0);
                    st->s.assign(
                        static_cast<size_t>(lin_vh * s_sz), 0.0);
                }
                // Unpack per-key-head [q|k|v-group] rows into the flat
                // [q_flat|k_flat|v_flat] conv layout (HF in_proj_qkvz).
                for (int64_t s = 0; s < seq; ++s) {
                    const double* src = qkvz.data() + static_cast<size_t>(
                        (base + s) * lin_kh * lin_group_sz);
                    double* dst = conv_in.data() +
                        static_cast<size_t>((base + s) * lin_conv_dim);
                    for (int64_t g = 0; g < lin_kh; ++g) {
                        const double* gr = src + g * lin_group_sz;
                        std::copy_n(gr, lin_kd, dst + g * lin_kd);
                        std::copy_n(gr + lin_kd, lin_kd,
                                    dst + lin_key_dim + g * lin_kd);
                        std::copy_n(gr + 2 * lin_kd, lin_vd * lin_ratio,
                                    dst + 2 * lin_key_dim +
                                        g * lin_vd * lin_ratio);
                    }
                }
                // Causal depthwise conv + SiLU: the slot's last kernel-1
                // raw rows supply the left context for continuation spans.
                conv_pad.resize(static_cast<size_t>(
                    (lin_kernel - 1 + seq) * lin_conv_dim));
                std::copy(st->conv_tail.begin(), st->conv_tail.end(),
                          conv_pad.begin());
                for (int64_t s = 0; s < seq; ++s) {
                    std::copy_n(
                        conv_in.data() +
                            static_cast<size_t>((base + s) * lin_conv_dim),
                        lin_conv_dim,
                        conv_pad.data() + static_cast<size_t>(
                            (lin_kernel - 1 + s) * lin_conv_dim));
                }
                conv_out.resize(static_cast<size_t>(seq * lin_conv_dim));
                for (int64_t s = 0; s < seq; ++s) {
                    double* out_row = conv_out.data() +
                        static_cast<size_t>(s * lin_conv_dim);
                    const double* pad_row = conv_pad.data() +
                        static_cast<size_t>((lin_kernel - 1 + s) * lin_conv_dim);
                    for (int64_t c = 0; c < lin_conv_dim; ++c) {
                        double acc = 0.0;
                        for (int64_t j = 0; j < lin_kernel; ++j) {
                            acc += conv_w[c * lin_kernel + j] *
                                   pad_row[static_cast<size_t>(
                                       c - j * lin_conv_dim)];
                        }
                        out_row[c] = silu_d(acc);
                    }
                }
                // Expand q/k across the value-head group, l2-normalize,
                // scale q by 1/sqrt(kd) — trainer order, row-wise.
                for (int64_t s = 0; s < seq; ++s) {
                    const int64_t row = base + s;
                    const double* cr = conv_out.data() +
                        static_cast<size_t>(s * lin_conv_dim);
                    for (int64_t g = 0; g < lin_kh; ++g) {
                        for (int64_t r = 0; r < lin_ratio; ++r) {
                            const int64_t h = g * lin_ratio + r;
                            std::copy_n(
                                cr + g * lin_kd, lin_kd,
                                lq.data() + static_cast<size_t>(
                                    (row * lin_vh + h) * lin_kd));
                            std::copy_n(
                                cr + lin_key_dim + g * lin_kd, lin_kd,
                                lk.data() + static_cast<size_t>(
                                    (row * lin_vh + h) * lin_kd));
                        }
                    }
                    std::copy_n(
                        cr + 2 * lin_key_dim, lin_val_dim,
                        lv.data() + static_cast<size_t>(
                            row * lin_val_dim));
                }
                for (int64_t s = 0; s < seq; ++s) {
                    const int64_t row = base + s;
                    for (int64_t h = 0; h < lin_vh; ++h) {
                        const double ar =
                            la[static_cast<size_t>(row * lin_vh + h)] +
                            dt_bias[h];
                        decay[static_cast<size_t>(row * lin_vh + h)] =
                            std::exp(-std::exp(a_log[h]) * softplus_d(ar));
                        beta[static_cast<size_t>(row * lin_vh + h)] =
                            sigmoid_d(
                                lb[static_cast<size_t>(row * lin_vh + h)]);
                    }
                }
                // Per-span l2-normalize + q scale (order matches trainer).
                l2norm_span_rows(
                    lq, base, seq, lin_vh, lin_kd, 1e-6);
                l2norm_span_rows(
                    lk, base, seq, lin_vh, lin_kd, 1e-6);
                for (int64_t s = 0; s < seq; ++s) {
                    double* qr = lq.data() + static_cast<size_t>(
                        ((base + s) * lin_vh) * lin_kd);
                    for (int64_t e = 0; e < lin_vh * lin_kd; ++e) {
                        qr[e] *= qscale;
                    }
                }
                // Delta-rule recurrence (fp64 twin of the trainer scan):
                //   S' = dec·S; u = β·(v − S'ᵀk); S' += k⊗u; o = qᵀS'.
                kvm.resize(static_cast<size_t>(lin_vd));
                for (int64_t h = 0; h < lin_vh; ++h) {
                    double* S = st->s.data() +
                        static_cast<size_t>(h * s_sz);
                    for (int64_t s = 0; s < seq; ++s) {
                        const int64_t row = base + s;
                        const double* kr = lk.data() + static_cast<size_t>(
                            (row * lin_vh + h) * lin_kd);
                        const double* vr = lv.data() + static_cast<size_t>(
                            (row * lin_vh + h) * lin_vd);
                        const double* qr = lq.data() + static_cast<size_t>(
                            (row * lin_vh + h) * lin_kd);
                        const double dec = decay[static_cast<size_t>(
                            row * lin_vh + h)];
                        const double bt = beta[static_cast<size_t>(
                            row * lin_vh + h)];
                        for (int64_t e = 0; e < s_sz; ++e) S[e] *= dec;
                        std::fill(kvm.begin(), kvm.end(), 0.0);
                        for (int64_t d = 0; d < lin_kd; ++d) {
                            axpy_f64(kvm.data(), kr[d],
                                     S + static_cast<size_t>(d * lin_vd),
                                     lin_vd);
                        }
                        for (int64_t j = 0; j < lin_vd; ++j) {
                            u[static_cast<size_t>(j)] =
                                (vr[j] - kvm[static_cast<size_t>(j)]) * bt;
                        }
                        for (int64_t d = 0; d < lin_kd; ++d) {
                            axpy_f64(S + static_cast<size_t>(d * lin_vd),
                                     kr[d], u.data(), lin_vd);
                        }
                        double* orow = lo.data() + static_cast<size_t>(
                            (row * lin_vh + h) * lin_vd);
                        std::fill(orow, orow + lin_vd, 0.0);
                        for (int64_t d = 0; d < lin_kd; ++d) {
                            axpy_f64(orow, qr[d],
                                     S + static_cast<size_t>(d * lin_vd),
                                     lin_vd);
                        }
                    }
                }
                // Persist the rolling conv window + folded position count
                // for the next span of this sequence.
                if (span.append_cache) {
                    const size_t tail = st->conv_tail.size();
                    std::copy(
                        conv_pad.end() - static_cast<std::ptrdiff_t>(tail),
                        conv_pad.end(), st->conv_tail.begin());
                    st->tokens = span.position_offset + seq;
                }
            }
            // Gated RMSNorm per value head, then SiLU(z) gate — the
            // (t,h) row decomposition matches the trainer exactly.
            for (int64_t s = 0; s < total_tokens; ++s) {
                for (int64_t h = 0; h < lin_vh; ++h) {
                    const double* orow = lo.data() + static_cast<size_t>(
                        (s * lin_vh + h) * lin_vd);
                    double* on = lon.data() + static_cast<size_t>(
                        (s * lin_vh + h) * lin_vd);
                    double ss = 0.0;
                    for (int64_t j = 0; j < lin_vd; ++j) {
                        ss += orow[j] * orow[j];
                    }
                    const double inv = 1.0 / std::sqrt(
                        ss / static_cast<double>(lin_vd) +
                        cfg.rms_norm_eps);
                    const double* zr = lz.data() + static_cast<size_t>(
                        (s * lin_vh + h) * lin_vd);
                    for (int64_t j = 0; j < lin_vd; ++j) {
                        on[j] = orow[j] * inv * out_norm[j] * silu_d(zr[j]);
                    }
                }
            }
            if (module_rms != nullptr) {
                for (double v : lq) {
                    rope_q_sumsq += v * v;
                }
                rope_q_count += static_cast<int64_t>(lq.size());
            }
            linear_into(
                lon, total_tokens, lin_val_dim, layer.lin_out_proj_t,
                hidden_size, attn_out);
        }
        } else {
        // ── full attention (dense + v27 gated hybrid layers) ─────────
        {
        // Projections and FFN are token-wise: the packed rows of all spans
        // share one GEMM — that sharing is the R9 throughput win.
        std::vector<double>& q_flat = fs_.q_flat;
        std::vector<double>& k_flat = fs_.k_flat;
        std::vector<double>& v_flat = fs_.v_flat;
        std::vector<double>& gate_flat = fs_.attn_gate;
        linear_into(
            normed, total_tokens, hidden_size, layer.qkv_t,
            q_mul * q_dim + 2 * kv_dim, fs_.qkv);
        if (cfg.attn_output_gate) {
            // q|gate fused rows: per head the q block emits
            // [q head_dim | gate head_dim] — de-interleave into separate
            // flat streams before head-major unpack.
            std::vector<double>& qg = fs_.lin_qkvz;
            split_columns(
                fs_.qkv, total_tokens,
                {{q_mul * q_dim, &qg},
                 {kv_dim, &k_flat}, {kv_dim, &v_flat}});
            q_flat.resize(static_cast<size_t>(total_tokens * q_dim));
            gate_flat.resize(static_cast<size_t>(total_tokens * q_dim));
            for (int64_t s = 0; s < total_tokens; ++s) {
                for (int64_t h = 0; h < cfg.num_attention_heads; ++h) {
                    const double* src = qg.data() + static_cast<size_t>(
                        (s * cfg.num_attention_heads + h) * 2 * cfg.head_dim);
                    std::copy_n(
                        src, cfg.head_dim,
                        q_flat.data() + static_cast<size_t>(
                            (s * cfg.num_attention_heads + h) *
                            cfg.head_dim));
                    std::copy_n(
                        src + cfg.head_dim, cfg.head_dim,
                        gate_flat.data() + static_cast<size_t>(
                            (s * cfg.num_attention_heads + h) *
                            cfg.head_dim));
                }
            }
        } else {
            split_columns(
                fs_.qkv, total_tokens,
                {{q_dim, &q_flat}, {kv_dim, &k_flat}, {kv_dim, &v_flat}});
        }

        std::vector<double>& attn_flat = fs_.attn_flat;
        attn_flat.resize(static_cast<size_t>(total_tokens * q_dim));
        const int64_t head_ratio = cfg.num_attention_heads / cfg.num_key_value_heads;
        const double scale = 1.0 / std::sqrt(static_cast<double>(cfg.head_dim));
        // Attention is the only span-aware stage: each sequence attends
        // exclusively to its own KV namespace (fresh projections + its own
        // cached prefix); no cross-sequence leakage is possible.
        for (size_t i = 0; i < spans.size(); ++i) {
            const BatchSpan& span = spans[i];
            // Fused length: vision prefix rows + text rows (early fusion —
            // downstream stages see one causal sequence per span).
            const int64_t seq = vlens[i];
            const int64_t base = starts[i];
            const int64_t total_len = span.position_offset + seq;

            std::vector<double>& q_heads = fs_.q_heads;
            std::vector<double>& k_heads = fs_.k_heads;
            std::vector<double>& v_heads = fs_.v_heads;
            q_heads.resize(static_cast<size_t>(cfg.num_attention_heads * seq * cfg.head_dim));
            k_heads.resize(static_cast<size_t>(cfg.num_key_value_heads * seq * cfg.head_dim));
            v_heads.resize(static_cast<size_t>(cfg.num_key_value_heads * seq * cfg.head_dim));
            for (int64_t s = 0; s < seq; ++s) {
                for (int64_t h = 0; h < cfg.num_attention_heads; ++h) {
                    std::copy_n(
                        q_flat.data() + static_cast<size_t>((base + s) * q_dim + h * cfg.head_dim),
                        cfg.head_dim,
                        q_heads.data() + static_cast<size_t>((h * seq + s) * cfg.head_dim));
                }
                for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                    std::copy_n(
                        k_flat.data() + static_cast<size_t>((base + s) * kv_dim + h * cfg.head_dim),
                        cfg.head_dim,
                        k_heads.data() + static_cast<size_t>((h * seq + s) * cfg.head_dim));
                    std::copy_n(
                        v_flat.data() + static_cast<size_t>((base + s) * kv_dim + h * cfg.head_dim),
                        cfg.head_dim,
                        v_heads.data() + static_cast<size_t>((h * seq + s) * cfg.head_dim));
                }
            }

            // v27 QK-norm: per-head RMSNorm on q/k pre-RoPE (trainer
            // order — norm first, then rotary).
            if (cfg.qk_norm) {
                std::vector<double>& qn = fs_.qk_tmp;
                rmsnorm_into(
                    q_heads, cfg.num_attention_heads * seq, cfg.head_dim,
                    layer.q_norm, cfg.rms_norm_eps, qn);
                q_heads.swap(qn);
                rmsnorm_into(
                    k_heads, cfg.num_key_value_heads * seq, cfg.head_dim,
                    layer.k_norm, cfg.rms_norm_eps, qn);
                k_heads.swap(qn);
            }

            std::vector<double>* q_attn = &q_heads;
            std::vector<double>* k_attn = &k_heads;
            if (cfg.position_embedding_type == "rope") {
                if (fused_contract) {
                    // v27 trainer pairing (xct_math.h): partial rotary is
                    // rotate-half over the first rd channels; rd == hd
                    // selects the interleaved (2i,2i+1) pairing. Both run
                    // in-place — unlike the legacy rotate-half C kernel.
                    if (rotary_dim < cfg.head_dim) {
                        rope_partial_rows(
                            q_heads.data(), cfg.num_attention_heads, seq,
                            cfg.head_dim, rotary_dim, span.position_offset,
                            *fused_bases, yarn_ms);
                        rope_partial_rows(
                            k_heads.data(), cfg.num_key_value_heads, seq,
                            cfg.head_dim, rotary_dim, span.position_offset,
                            *fused_bases, yarn_ms);
                    } else {
                        rope_interleaved_rows(
                            q_heads.data(), cfg.num_attention_heads, seq,
                            cfg.head_dim, span.position_offset,
                            *fused_bases, yarn_ms);
                        rope_interleaved_rows(
                            k_heads.data(), cfg.num_key_value_heads, seq,
                            cfg.head_dim, span.position_offset,
                            *fused_bases, yarn_ms);
                    }
                } else {
                std::vector<double>& q_rope = fs_.q_rope;
                std::vector<double>& k_rope = fs_.k_rope;
                q_rope.resize(q_heads.size());
                k_rope.resize(k_heads.size());
                checked_c_call(
                    gptbridge_native_transformer_rope(
                        q_heads.data(), 1, cfg.num_attention_heads, seq, cfg.head_dim,
                        rope_cos[i].data(), rope_sin[i].data(), q_rope.data()),
                    "rope-q");
                checked_c_call(
                    gptbridge_native_transformer_rope(
                        k_heads.data(), 1, cfg.num_key_value_heads, seq, cfg.head_dim,
                        rope_cos[i].data(), rope_sin[i].data(), k_rope.data()),
                    "rope-k");
                q_attn = &q_rope;
                k_attn = &k_rope;
                }
            }
            if (module_rms != nullptr) {
                for (double value : *q_attn) rope_q_sumsq += value * value;
                rope_q_count += static_cast<int64_t>(q_attn->size());
            }

            if (span.append_cache) {
                for (int64_t s = 0; s < seq; ++s) {
                    const int64_t position = span.position_offset + s;
                    kv_ensure_position(span.slot, position);
                    for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                        kv_write(
                            span.slot, true, layer_idx, position, h,
                            k_attn->data() + static_cast<size_t>((h * seq + s) * cfg.head_dim));
                        kv_write(
                            span.slot, false, layer_idx, position, h,
                            v_heads.data() + static_cast<size_t>((h * seq + s) * cfg.head_dim));
                    }
                }
            }

            // P1-1② device-resident KV: when the governed opt-in is active
            // and this span is the single-sequence slot, the current-step
            // K/V rows are mirrored on-device and attention runs entirely
            // in the CUDA online-softmax kernel (same causal bound and
            // fp64 semantics as the host path below). Other slots keep the
            // host loop.
            bool device_attn_done = false;
#if defined(XINGCHENG_CUDA)
            if (kv_device_active_ && span.slot == 0) {
                // Device buffers are indexed by the dense full-attention
                // ordinal (kv_layer_ord_), matching xcuda_kv_alloc and the
                // host pool — the global layer index is out of bounds on
                // every hybrid bundle (DeltaNet layers own no KV).
                const int64_t kv_ord =
                    kv_layer_ord_[static_cast<size_t>(layer_idx)];
                for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                    if (xcuda_kv_write_rows(
                            1, kv_ord, h, span.position_offset, seq,
                            k_attn->data() +
                                static_cast<size_t>(h * seq * cfg.head_dim)) != 0 ||
                        xcuda_kv_write_rows(
                            0, kv_ord, h, span.position_offset, seq,
                            v_heads.data() +
                                static_cast<size_t>(h * seq * cfg.head_dim)) != 0) {
                        throw InferenceError("CUDA_KV_WRITE_FAILED");
                    }
                }
                if (xcuda_kv_attention(
                        kv_ord, q_attn->data(), cfg.num_attention_heads,
                        seq, cfg.num_key_value_heads, cfg.head_dim,
                        span.position_offset,
                        attn_flat.data() + static_cast<size_t>(base * q_dim),
                        q_dim) != 0) {
                    throw InferenceError("CUDA_ATTENTION_FAILED");
                }
                device_attn_done = true;
            }
#endif

            // W1 residual: online/blocked attention — K/V streamed in tiles
            // with a running max/sum/accumulator (FlashAttention-style online
            // softmax); the seq x total_len scores matrix is never
            // materialized, so per-query state stays O(head_dim) instead of
            // O(total_len). Masked positions are skipped via the causal bound
            // — identical semantics to the former -inf mask + softmax pass.
            // KV INT8: cached entries carry packed int8 + per-head scale;
            // current-step sources stay fp64 (KvSrc.fp).
            constexpr int64_t kAttnTile = 64;
            if (!device_attn_done) {
            std::vector<KvSrc>& k_srcs = fs_.k_srcs;
            std::vector<KvSrc>& v_srcs = fs_.v_srcs;
            std::vector<double>& tile_scores = fs_.tile_scores;
            std::vector<double>& acc = fs_.acc;
            k_srcs.resize(static_cast<size_t>(total_len));
            v_srcs.resize(static_cast<size_t>(total_len));
            tile_scores.resize(static_cast<size_t>(kAttnTile));
            acc.resize(static_cast<size_t>(cfg.head_dim));
            int64_t srcs_kv_head = -1;
            for (int64_t h = 0; h < cfg.num_attention_heads; ++h) {
                const int64_t kv_head = h / head_ratio;
                // GQA sharing: the source views depend only on kv_head —
                // heads that share a kv_head rebuild identical tables, so
                // the build runs once per distinct kv_head.
                if (kv_head != srcs_kv_head) {
                    srcs_kv_head = kv_head;
                    for (int64_t t = 0; t < total_len; ++t) {
                        if (t < span.position_offset) {
                            k_srcs[static_cast<size_t>(t)] =
                                kv_src(span.slot, true, layer_idx, t, kv_head);
                            v_srcs[static_cast<size_t>(t)] =
                                kv_src(span.slot, false, layer_idx, t, kv_head);
                        } else {
                            const int64_t s = t - span.position_offset;
                            // Whole-struct assign: the scratch vectors keep
                            // their elements across calls/layers, so a bare
                            // .fp write would leak a stale .q8 (from an
                            // earlier int8 restore) into the current-step
                            // rows and silently re-route the dot through
                            // dot_int8 on a dangling pool pointer.
                            KvSrc kcur; KvSrc vcur;
                            kcur.fp =
                                k_attn->data() + static_cast<size_t>((kv_head * seq + s) * cfg.head_dim);
                            vcur.fp =
                                v_heads.data() + static_cast<size_t>((kv_head * seq + s) * cfg.head_dim);
                            k_srcs[static_cast<size_t>(t)] = kcur;
                            v_srcs[static_cast<size_t>(t)] = vcur;
                        }
                    }
                }
                const double* q_head =
                    q_attn->data() + static_cast<size_t>(h * seq * cfg.head_dim);
                for (int64_t s = 0; s < seq; ++s) {
                    const double* q_row = q_head + s * cfg.head_dim;
                    double* out = attn_flat.data() +
                        static_cast<size_t>((base + s) * q_dim + h * cfg.head_dim);
                    std::fill(acc.begin(), acc.end(), 0.0);
                    double m = -std::numeric_limits<double>::infinity();
                    double l = 0.0;
                    const int64_t last = span.position_offset + s;  // inclusive
                    for (int64_t t0 = 0; t0 <= last; t0 += kAttnTile) {
                        const int64_t tn = std::min(kAttnTile, last - t0 + 1);
                        double tile_max = -std::numeric_limits<double>::infinity();
                        for (int64_t j = 0; j < tn; ++j) {
                            const KvSrc& ksrc = k_srcs[static_cast<size_t>(t0 + j)];
                            const double score =
                                (ksrc.q8 != nullptr
                                     ? dot_int8(q_row, ksrc.q8, cfg.head_dim) *
                                           ksrc.scale
                                     : dot_f64(q_row, ksrc.fp, cfg.head_dim)) *
                                scale;
                            tile_scores[static_cast<size_t>(j)] = score;
                            if (score > tile_max) tile_max = score;
                        }
                        const double m_new = std::max(m, tile_max);
                        const double rescale = std::exp(m - m_new);
                        if (rescale != 1.0) {
                            for (int64_t d = 0; d < cfg.head_dim; ++d)
                                acc[static_cast<size_t>(d)] *= rescale;
                            l *= rescale;
                        }
                        for (int64_t j = 0; j < tn; ++j) {
                            const double w = std::exp(
                                tile_scores[static_cast<size_t>(j)] - m_new);
                            l += w;
                            const KvSrc& vsrc = v_srcs[static_cast<size_t>(t0 + j)];
                            if (vsrc.q8 != nullptr) {
                                axpy_int8(
                                    acc.data(), w * vsrc.scale, vsrc.q8,
                                    cfg.head_dim);
                            } else {
                                axpy_f64(acc.data(), w, vsrc.fp, cfg.head_dim);
                            }
                        }
                        m = m_new;
                    }
                    const double inv_l = 1.0 / l;
                    for (int64_t d = 0; d < cfg.head_dim; ++d) {
                        out[d] = acc[static_cast<size_t>(d)] * inv_l;
                    }
                }
            }
            }
        }

        // v27 attention output gate: sigmoid gate on the pre-o_proj
        // stream (trainer attn_gated = attn_out * sigmoid(gate)).
        if (cfg.attn_output_gate) {
            for (int64_t e = 0; e < total_tokens * q_dim; ++e) {
                attn_flat[static_cast<size_t>(e)] *=
                    sigmoid_d(gate_flat[static_cast<size_t>(e)]);
            }
        }
        linear_into(
            attn_flat, total_tokens, q_dim, layer.o_proj_t,
            cfg.hidden_size, attn_out);
        }
        }

        if (module_rms != nullptr) {
            module_rms->push_back(
                rope_q_count > 0
                    ? std::sqrt(
                          rope_q_sumsq / static_cast<double>(rope_q_count))
                    : 0.0);
            module_rms->push_back(
                hidden_rms(attn_out, total_tokens, hidden_size));
        }
        axpy_f64(
            hidden.data(), 1.0, attn_out.data(),
            static_cast<int64_t>(hidden.size()));

        rmsnorm_into(
            hidden, total_tokens, hidden_size, layer.post_norm,
            cfg.rms_norm_eps, normed);
        if (module_rms != nullptr) {
            module_rms->push_back(
                hidden_rms(normed, total_tokens, hidden_size));
        }
        if (layer.is_moe) {
            // R5 grouped sparse MoE (token-choice, mirrors modules/moe.py):
            // router softmax over experts → deterministic top-k → weights
            // renormalized inside top-k → per-expert gather / SwiGLU GEMM /
            // weighted scatter-add. Positions index the packed rows, so
            // expert groups naturally span all sequences in the batch.
            const int64_t experts = cfg.moe_num_experts;
            const int64_t top_k = cfg.moe_top_k;
            // v26 fine-grained experts: the routed/shared MLP inner width
            // may differ from the dense FFN's intermediate_size.
            const int64_t expert_inter =
                cfg.moe_expert_intermediate_size > 0
                    ? cfg.moe_expert_intermediate_size
                    : cfg.intermediate_size;
            const int64_t shared_inter =
                cfg.moe_shared_intermediate_size > 0
                    ? cfg.moe_shared_intermediate_size
                    : expert_inter;
            std::vector<double>& probs = fs_.moe_probs;
            linear_into(
                normed, total_tokens, hidden_size, layer.router_t,
                experts, probs);
            std::vector<int64_t>& top_idx = fs_.moe_top_idx;
            std::vector<double>& top_w = fs_.moe_top_w;
            std::vector<int64_t>& order = fs_.moe_order;
            top_idx.resize(static_cast<size_t>(total_tokens * top_k));
            top_w.resize(static_cast<size_t>(total_tokens * top_k));
            order.resize(static_cast<size_t>(experts));
            for (int64_t s = 0; s < total_tokens; ++s) {
                double* row = probs.data() + static_cast<size_t>(s * experts);
                if (cfg.moe_router_sigmoid) {
                    // v28 fused router (Qwen3.5): per-expert logistic
                    // score instead of a shared softmax denominator —
                    // mirrors xct_math.h; deterministic top-k + renorm
                    // below are unchanged.
                    for (int64_t e = 0; e < experts; ++e)
                        row[e] = 1.0 / (1.0 + std::exp(-row[e]));
                } else {
                    const double mx = *std::max_element(row, row + experts);
                    double total = 0.0;
                    for (int64_t e = 0; e < experts; ++e) {
                        row[e] = std::exp(row[e] - mx);
                        total += row[e];
                    }
                    for (int64_t e = 0; e < experts; ++e) row[e] /= total;
                }
                std::iota(order.begin(), order.end(), 0);
                std::stable_sort(
                    order.begin(), order.end(),
                    [&](int64_t a, int64_t b) { return row[a] > row[b]; });
                double selected = 0.0;
                for (int64_t k = 0; k < top_k; ++k) {
                    selected += row[order[static_cast<size_t>(k)]];
                }
                for (int64_t k = 0; k < top_k; ++k) {
                    const int64_t e = order[static_cast<size_t>(k)];
                    top_idx[static_cast<size_t>(s * top_k + k)] = e;
                    top_w[static_cast<size_t>(s * top_k + k)] = row[e] / selected;
                }
            }
            if (router_trace_enabled_) {
                // §16 router-level evidence: the layer's router family,
                // the deterministic top-k selection and the renormalized
                // mixing weights (level 1); grouped dispatch below is the
                // expert-level execution the record refers to.
                RouterLayerTrace tr;
                tr.layer_id = layer_idx;
                tr.router_type = cfg.moe_router_sigmoid
                                     ? "sigmoid_topk" : "softmax_topk";
                tr.top_k = top_k;
                tr.token_count = total_tokens;
                tr.expert_ids = top_idx;
                tr.weights = top_w;
                tr.shared_experts =
                    static_cast<int64_t>(layer.shared_gate.size());
                tr.shared_expert_gated =
                    !layer.shared_expert_gate_t.empty();
                router_trace_.push_back(std::move(tr));
            }
            if (moe_trace_enabled_) {
                // Aggregated two-level MoE trace: level 1 = router
                // decision (router type, top-k, bounded per-token
                // selection sample), level 2 = expert dispatch histogram
                // + shared-expert participation, aggregated across this
                // request's forwards. Bounded by kMoeTraceSelectedCap.
                MoeTraceLayer* tl = nullptr;
                for (auto& x : moe_trace_.layers)
                    if (x.layer_id == layer_idx) { tl = &x; break; }
                if (tl == nullptr) {
                    MoeTraceLayer t;
                    t.layer_id = layer_idx;
                    t.router_type =
                        cfg.moe_router_sigmoid ? "sigmoid" : "softmax";
                    t.top_k = top_k;
                    t.expert_counts.assign(
                        static_cast<size_t>(experts), 0);
                    t.shared_expert_used = cfg.moe_num_shared_experts > 0;
                    moe_trace_.layers.push_back(std::move(t));
                    tl = &moe_trace_.layers.back();
                }
                tl->tokens_routed += total_tokens;
                for (int64_t s = 0; s < total_tokens; ++s)
                    for (int64_t k = 0; k < top_k; ++k)
                        ++tl->expert_counts[static_cast<size_t>(
                            top_idx[static_cast<size_t>(
                                s * top_k + k)])];
                for (int64_t s = 0; s < total_tokens; ++s) {
                    const double* row = probs.data() +
                        static_cast<size_t>(s * experts);
                    double ent = 0.0;
                    for (int64_t e = 0; e < experts; ++e)
                        if (row[e] > 0.0)
                            ent -= row[e] * std::log(row[e]);
                    tl->entropy_sum += ent;
                    for (int64_t k = 0; k < top_k; ++k) {
                        const double sc = row[static_cast<size_t>(
                            top_idx[static_cast<size_t>(
                                s * top_k + k)])];
                        tl->score_min = std::min(tl->score_min, sc);
                        tl->score_max = std::max(tl->score_max, sc);
                        tl->score_sum += sc;
                        ++tl->score_n;
                    }
                }
                int64_t room = kMoeTraceSelectedCap -
                    static_cast<int64_t>(tl->selected.size());
                for (int64_t s = 0; s < total_tokens && room > 0;
                     ++s, --room) {
                    tl->selected.emplace_back(
                        top_idx.begin() +
                            static_cast<size_t>(s * top_k),
                        top_idx.begin() +
                            static_cast<size_t>(s * top_k + top_k));
                    tl->weights.emplace_back(
                        top_w.begin() +
                            static_cast<size_t>(s * top_k),
                        top_w.begin() +
                            static_cast<size_t>(s * top_k + top_k));
                }
            }
            // R5 residual: grouped GEMM — each token's top-k expert rows are
            // gathered once into a single contiguous buffer (expert-major,
            // token order preserved inside each group, matching the former
            // per-expert `positions` order row for row), then gate / up /
            // down each run as one grouped GEMM dispatch instead of a
            // per-expert GEMM call chain. Identical rows + identical kernel
            // → identical outputs; only the dispatch/allocation shape changed.
            std::vector<int64_t>& group_count = fs_.moe_group_count;
            group_count.assign(static_cast<size_t>(experts), 0);
            for (int64_t s = 0; s < total_tokens; ++s) {
                for (int64_t k = 0; k < top_k; ++k) {
                    ++group_count[static_cast<size_t>(
                        top_idx[static_cast<size_t>(s * top_k + k)])];
                }
            }
            if (moe_trace_enabled_) {
                // Two-level MoE trace: level 1 = router decision (router
                // type, top-k, bounded per-token selection sample),
                // level 2 = expert dispatch histogram + shared-expert
                // participation, aggregated across this request's
                // forwards. Bounded by kMoeTraceSelectedCap.
                MoeTraceLayer* tl = nullptr;
                for (auto& x : moe_trace_.layers)
                    if (x.layer_id == layer_idx) { tl = &x; break; }
                if (tl == nullptr) {
                    MoeTraceLayer t;
                    t.layer_id = layer_idx;
                    t.router_type =
                        cfg.moe_router_sigmoid ? "sigmoid" : "softmax";
                    t.top_k = top_k;
                    t.expert_counts.assign(
                        static_cast<size_t>(experts), 0);
                    t.shared_expert_used = cfg.moe_num_shared_experts > 0;
                    moe_trace_.layers.push_back(std::move(t));
                    tl = &moe_trace_.layers.back();
                }
                tl->tokens_routed += total_tokens;
                for (int64_t e = 0; e < experts; ++e)
                    tl->expert_counts[static_cast<size_t>(e)] +=
                        group_count[static_cast<size_t>(e)];
                // §20: router score summary + entropy accumulate over
                // every routed token (unbounded aggregates, bounded
                // per-token buffers stay at kMoeTraceSelectedCap).
                for (int64_t s = 0; s < total_tokens; ++s) {
                    const double* row = probs.data() +
                        static_cast<size_t>(s * experts);
                    double ent = 0.0;
                    for (int64_t e = 0; e < experts; ++e)
                        if (row[e] > 0.0)
                            ent -= row[e] * std::log(row[e]);
                    tl->entropy_sum += ent;
                    for (int64_t k = 0; k < top_k; ++k) {
                        const double sc = row[static_cast<size_t>(
                            top_idx[static_cast<size_t>(
                                s * top_k + k)])];
                        tl->score_min = std::min(tl->score_min, sc);
                        tl->score_max = std::max(tl->score_max, sc);
                        tl->score_sum += sc;
                        ++tl->score_n;
                    }
                }
                int64_t room = kMoeTraceSelectedCap -
                    static_cast<int64_t>(tl->selected.size());
                for (int64_t s = 0; s < total_tokens && room > 0;
                     ++s, --room) {
                    tl->selected.emplace_back(
                        top_idx.begin() +
                            static_cast<size_t>(s * top_k),
                        top_idx.begin() +
                            static_cast<size_t>(s * top_k + top_k));
                    tl->weights.emplace_back(
                        top_w.begin() +
                            static_cast<size_t>(s * top_k),
                        top_w.begin() +
                            static_cast<size_t>(s * top_k + top_k));
                }
            }
            std::vector<int64_t>& group_offset = fs_.moe_group_offset;
            group_offset.resize(static_cast<size_t>(experts) + 1);
            group_offset[0] = 0;
            for (int64_t e = 0; e < experts; ++e) {
                group_offset[static_cast<size_t>(e + 1)] =
                    group_offset[static_cast<size_t>(e)] +
                    group_count[static_cast<size_t>(e)];
            }
            const int64_t grouped_rows =
                group_offset[static_cast<size_t>(experts)];
            std::vector<int64_t>& group_fill = fs_.moe_group_fill;
            group_fill.assign(
                group_offset.begin(), group_offset.end() - 1);
            std::vector<double>& grouped_in = fs_.moe_grouped_in;
            std::vector<int64_t>& row_token = fs_.moe_row_token;
            std::vector<double>& row_weight = fs_.moe_row_weight;
            grouped_in.resize(static_cast<size_t>(grouped_rows * hidden_size));
            row_token.resize(static_cast<size_t>(grouped_rows));
            row_weight.resize(static_cast<size_t>(grouped_rows));
            for (int64_t s = 0; s < total_tokens; ++s) {
                for (int64_t k = 0; k < top_k; ++k) {
                    const int64_t e =
                        top_idx[static_cast<size_t>(s * top_k + k)];
                    const int64_t r =
                        group_fill[static_cast<size_t>(e)]++;
                    std::copy_n(
                        normed.data() +
                            static_cast<size_t>(s * hidden_size),
                        hidden_size,
                        grouped_in.data() +
                            static_cast<size_t>(r * hidden_size));
                    row_token[static_cast<size_t>(r)] = s;
                    row_weight[static_cast<size_t>(r)] =
                        top_w[static_cast<size_t>(s * top_k + k)];
                }
            }
            std::vector<int64_t> group_rows;
            std::vector<const double*> gate_up_list;
            std::vector<const double*> down_list;
            group_rows.reserve(static_cast<size_t>(experts));
            for (int64_t e = 0; e < experts; ++e) {
                if (group_count[static_cast<size_t>(e)] == 0) continue;
                group_rows.push_back(group_count[static_cast<size_t>(e)]);
                gate_up_list.push_back(
                    layer.expert_gate_up_t[static_cast<size_t>(e)].data());
                down_list.push_back(
                    layer.expert_down_t[static_cast<size_t>(e)].data());
            }
            std::vector<double>& mlp_out = fs_.moe_mlp_out;
            mlp_out.assign(
                static_cast<size_t>(total_tokens * hidden_size), 0.0);
            if (!group_rows.empty()) {
                // Fused [gate|up] grouped GEMM: each row carries gate in
                // columns [0, inter) and up in [inter, 2*inter).
                std::vector<double>& gate_up = fs_.moe_gate_up;
                matmul_grouped_into(
                    grouped_in, group_rows, gate_up_list, hidden_size,
                    2 * expert_inter, gate_up);
                std::vector<double>& act = fs_.moe_act;
                act.resize(static_cast<size_t>(grouped_rows * expert_inter));
                for (int64_t r = 0; r < grouped_rows; ++r) {
                    const double* fused_row = gate_up.data() +
                        static_cast<size_t>(r * 2 * expert_inter);
                    double* act_row = act.data() +
                        static_cast<size_t>(r * expert_inter);
                    for (int64_t j = 0; j < expert_inter; ++j) {
                        const double g = fused_row[j];
                        act_row[j] =
                            (g / (1.0 + std::exp(-g))) * fused_row[expert_inter + j];
                    }
                }
                std::vector<double>& grouped_out = fs_.moe_grouped_out;
                matmul_grouped_into(
                    act, group_rows, down_list, expert_inter,
                    hidden_size, grouped_out);
                for (int64_t r = 0; r < grouped_rows; ++r) {
                    const double w = row_weight[static_cast<size_t>(r)];
                    const double* src = grouped_out.data() +
                        static_cast<size_t>(r * hidden_size);
                    double* dst = mlp_out.data() + static_cast<size_t>(
                        row_token[static_cast<size_t>(r)] * hidden_size);
                    axpy_f64(dst, w, src, hidden_size);
                }
            }
            // v27 shared-expert sigmoid gate: σ(gate·x) on the post-norm
            // input scales every shared expert's contribution (trainer
            // shared_gate on the n2 activation).
            const bool shared_gated = !layer.shared_expert_gate_t.empty();
            std::vector<double>& sg_sig = fs_.shared_sig;
            if (shared_gated) {
                linear_into(
                    normed, total_tokens, hidden_size,
                    layer.shared_expert_gate_t, 1, sg_sig);
                for (double& v : sg_sig) v = sigmoid_d(v);
            }
            // Shared experts (v26): always-on SwiGLU over every token,
            // weight 1.0 — mirrors modules/moe.py `output + shared(x)`.
            for (size_t se = 0; se < layer.shared_gate.size(); ++se) {
                std::vector<double>& sgu = fs_.shared_sgu;
                linear_into(
                    normed, total_tokens, hidden_size,
                    layer.shared_gate_up_t[se], 2 * shared_inter, sgu);
                std::vector<double>& sg = fs_.shared_sg;
                sg.resize(static_cast<size_t>(total_tokens * shared_inter));
                for (int64_t r = 0; r < total_tokens; ++r) {
                    const double* fused_row = sgu.data() +
                        static_cast<size_t>(r * 2 * shared_inter);
                    double* sg_row = sg.data() +
                        static_cast<size_t>(r * shared_inter);
                    for (int64_t j = 0; j < shared_inter; ++j) {
                        const double g = fused_row[j];
                        sg_row[j] =
                            (g / (1.0 + std::exp(-g))) * fused_row[shared_inter + j];
                    }
                }
                std::vector<double>& sd = fs_.shared_sd;
                linear_into(
                    sg, total_tokens, shared_inter,
                    layer.shared_down_t[se], hidden_size, sd);
                if (shared_gated) {
                    for (int64_t r = 0; r < total_tokens; ++r) {
                        axpy_f64(
                            mlp_out.data() +
                                static_cast<size_t>(r * hidden_size),
                            sg_sig[static_cast<size_t>(r)],
                            sd.data() +
                                static_cast<size_t>(r * hidden_size),
                            hidden_size);
                    }
                } else {
                    axpy_f64(
                        mlp_out.data(), 1.0, sd.data(),
                        static_cast<int64_t>(mlp_out.size()));
                }
            }
            if (moe_trace_enabled_ && !layer.shared_gate.empty()) {
                // §20 shared_expert_weight: Σ σ(gate·x) over the batch
                // (ungated shared experts contribute weight 1.0) — the
                // trace owns observability, never policy.
                for (auto& tl : moe_trace_.layers)
                    if (tl.layer_id == layer_idx) {
                        if (shared_gated)
                            for (double v : sg_sig)
                                tl.shared_weight_sum += v;
                        else
                            tl.shared_weight_sum += total_tokens;
                        break;
                    }
            }
            if (module_rms != nullptr) {
                module_rms->push_back(
                    hidden_rms(mlp_out, total_tokens, hidden_size));
            }
            axpy_f64(
                hidden.data(), 1.0, mlp_out.data(),
                static_cast<int64_t>(hidden.size()));
        } else {
            const int64_t inter = cfg.intermediate_size;
            std::vector<double>& gate_up = fs_.gate_up;
            linear_into(
                normed, total_tokens, hidden_size, layer.gate_up_t,
                2 * inter, gate_up);
            std::vector<double>& mlp_in = fs_.mlp_in;
            mlp_in.resize(static_cast<size_t>(total_tokens * inter));
            for (int64_t r = 0; r < total_tokens; ++r) {
                const double* fused_row = gate_up.data() +
                    static_cast<size_t>(r * 2 * inter);
                double* out_row = mlp_in.data() +
                    static_cast<size_t>(r * inter);
                for (int64_t j = 0; j < inter; ++j) {
                    const double g = fused_row[j];
                    out_row[j] =
                        (g / (1.0 + std::exp(-g))) * fused_row[inter + j];
                }
            }
            std::vector<double>& mlp_out = fs_.mlp_out;
            linear_into(
                mlp_in, total_tokens, cfg.intermediate_size,
                layer.down_proj_t, hidden_size, mlp_out);
            if (module_rms != nullptr) {
                module_rms->push_back(
                    hidden_rms(mlp_out, total_tokens, hidden_size));
            }
            axpy_f64(
                hidden.data(), 1.0, mlp_out.data(),
                static_cast<int64_t>(hidden.size()));
        }
        if (layer_rms != nullptr) {
            layer_rms->push_back(hidden_rms(hidden, total_tokens, hidden_size));
        }
    }

    for (size_t i = 0; i < spans.size(); ++i) {
        const BatchSpan& span = spans[i];
        if (span.append_cache) {
            // Advance by the fused length (vision prefix + text) — the next
            // position_offset must continue after the patch rows too.
            kv_lens_[static_cast<size_t>(span.slot)] =
                span.position_offset + vlens[i];
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

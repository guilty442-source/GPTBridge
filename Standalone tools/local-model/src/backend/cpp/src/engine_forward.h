// engine_forward.h — B94 fragment of engine.cpp (forward_batch_hidden).
// Included once by engine.cpp inside namespace xingcheng::inference.
#pragma once

std::vector<double> NativeInferenceEngine::forward_batch_hidden(
    const std::vector<BatchSpan>& spans,
    std::vector<double>* layer_rms,
    std::vector<double>* module_rms) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (spans.empty()) throw InferenceError("INPUT_EMPTY");
    const ModelConfig& cfg = bundle_->config();
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
            const int64_t token = (*span.ids)[static_cast<size_t>(s)];
            if (token < 0 || token >= cfg.vocab_size) {
                throw InferenceError("TOKEN_ID_OUT_OF_RANGE");
            }
            std::copy_n(
                embedding_.data + token * hidden_size,
                hidden_size,
                hidden.data() + static_cast<size_t>((base + patches + s) * hidden_size));
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
    // Per-span RoPE tables (each sequence carries its own position offset);
    // the frequency bases are model-constant and cached once per engine.
    std::vector<std::vector<double>> rope_cos(spans.size());
    std::vector<std::vector<double>> rope_sin(spans.size());
    if (cfg.position_embedding_type == "rope" && !fused_contract) {
        const std::vector<double>& bases = rope_bases(
            cfg.head_dim, cfg.rope_theta,
            rope_base_, rope_base_dim_, rope_base_theta_);
        for (size_t i = 0; i < spans.size(); ++i) {
            rope_tables(
                vlens[i],
                spans[i].position_offset, cfg.head_dim, bases,
                rope_cos[i], rope_sin[i]);
        }
    }
    // Fused-contract rope: partial rotary uses bases over rotary_dim,
    // full rotary (rd == head_dim) uses the interleaved pairing over
    // head_dim — both exactly as the trainer computes them.
    const std::vector<double>* fused_bases = nullptr;
    if (fused_contract && cfg.position_embedding_type == "rope") {
        const int64_t base_dim =
            rotary_dim < cfg.head_dim ? rotary_dim : cfg.head_dim;
        fused_bases = &rope_bases(
            base_dim, cfg.rope_theta,
            rope_base_, rope_base_dim_, rope_base_theta_);
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
        // (post-projection queries when the config is not rope).
        double rope_q_sumsq = 0.0;
        int64_t rope_q_count = 0;
        // Projections and FFN are token-wise: the packed rows of all spans
        // share one GEMM — that sharing is the R9 throughput win.
        std::vector<double>& q_flat = fs_.q_flat;
        std::vector<double>& k_flat = fs_.k_flat;
        std::vector<double>& v_flat = fs_.v_flat;
        linear_into(
            normed, total_tokens, hidden_size, layer.qkv_t,
            q_dim + 2 * kv_dim, fs_.qkv);
        split_columns(
            fs_.qkv, total_tokens,
            {{q_dim, &q_flat}, {kv_dim, &k_flat}, {kv_dim, &v_flat}});

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

            std::vector<double>* q_attn = &q_heads;
            std::vector<double>* k_attn = &k_heads;
            if (cfg.position_embedding_type == "rope") {
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
                for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                    if (xcuda_kv_write_rows(
                            1, layer_idx, h, span.position_offset, seq,
                            k_attn->data() +
                                static_cast<size_t>(h * seq * cfg.head_dim)) != 0 ||
                        xcuda_kv_write_rows(
                            0, layer_idx, h, span.position_offset, seq,
                            v_heads.data() +
                                static_cast<size_t>(h * seq * cfg.head_dim)) != 0) {
                        throw InferenceError("CUDA_KV_WRITE_FAILED");
                    }
                }
                if (xcuda_kv_attention(
                        layer_idx, q_attn->data(), cfg.num_attention_heads,
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
                            k_srcs[static_cast<size_t>(t)].fp =
                                k_attn->data() + static_cast<size_t>((kv_head * seq + s) * cfg.head_dim);
                            v_srcs[static_cast<size_t>(t)].fp =
                                v_heads.data() + static_cast<size_t>((kv_head * seq + s) * cfg.head_dim);
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

        if (module_rms != nullptr) {
            module_rms->push_back(
                rope_q_count > 0
                    ? std::sqrt(
                          rope_q_sumsq / static_cast<double>(rope_q_count))
                    : 0.0);
        }
        std::vector<double>& attn_out = fs_.attn_out;
        linear_into(
            attn_flat, total_tokens, q_dim, layer.o_proj_t,
            cfg.hidden_size, attn_out);
        if (module_rms != nullptr) {
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
                axpy_f64(
                    mlp_out.data(), 1.0, sd.data(),
                    static_cast<int64_t>(mlp_out.size()));
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

    for (const BatchSpan& span : spans) {
        if (span.append_cache) {
            kv_lens_[static_cast<size_t>(span.slot)] =
                span.position_offset + static_cast<int64_t>(span.ids->size());
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
    return normed;
}

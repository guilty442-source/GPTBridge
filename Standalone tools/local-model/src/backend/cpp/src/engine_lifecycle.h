// engine_lifecycle.h — B94 fragment of engine.cpp (NativeInferenceEngine lifecycle/encode/decode/kv/metrics).
// Included once by engine.cpp inside namespace xingcheng::inference.
#pragma once

NativeInferenceEngine::~NativeInferenceEngine() { unload(); }

void NativeInferenceEngine::load(const std::string& bundle_dir) {
    unload();
    try {
    const std::filesystem::path root(bundle_dir);
    bundle_ = std::make_unique<WeightBundle>(WeightBundle::load((root / "manifest.json").string()));
    const ModelConfig& cfg = bundle_->config();
    validate_supported();
    // CUDA opt-in is decided by the governed layer via env; requesting it
    // without a CUDA build or device fails closed at load.
    g_cuda_requested.store(env_flag("XINGCHENG_CPP_CUDA"));
    cuda_session_owned_ = false;
    if (g_cuda_requested.load()) {
#if defined(XINGCHENG_CUDA)
        if (!xcuda_available()) {
            g_cuda_requested.store(false);
            throw InferenceError("CUDA_UNAVAILABLE");
        }
        cuda_session_owned_ = true;
#else
        g_cuda_requested.store(false);
        throw InferenceError("CUDA_UNAVAILABLE");
#endif
    }
    // bf16 GEMM is a further opt-in on the CUDA path (P1-1③): requested but
    // kernels/device absent → fail closed at load, never silent fp64.
    g_cuda_bf16_requested.store(env_flag("XINGCHENG_CPP_CUDA_BF16"));
    if (g_cuda_bf16_requested.load()) {
#if defined(XINGCHENG_CUDA)
        if (!g_cuda_requested.load() || !xcuda_bf16_available()) {
            g_cuda_bf16_requested.store(false);
            throw InferenceError("CUDA_BF16_UNAVAILABLE");
        }
#else
        g_cuda_bf16_requested.store(false);
        throw InferenceError("CUDA_BF16_UNAVAILABLE");
#endif
    }
    // fp8 weight-storage GEMM (P1-1③ residual): same opt-in contract as
    // bf16. bf16 and fp8 are mutually exclusive precision domains —
    // requesting both is ambiguous configuration, not a precedence rule.
    g_cuda_fp8_requested.store(env_flag("XINGCHENG_CPP_CUDA_FP8"));
    if (g_cuda_fp8_requested.load()) {
        if (g_cuda_bf16_requested.load()) {
            g_cuda_fp8_requested.store(false);
            throw InferenceError("CUDA_PRECISION_CONFLICT");
        }
#if defined(XINGCHENG_CUDA)
        if (!g_cuda_requested.load() || !xcuda_fp8_available()) {
            g_cuda_fp8_requested.store(false);
            throw InferenceError("CUDA_FP8_UNAVAILABLE");
        }
#else
        g_cuda_fp8_requested.store(false);
        throw InferenceError("CUDA_FP8_UNAVAILABLE");
#endif
    }
    // P1-1② device-resident KV: opt-in on the CUDA path; the int8 KV format
    // has no device representation, so requesting both fails closed.
    g_cuda_kv_requested.store(env_flag("XINGCHENG_CPP_CUDA_KV"));
    if (g_cuda_kv_requested.load()) {
#if defined(XINGCHENG_CUDA)
        if (!g_cuda_requested.load() || !xcuda_kv_available()) {
            g_cuda_kv_requested.store(false);
            throw InferenceError("CUDA_KV_UNAVAILABLE");
        }
#else
        g_cuda_kv_requested.store(false);
        throw InferenceError("CUDA_KV_UNAVAILABLE");
#endif
    }

    embedding_ = bundle_->tensor("model.embeddings.word_embeddings.weight");
    final_norm_ = bundle_->tensor("model.final_norm.weight");
    lm_head_ = bundle_->has_tensor("lm_head.weight")
        ? bundle_->tensor("lm_head.weight")
        : embedding_;
    lm_head_t_ = transpose_matrix(lm_head_);

    // Gemma4 "sqrt_hidden_size" embedding scale resolves at load.
    if (cfg.embedding_scale < 0.0) {
        const_cast<ModelConfig&>(cfg).embedding_scale =
            std::sqrt(static_cast<double>(cfg.hidden_size));
    }

    // Vision early-fusion projection [hidden x patch_dim]. Required when
    // use_vision; absent otherwise (text-only bundles never carry it).
    if (cfg.use_vision) {
        if (!bundle_->has_tensor("vision.patch_proj.weight"))
            throw InferenceError("VISION_WEIGHT_MISSING");
        vision_patch_proj_ =
            bundle_->tensor("vision.patch_proj.weight");
        if (vision_patch_proj_.shape.size() != 2 ||
            vision_patch_proj_.shape[0] != cfg.hidden_size ||
            vision_patch_proj_.shape[1] != cfg.vision_patch_dim)
            throw InferenceError("VISION_WEIGHT_SHAPE_MISMATCH");
        vision_patch_proj_t_ = transpose_matrix(vision_patch_proj_);
    }

    layers_.assign(static_cast<size_t>(cfg.num_hidden_layers), LayerWeights{});
    const int64_t lin_kh = cfg.linear_num_key_heads;
    const int64_t lin_kd = cfg.linear_key_head_dim;
    const int64_t lin_vh = cfg.linear_num_value_heads;
    const int64_t lin_vd = cfg.linear_value_head_dim;
    const int64_t lin_ratio =
        lin_kh > 0 ? lin_vh / lin_kh : 0;
    const int64_t lin_key_dim = lin_kh * lin_kd;
    const int64_t lin_val_dim = lin_vh * lin_vd;
    const int64_t lin_conv_dim = lin_key_dim * 2 + lin_val_dim;
    const int64_t lin_group = 2 * lin_kd + lin_vd * lin_ratio;
    auto expect_shape = [](const TensorView& t,
                           const std::vector<int64_t>& dims,
                           const std::string& name) {
        if (t.shape != dims) {
            throw InferenceError("TENSOR_SHAPE_MISMATCH:" + name);
        }
    };
    for (int64_t i = 0; i < cfg.num_hidden_layers; ++i) {
        LayerWeights& layer = layers_[static_cast<size_t>(i)];
        const std::string prefix = "model.layers." + std::to_string(i) + ".";
        if (cfg.is_gemma4()) {
            const int64_t hd = cfg.layer_head_dim(i);
            layer.g4_head_dim = hd;
            layer.g4_q_dim = cfg.num_attention_heads * hd;
            layer.g4_kv_dim = cfg.num_key_value_heads * hd;
            layer.kv_shared = cfg.layer_kv_shared(i);
            layer.kv_anchor_layer =
                layer.kv_shared ? cfg.kv_anchor(i) : -1;
            layer.input_norm = bundle_->tensor(prefix + "input_norm.weight");
            layer.q_proj = bundle_->tensor(prefix + "attention.q_proj.weight");
            layer.q_norm = bundle_->tensor(prefix + "attention.q_norm.weight");
            if (!layer.kv_shared) {
                layer.k_proj = bundle_->tensor(prefix + "attention.k_proj.weight");
                layer.v_proj = bundle_->tensor(prefix + "attention.v_proj.weight");
                layer.k_norm = bundle_->tensor(prefix + "attention.k_norm.weight");
            }
            layer.o_proj = bundle_->tensor(prefix + "attention.o_proj.weight");
            layer.post_attn_norm =
                bundle_->tensor(prefix + "post_attention_norm.weight");
            layer.pre_ffn_norm =
                bundle_->tensor(prefix + "pre_feedforward_norm.weight");
            layer.post_ffn_norm =
                bundle_->tensor(prefix + "post_feedforward_norm.weight");
            layer.gate_proj = bundle_->tensor(prefix + "mlp.gate_proj.weight");
            layer.up_proj = bundle_->tensor(prefix + "mlp.up_proj.weight");
            layer.down_proj = bundle_->tensor(prefix + "mlp.down_proj.weight");
            const int64_t inter =
                cfg.intermediate_size *
                (cfg.use_double_wide_mlp && layer.kv_shared ? 2 : 1);
            const std::vector<double> gate_t =
                transpose_matrix(layer.gate_proj);
            const std::vector<double> up_t =
                transpose_matrix(layer.up_proj);
            layer.gate_up_t = hcat_weights(
                {{&gate_t, inter}, {&up_t, inter}}, cfg.hidden_size);
            layer.down_proj_t = transpose_matrix(layer.down_proj);
            layer.q_proj_t = transpose_matrix(layer.q_proj);
            layer.o_proj_t = transpose_matrix(layer.o_proj);
            if (!layer.kv_shared) {
                layer.k_proj_t = transpose_matrix(layer.k_proj);
                layer.v_proj_t = transpose_matrix(layer.v_proj);
            }
            if (cfg.hidden_size_per_layer_input > 0) {
                layer.ple_gate =
                    bundle_->tensor(prefix + "per_layer_input_gate.weight");
                layer.ple_proj =
                    bundle_->tensor(prefix + "per_layer_projection.weight");
                layer.ple_post_norm = bundle_->tensor(
                    prefix + "post_per_layer_input_norm.weight");
                layer.ple_gate_t = transpose_matrix(layer.ple_gate);
                layer.ple_proj_t = transpose_matrix(layer.ple_proj);
            }
            continue;
        }
        layer.input_norm = bundle_->tensor(prefix + "input_norm.weight");
        layer.post_norm = bundle_->tensor(prefix + "post_attention_norm.weight");
        layer.is_linear = cfg.is_linear_layer(i);
        if (layer.is_linear) {
            // v27 gated DeltaNet (linear attention): fused projection
            // weights, depthwise conv, decay/bias and gated norm.
            const std::string lp = prefix + "linear_attn.";
            layer.lin_in_proj_qkv =
                bundle_->tensor(lp + "in_proj_qkv.weight");
            layer.lin_in_proj_z = bundle_->tensor(lp + "in_proj_z.weight");
            layer.lin_in_proj_a = bundle_->tensor(lp + "in_proj_a.weight");
            layer.lin_in_proj_b = bundle_->tensor(lp + "in_proj_b.weight");
            layer.lin_conv1d = bundle_->tensor(lp + "conv1d.weight");
            layer.lin_a_log = bundle_->tensor(lp + "A_log.weight");
            layer.lin_dt_bias = bundle_->tensor(lp + "dt_bias.weight");
            layer.lin_out_norm = bundle_->tensor(lp + "norm.weight");
            layer.lin_out_proj = bundle_->tensor(lp + "out_proj.weight");
            expect_shape(layer.lin_in_proj_qkv,
                         {lin_kh * lin_group, cfg.hidden_size},
                         lp + "in_proj_qkv.weight");
            expect_shape(layer.lin_in_proj_z,
                         {lin_val_dim, cfg.hidden_size},
                         lp + "in_proj_z.weight");
            expect_shape(layer.lin_in_proj_a, {lin_vh, cfg.hidden_size},
                         lp + "in_proj_a.weight");
            expect_shape(layer.lin_in_proj_b, {lin_vh, cfg.hidden_size},
                         lp + "in_proj_b.weight");
            expect_shape(layer.lin_conv1d,
                         {lin_conv_dim, cfg.linear_conv_kernel_dim},
                         lp + "conv1d.weight");
            expect_shape(layer.lin_a_log, {lin_vh}, lp + "A_log.weight");
            expect_shape(layer.lin_dt_bias, {lin_vh}, lp + "dt_bias.weight");
            expect_shape(layer.lin_out_norm, {lin_vd}, lp + "norm.weight");
            expect_shape(layer.lin_out_proj, {cfg.hidden_size, lin_val_dim},
                         lp + "out_proj.weight");
            // Compile-time fusion: [qkv|z|a|b] share one GEMM.
            const std::vector<double> qkv_t =
                transpose_matrix(layer.lin_in_proj_qkv);
            const std::vector<double> z_t =
                transpose_matrix(layer.lin_in_proj_z);
            const std::vector<double> a_t =
                transpose_matrix(layer.lin_in_proj_a);
            const std::vector<double> b_t =
                transpose_matrix(layer.lin_in_proj_b);
            layer.lin_fused_t = hcat_weights(
                {{&qkv_t, lin_kh * lin_group},
                 {&z_t, lin_val_dim},
                 {&a_t, lin_vh},
                 {&b_t, lin_vh}},
                cfg.hidden_size);
            layer.lin_out_proj_t = transpose_matrix(layer.lin_out_proj);
        } else {
            layer.q_proj = bundle_->tensor(prefix + "attention.q_proj.weight");
            layer.k_proj = bundle_->tensor(prefix + "attention.k_proj.weight");
            layer.v_proj = bundle_->tensor(prefix + "attention.v_proj.weight");
            layer.o_proj = bundle_->tensor(prefix + "attention.o_proj.weight");
            const int64_t q_rows =
                cfg.num_attention_heads * cfg.head_dim *
                (cfg.attn_output_gate ? 2 : 1);
            expect_shape(layer.q_proj, {q_rows, cfg.hidden_size},
                         prefix + "attention.q_proj.weight");
            if (cfg.qk_norm) {
                layer.q_norm =
                    bundle_->tensor(prefix + "attention.q_norm.weight");
                layer.k_norm =
                    bundle_->tensor(prefix + "attention.k_norm.weight");
                expect_shape(layer.q_norm, {cfg.head_dim},
                             prefix + "attention.q_norm.weight");
                expect_shape(layer.k_norm, {cfg.head_dim},
                             prefix + "attention.k_norm.weight");
            }
            const std::vector<double> q_t = transpose_matrix(layer.q_proj);
            const std::vector<double> k_t = transpose_matrix(layer.k_proj);
            const std::vector<double> v_t = transpose_matrix(layer.v_proj);
            // attn_output_gate: the q block is 2*q_dim wide, per head
            // interleaved [q|gate] — de-interleaved per token in forward.
            layer.qkv_t = hcat_weights(
                {{&q_t, cfg.num_attention_heads * cfg.head_dim *
                            (cfg.attn_output_gate ? 2 : 1)},
                 {&k_t, cfg.num_key_value_heads * cfg.head_dim},
                 {&v_t, cfg.num_key_value_heads * cfg.head_dim}},
                cfg.hidden_size);
            layer.o_proj_t = transpose_matrix(layer.o_proj);
        }
        layer.is_moe = cfg.use_moe &&
            (i % std::max<int64_t>(1, cfg.moe_layer_interval)) == 0;
        if (layer.is_moe) {
            // R5 sparse MoE (token-choice routing, mirrors
            // modules/moe.py): router Linear + per-expert SwiGLU MLPs.
            layer.router = bundle_->tensor(prefix + "mlp.router.weight");
            layer.router_t = transpose_matrix(layer.router);
            const int64_t experts = cfg.moe_num_experts;
            layer.expert_gate.reserve(static_cast<size_t>(experts));
            layer.expert_up.reserve(static_cast<size_t>(experts));
            layer.expert_down.reserve(static_cast<size_t>(experts));
            layer.expert_gate_up_t.reserve(static_cast<size_t>(experts));
            layer.expert_down_t.reserve(static_cast<size_t>(experts));
            // Effective expert inner widths: forward falls back to
            // intermediate_size when the optional moe_*_intermediate_size
            // manifest fields are absent -- the fused gate_up/down views
            // must be built with the same width or they silently come out
            // empty (null data -> C_ABI_CALL_FAILED:matmul-grouped).
            const int64_t expert_inter =
                cfg.moe_expert_intermediate_size > 0
                    ? cfg.moe_expert_intermediate_size
                    : cfg.intermediate_size;
            const int64_t shared_inter =
                cfg.moe_shared_intermediate_size > 0
                    ? cfg.moe_shared_intermediate_size
                    : expert_inter;
            for (int64_t e = 0; e < experts; ++e) {
                const std::string ep =
                    prefix + "mlp.experts." + std::to_string(e) + ".";
                layer.expert_gate.push_back(
                    bundle_->tensor(ep + "gate_proj.weight"));
                layer.expert_up.push_back(
                    bundle_->tensor(ep + "up_proj.weight"));
                layer.expert_down.push_back(
                    bundle_->tensor(ep + "down_proj.weight"));
                const std::vector<double> gate_t =
                    transpose_matrix(layer.expert_gate.back());
                const std::vector<double> up_t =
                    transpose_matrix(layer.expert_up.back());
                layer.expert_gate_up_t.push_back(hcat_weights(
                    {{&gate_t, expert_inter},
                     {&up_t, expert_inter}},
                    cfg.hidden_size));
                layer.expert_down_t.push_back(
                    transpose_matrix(layer.expert_down.back()));
            }
            // Always-on shared experts (v26 DeepSeek-MoE): weight-1.0
            // contribution on every token, mirrors modules/moe.py.
            const int64_t shared = cfg.moe_num_shared_experts;
            layer.shared_gate.reserve(static_cast<size_t>(shared));
            layer.shared_up.reserve(static_cast<size_t>(shared));
            layer.shared_down.reserve(static_cast<size_t>(shared));
            layer.shared_gate_up_t.reserve(static_cast<size_t>(shared));
            layer.shared_down_t.reserve(static_cast<size_t>(shared));
            for (int64_t e = 0; e < shared; ++e) {
                const std::string sp =
                    prefix + "mlp.shared_experts." +
                    std::to_string(e) + ".";
                layer.shared_gate.push_back(
                    bundle_->tensor(sp + "gate_proj.weight"));
                layer.shared_up.push_back(
                    bundle_->tensor(sp + "up_proj.weight"));
                layer.shared_down.push_back(
                    bundle_->tensor(sp + "down_proj.weight"));
                const std::vector<double> gate_t =
                    transpose_matrix(layer.shared_gate.back());
                const std::vector<double> up_t =
                    transpose_matrix(layer.shared_up.back());
                layer.shared_gate_up_t.push_back(hcat_weights(
                    {{&gate_t, shared_inter},
                     {&up_t, shared_inter}},
                    cfg.hidden_size));
                layer.shared_down_t.push_back(
                    transpose_matrix(layer.shared_down.back()));
            }
            // v27 shared-expert sigmoid gate: a [1 x hidden] linear on
            // the post-norm activation scaling every shared expert out.
            if (cfg.shared_expert_gate && shared > 0) {
                layer.shared_expert_gate = bundle_->tensor(
                    prefix + "mlp.shared_expert_gate.weight");
                expect_shape(layer.shared_expert_gate,
                             {int64_t{1}, cfg.hidden_size},
                             prefix + "mlp.shared_expert_gate.weight");
                layer.shared_expert_gate_t =
                    transpose_matrix(layer.shared_expert_gate);
            }
        } else {
            layer.gate_proj = bundle_->tensor(prefix + "mlp.gate_proj.weight");
            layer.up_proj = bundle_->tensor(prefix + "mlp.up_proj.weight");
            layer.down_proj = bundle_->tensor(prefix + "mlp.down_proj.weight");
            const std::vector<double> gate_t =
                transpose_matrix(layer.gate_proj);
            const std::vector<double> up_t =
                transpose_matrix(layer.up_proj);
            layer.gate_up_t = hcat_weights(
                {{&gate_t, cfg.intermediate_size},
                 {&up_t, cfg.intermediate_size}},
                cfg.hidden_size);
            layer.down_proj_t = transpose_matrix(layer.down_proj);
        }
    }

    // Gemma4 model-level PLE tensors.
    if (cfg.is_gemma4()) {
        if (cfg.hidden_size_per_layer_input > 0) {
            embed_per_layer_ =
                bundle_->tensor("model.embed_tokens_per_layer.weight");
            ple_model_projection_ = bundle_->tensor(
                "model.per_layer_model_projection.weight");
            ple_projection_norm_ = bundle_->tensor(
                "model.per_layer_projection_norm.weight");
            ple_model_projection_t_ =
                transpose_matrix(ple_model_projection_);
        }
    }

    // KV pools cover only full-attention layers: DeltaNet layers carry
    // their own bounded per-slot state instead, so blocks are sized by
    // the dense ordinal count (75% of a 4:1 hybrid is never wasted).
    kv_layer_ord_.assign(static_cast<size_t>(cfg.num_hidden_layers), -1);
    int64_t kv_layers = 0;
    for (int64_t i = 0; i < cfg.num_hidden_layers; ++i) {
        if (!cfg.is_linear_layer(i)) {
            kv_layer_ord_[static_cast<size_t>(i)] = kv_layers++;
        }
    }
    // Gemma4 layers carry per-type head dims; the pool keeps a
    // uniform per-head stride sized to the largest head_dim.
    const int64_t kv_head_dim =
        cfg.is_gemma4() ? cfg.max_head_dim() : cfg.head_dim;
    const int64_t kv_dim = cfg.num_key_value_heads * kv_head_dim;
    // KV INT8 (opt-in): per-token/per-head symmetric quantization shrinks
    // the packed element stride ~8x; the governed layer owns the env flag.
    kv_int8_ = env_flag("XINGCHENG_CPP_KV_INT8");
    kv_elem_stride_bytes_ = kv_int8_
        ? ((kv_head_dim + 7) & ~int64_t{7}) + 8
        : kv_head_dim * static_cast<int64_t>(sizeof(double));
    const int64_t kv_bytes = kv_int8_
        ? kv_layers * cfg.max_position_embeddings *
              cfg.num_key_value_heads * kv_elem_stride_bytes_ * 2
        : kv_layers * cfg.max_position_embeddings * kv_dim * 8 * 2;
    if (kv_limit_bytes_ > 0 && kv_bytes > kv_limit_bytes_) {
        throw InferenceError("KV_MEMORY_LIMIT_EXCEEDED");
    }
    // R6 paged KV: allocate on demand instead of the worst-case footprint.
    kv_block_stride_ = kv_int8_
        ? (kv_layers * kKvBlockTokens *
               cfg.num_key_value_heads * kv_elem_stride_bytes_ + 7) /
              8
        : kv_layers * kKvBlockTokens * kv_dim;
    if (kv_pool_ != nullptr) {
        gptbridge_kv_pool_destroy(kv_pool_);
        kv_pool_ = nullptr;
    }
    kv_pool_ = gptbridge_kv_pool_create(kv_block_stride_, kv_limit_bytes_);
    if (kv_pool_ == nullptr) throw InferenceError("KV_POOL_CREATE_FAILED");
    kv_block_tables_.clear();
    kv_slot_active_.clear();
    kv_lens_.clear();
    lin_states_.clear();

    // Device-resident KV mirror (P1-1②): fp64 device buffers sized to the
    // worst-case footprint; writes are mirrored per row for slot 0 only.
    // int8 KV has no device format — the combination fails closed rather
    // than silently serving a different precision than requested.
    kv_device_active_ = false;
#if defined(XINGCHENG_CUDA)
    if (g_cuda_kv_requested.load()) {
        if (kv_int8_) throw InferenceError("CUDA_KV_UNSUPPORTED_CONFIG");
        // The device path assumes a uniform per-layer head_dim;
        // the hybrid Gemma4 profile stays host-side fail-closed.
        if (cfg.is_gemma4()) {
            throw InferenceError("CUDA_KV_UNSUPPORTED_CONFIG");
        }
        if (xcuda_kv_alloc(
                kv_layers, cfg.num_key_value_heads,
                cfg.head_dim, cfg.max_position_embeddings) != 0) {
            throw InferenceError("CUDA_KV_UNAVAILABLE");
        }
        kv_device_active_ = true;
    }
#endif

    const std::filesystem::path tokenizer_path = root / "tokenizer.json";
    if (std::filesystem::exists(tokenizer_path)) {
        tokenizer_ = std::make_unique<ByteLevelBPETokenizer>(
            ByteLevelBPETokenizer::load(tokenizer_path.string()));
        const std::string tok_bytes =
            read_text(tokenizer_path, 64 * 1024 * 1024);
        tokenizer_sha256_ = sha256_hex(
            reinterpret_cast<const unsigned char*>(tok_bytes.data()),
            tok_bytes.size());
    } else {
        tokenizer_sha256_.clear();
    }
    } catch (...) {
        // A refused load must leave zero partial state: bundle_/kv_pool_/
        // tokenizer_ all cleared so loaded()==false and a later call never
        // operates on a half-initialized engine.
        unload();
        throw;
    }
}

bool NativeInferenceEngine::cuda_active() const {
    return g_cuda_requested.load();
}

void NativeInferenceEngine::unload() {
    bundle_.reset();
    tokenizer_.reset();
#if defined(XINGCHENG_CUDA)
    // Only the instance that activated the CUDA session tears it down — a
    // stale engine's destructor must not free a live engine's device state.
    if (cuda_session_owned_) {
        xcuda_release_weights();
        g_cuda_requested.store(false);
        g_cuda_bf16_requested.store(false);
        g_cuda_fp8_requested.store(false);
        g_cuda_kv_requested.store(false);
        cuda_session_owned_ = false;
    }
#endif
    kv_device_active_ = false;
    layers_.clear();
    prefix_cache_.clear();
    prefix_scope_ = "default";
    prefix_ctx_sha256_.clear();
    tokenizer_sha256_.clear();
    prefix_tick_ = 0;
    prefix_hits_ = 0;
    prefix_misses_ = 0;
    if (kv_pool_ != nullptr) {
        gptbridge_kv_pool_destroy(kv_pool_);
        kv_pool_ = nullptr;
    }
    kv_block_tables_.clear();
    kv_slot_active_.clear();
    kv_lens_.clear();
    lin_states_.clear();
    kv_layer_ord_.clear();
    kv_block_stride_ = 0;
    kv_int8_ = false;
    kv_elem_stride_bytes_ = 0;
    lm_head_t_.clear();
    ple_model_projection_t_.clear();
    sequence_.clear();
}

void NativeInferenceEngine::validate_supported() const {
    const ModelConfig& cfg = bundle_->config();
    if (cfg.use_moe &&
        (cfg.moe_num_experts < 2 || cfg.moe_top_k < 1 ||
         cfg.moe_top_k > cfg.moe_num_experts || cfg.moe_layer_interval < 1 ||
         cfg.moe_num_shared_experts < 0 ||
         cfg.moe_expert_intermediate_size < 0 ||
         cfg.moe_shared_intermediate_size < 0)) {
        throw InferenceError("MOE_CONFIG_UNSUPPORTED");
    }
    // Accepted manifest quantizations: none / int8 / int4(_packed —
    // the exporter's exact string) / bf16 (PRODUCTION_BF16 candidate);
    // bf16 and int8/int4 weights are dequantized to fp64 at load — the
    // marker records storage, not a different math lane.
    if (cfg.quantization != "none" && cfg.quantization != "int8" &&
        cfg.quantization != "int4" && cfg.quantization != "int4_packed" &&
        cfg.quantization != "bf16") {
        throw InferenceError("QUANTIZED_INFERENCE_UNSUPPORTED");
    }
    if (cfg.norm_type != "rmsnorm") throw InferenceError("NORM_TYPE_UNSUPPORTED");
    if (cfg.is_gemma4()) {
        // Gemma4 profile bounds: uniform fields are validated below;
        // here only the hybrid-attention invariants. Head dims are
        // per layer type — hidden_size is intentionally NOT required
        // to equal heads*head_dim.
        if (static_cast<int64_t>(cfg.layer_types.size()) !=
            cfg.num_hidden_layers) {
            throw InferenceError("LAYER_TYPES_LENGTH_MISMATCH");
        }
        bool any_sliding = false;
        for (const std::string& t : cfg.layer_types) {
            if (t != "sliding_attention" && t != "full_attention") {
                throw InferenceError("LAYER_TYPE_UNSUPPORTED:" + t);
            }
            if (t == "sliding_attention") any_sliding = true;
        }
        if (any_sliding && cfg.sliding_window <= 0) {
            throw InferenceError("SLIDING_WINDOW_MISSING");
        }
        if (cfg.head_dim <= 0 || cfg.global_head_dim <= 0 ||
            (cfg.head_dim % 2) != 0 || (cfg.global_head_dim % 2) != 0) {
            throw InferenceError("G4_HEAD_DIM_UNSUPPORTED");
        }
        if (cfg.num_kv_shared_layers < 0 ||
            cfg.num_kv_shared_layers >= cfg.num_hidden_layers) {
            throw InferenceError("KV_SHARED_BOUNDS_INVALID");
        }
        for (int64_t i = cfg.first_kv_shared_layer();
             i < cfg.num_hidden_layers; ++i) {
            if (cfg.num_kv_shared_layers > 0 && cfg.kv_anchor(i) < 0) {
                throw InferenceError("KV_SHARED_ANCHOR_MISSING");
            }
        }
        if (cfg.hidden_size_per_layer_input < 0 ||
            cfg.vocab_size_per_layer_input < 0 ||
            (cfg.hidden_size_per_layer_input > 0 &&
             cfg.vocab_size_per_layer_input <= 0)) {
            throw InferenceError("PLE_CONFIG_UNSUPPORTED");
        }
        // E4B scope is the dense family; the Gemma4 MoE block
        // (26B-A4B) is a separate profile, refused fail-closed.
        if (cfg.use_moe) {
            throw InferenceError("MOE_GEMMA4_UNSUPPORTED");
        }
        if (cfg.hidden_act != "gelu_pytorch_tanh" &&
            cfg.hidden_act != "silu") {
            throw InferenceError("MLP_TYPE_UNSUPPORTED");
        }
        if (cfg.position_embedding_type != "rope") {
            throw InferenceError("POSITION_EMBEDDING_UNSUPPORTED");
        }
        if (cfg.num_attention_heads <= 0 || cfg.num_key_value_heads <= 0 ||
            cfg.num_attention_heads % cfg.num_key_value_heads != 0 ||
            cfg.hidden_size <= 0) {
            throw InferenceError("MODEL_SHAPE_UNSUPPORTED");
        }
        return;
    }
    if (cfg.use_vision &&
        (cfg.vision_patch_dim <= 0 || cfg.vision_max_patches <= 0)) {
        throw InferenceError("VISION_CONFIG_UNSUPPORTED");
    }
    if (!cfg.use_swiglu || cfg.hidden_act != "silu") {
        throw InferenceError("MLP_TYPE_UNSUPPORTED");
    }
    if (cfg.position_embedding_type != "rope" && cfg.position_embedding_type != "learned") {
        throw InferenceError("POSITION_EMBEDDING_UNSUPPORTED");
    }
    if (cfg.hidden_size <= 0 || cfg.head_dim <= 0 ||
        cfg.hidden_size != cfg.num_attention_heads * cfg.head_dim ||
        cfg.num_key_value_heads <= 0 ||
        cfg.num_attention_heads % cfg.num_key_value_heads != 0) {
        throw InferenceError("MODEL_SHAPE_UNSUPPORTED");
    }
    // v27 fused hybrid: interval selects periodic full attention; all
    // other layers need a complete, geometrically consistent DeltaNet
    // block — a partial declaration fails closed instead of silently
    // degrading to the dense path.
    if (cfg.full_attention_interval < 0) {
        throw InferenceError("LINEAR_ATTN_CONFIG_UNSUPPORTED");
    }
    if (cfg.full_attention_interval > 0) {
        if (cfg.linear_num_key_heads <= 0 || cfg.linear_key_head_dim <= 0 ||
            cfg.linear_num_value_heads <= 0 ||
            cfg.linear_value_head_dim <= 0 ||
            cfg.linear_num_value_heads % cfg.linear_num_key_heads != 0 ||
            cfg.linear_conv_kernel_dim <= 0 ||
            cfg.num_key_value_heads <= 0) {
            throw InferenceError("LINEAR_ATTN_CONFIG_UNSUPPORTED");
        }
    }
    if (cfg.partial_rotary_factor <= 0.0 || cfg.partial_rotary_factor > 1.0) {
        throw InferenceError("LINEAR_ATTN_CONFIG_UNSUPPORTED");
    }
    if (cfg.fused_rope_contract() &&
        cfg.position_embedding_type != "rope") {
        throw InferenceError("POSITION_EMBEDDING_UNSUPPORTED");
    }
}

std::vector<int64_t> NativeInferenceEngine::encode(
    const std::string& text,
    bool add_bos,
    bool add_eos,
    int64_t max_length) const {
    if (tokenizer_ == nullptr) {
        throw InferenceError("TOKENIZER_NOT_LOADED");
    }
    return tokenizer_->encode(text, add_bos, add_eos, max_length);
}

std::string NativeInferenceEngine::decode(
    const std::vector<int64_t>& ids,
    bool skip_special) const {
    if (tokenizer_ == nullptr) {
        throw InferenceError("TOKENIZER_NOT_LOADED");
    }
    return tokenizer_->decode(ids, skip_special);
}

int32_t NativeInferenceEngine::kv_alloc_block() {
    if (kv_pool_ == nullptr) throw InferenceError("KV_POOL_NOT_INITIALIZED");
    const int32_t id = gptbridge_kv_pool_alloc(kv_pool_);
    if (id < 0) throw InferenceError("KV_MEMORY_LIMIT_EXCEEDED");
    return id;
}

int64_t NativeInferenceEngine::kv_alloc_slot() {
    // R9: bounded per-sequence KV namespaces over the shared pool.
    const int64_t layers = bundle_->config().num_hidden_layers;
    for (int64_t i = 0; i < static_cast<int64_t>(kv_slot_active_.size()); ++i) {
        if (!kv_slot_active_[static_cast<size_t>(i)]) {
            kv_slot_active_[static_cast<size_t>(i)] = true;
            kv_block_tables_[static_cast<size_t>(i)].clear();
            kv_lens_[static_cast<size_t>(i)] = 0;
            lin_states_[static_cast<size_t>(i)].assign(
                static_cast<size_t>(layers), LinLayerState{});
            return i;
        }
    }
    if (static_cast<int64_t>(kv_slot_active_.size()) >= kMaxBatchSeqs) {
        throw InferenceError("BATCH_SLOT_EXHAUSTED");
    }
    kv_slot_active_.push_back(true);
    kv_block_tables_.emplace_back();
    kv_lens_.push_back(0);
    lin_states_.emplace_back(static_cast<size_t>(layers), LinLayerState{});
    return static_cast<int64_t>(kv_slot_active_.size()) - 1;
}

void NativeInferenceEngine::kv_free_slot(int64_t slot) {
    if (slot < 0 || slot >= static_cast<int64_t>(kv_block_tables_.size())) {
        return;
    }
    for (const int32_t block : kv_block_tables_[static_cast<size_t>(slot)]) {
        gptbridge_kv_pool_release(kv_pool_, block);
    }
    kv_block_tables_[static_cast<size_t>(slot)].clear();
    kv_lens_[static_cast<size_t>(slot)] = 0;
    // DeltaNet state is slot-scoped exactly like the KV blocks: a freed
    // sequence must never leak its recurrence into the next tenant.
    for (LinLayerState& st : lin_states_[static_cast<size_t>(slot)]) {
        st = LinLayerState{};
    }
    kv_slot_active_[static_cast<size_t>(slot)] = false;
}

void NativeInferenceEngine::kv_ensure_position(int64_t slot, int64_t position) {
    const int64_t block_index = position / kKvBlockTokens;
    std::vector<int32_t>& table = kv_block_tables_[static_cast<size_t>(slot)];
    while (static_cast<int64_t>(table.size()) <= block_index) {
        table.push_back(kv_alloc_block());
    }
}

char* NativeInferenceEngine::kv_slot_bytes(
    int64_t slot, bool key_cache, int64_t layer, int64_t position, int64_t head) {
    const ModelConfig& cfg = bundle_->config();
    const int64_t ord = kv_layer_ord_[static_cast<size_t>(layer)];
    if (ord < 0) throw InferenceError("KV_LAYER_NOT_CACHED");
    const int64_t block =
        kv_block_tables_[static_cast<size_t>(slot)]
            [static_cast<size_t>(position / kKvBlockTokens)];
    const int64_t elem_index =
        ord * (kKvBlockTokens * cfg.num_key_value_heads) +
        (position % kKvBlockTokens) * cfg.num_key_value_heads + head;
    char* base = reinterpret_cast<char*>(
        gptbridge_kv_pool_data(
            kv_pool_, static_cast<int32_t>(block), key_cache ? 1 : 0));
    if (base == nullptr) throw InferenceError("KV_BLOCK_NOT_ACTIVE");
    return base + static_cast<size_t>(elem_index) *
                      static_cast<size_t>(kv_elem_stride_bytes_);
}

// Per-token/per-head symmetric quantization: scale = amax/127 stored as a
// trailing double after the aligned int8 payload (scale 0 = zero vector).
void NativeInferenceEngine::kv_write(
    int64_t slot, bool key_cache, int64_t layer, int64_t position,
    int64_t head, const double* src) {
    const ModelConfig& cfg = bundle_->config();
    const int64_t n = cfg.is_gemma4()
        ? cfg.layer_head_dim(layer) : cfg.head_dim;
    char* dst = kv_slot_bytes(slot, key_cache, layer, position, head);
#if defined(XINGCHENG_CUDA)
    // Write-through to the device-resident mirror (slot 0 only); the host
    // pool stays the source of truth and a failed mirror fails the forward
    // — never silently divergent caches. Hybrid: device buffers are
    // indexed by the dense full-attention ordinal, like the host pool.
    if (kv_device_active_ && slot == 0 &&
        xcuda_kv_write_rows(
            key_cache ? 1 : 0,
            kv_layer_ord_[static_cast<size_t>(layer)], head, position, 1,
            src) != 0) {
        throw InferenceError("CUDA_KV_WRITE_FAILED");
    }
#endif
    if (!kv_int8_) {
        std::memcpy(dst, src, static_cast<size_t>(n) * sizeof(double));
        return;
    }
    double amax = 0.0;
    for (int64_t i = 0; i < n; ++i) {
        const double a = std::abs(src[i]);
        if (a > amax) amax = a;
    }
    const double scale = amax > 0.0 ? amax / 127.0 : 0.0;
    int8_t* q = reinterpret_cast<int8_t*>(dst);
    if (scale > 0.0) {
        for (int64_t i = 0; i < n; ++i) {
            long v = std::lround(src[i] / scale);
            if (v > 127) v = 127;
            if (v < -127) v = -127;
            q[i] = static_cast<int8_t>(v);
        }
    } else {
        std::memset(q, 0, static_cast<size_t>(n));
    }
    *reinterpret_cast<double*>(dst + ((n + 7) & ~int64_t{7})) = scale;
}

// Prefix-restore write path: the snapshot already holds the on-pool
// representation, so the bytes land verbatim — under KV-INT8 this avoids
// requantizing a dequantized vector (which would drift the stored scale
// by ~1 ulp and break bit-identical restore). The fp64 CUDA mirror still
// gets its write-through; kv_int8_ never reaches the device (load fails
// closed), so raw bytes suffice there.
void NativeInferenceEngine::kv_restore_bytes(
    int64_t slot, bool key_cache, int64_t layer, int64_t position,
    int64_t head, const char* raw) {
    char* dst = kv_slot_bytes(slot, key_cache, layer, position, head);
#if defined(XINGCHENG_CUDA)
    if (!kv_int8_ && kv_device_active_ && slot == 0 &&
        xcuda_kv_write_rows(
            key_cache ? 1 : 0,
            kv_layer_ord_[static_cast<size_t>(layer)], head, position, 1,
            reinterpret_cast<const double*>(raw)) != 0) {
        throw InferenceError("CUDA_KV_WRITE_FAILED");
    }
#endif
    std::memcpy(dst, raw, static_cast<size_t>(kv_elem_stride_bytes_));
}

NativeInferenceEngine::KvSrc NativeInferenceEngine::kv_src(
    int64_t slot, bool key_cache, int64_t layer, int64_t position, int64_t head) {
    char* p = kv_slot_bytes(slot, key_cache, layer, position, head);
    KvSrc src;
    const ModelConfig& cfg = bundle_->config();
    const int64_t n = cfg.is_gemma4()
        ? cfg.layer_head_dim(layer) : cfg.head_dim;
    if (kv_int8_) {
        src.q8 = reinterpret_cast<const int8_t*>(p);
        src.scale = *reinterpret_cast<const double*>(p + ((n + 7) & ~int64_t{7}));
    } else {
        src.fp = reinterpret_cast<const double*>(p);
    }
    return src;
}

void NativeInferenceEngine::kv_read_head(
    int64_t slot, bool key_cache, int64_t layer, int64_t position,
    int64_t head, double* out) {
    const KvSrc src = kv_src(slot, key_cache, layer, position, head);
    const ModelConfig& cfg = bundle_->config();
    const int64_t n = cfg.is_gemma4()
        ? cfg.layer_head_dim(layer) : cfg.head_dim;
    if (src.q8 != nullptr) {
        for (int64_t i = 0; i < n; ++i) {
            out[i] = static_cast<double>(src.q8[i]) * src.scale;
        }
    } else {
        std::copy_n(src.fp, n, out);
    }
}

void NativeInferenceEngine::reset_cache() {
    for (int64_t slot = 0;
         slot < static_cast<int64_t>(kv_block_tables_.size()); ++slot) {
        kv_free_slot(slot);
    }
    // Slot 0 is the single-sequence namespace: keep it live after every
    // reset (kv_alloc_slot returns the first inactive slot → slot 0).
    kv_alloc_slot();
    sequence_.clear();
    mem_prefill_peak_ = 0;
    mem_decode_peak_ = 0;
}

std::vector<double> NativeInferenceEngine::logits(const std::vector<int64_t>& input_ids) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    return forward_last_logits(input_ids, 0, false);
}

std::vector<double> NativeInferenceEngine::forward_all_hidden(
    const std::vector<int64_t>& input_ids) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    return forward_hidden(input_ids, 0, false);
}

// §49 decision-head binding accessors — an unloaded engine exposes no
// identity, so every accessor fails closed instead of returning "".
const std::string& NativeInferenceEngine::model_sha256() const {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    return bundle_->weights_sha256();
}
const std::string& NativeInferenceEngine::generation() const {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    return bundle_->architecture_generation();
}
const std::string& NativeInferenceEngine::tokenizer_sha256() const {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    return tokenizer_sha256_;
}
int64_t NativeInferenceEngine::hidden_size() const {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    return bundle_->config().hidden_size;
}
std::vector<double> NativeInferenceEngine::prefill_hidden(
    const std::vector<int64_t>& input_ids) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (input_ids.empty()) throw InferenceError("PREFILL_EMPTY_INPUT");
    // Pure feedforward — a decision writes no KV/DeltaNet state
    // (append_cache needs an active decode slot, which the no-decode
    // path never allocates). Prefix-cache reuse applies when the
    // decision rides on a generative prefill that populated the cache
    // (§14); the standalone fast path simply never pays for state it
    // will not use.
    return forward_hidden(input_ids, 0, false);
}

std::pair<double, int64_t> NativeInferenceEngine::sequence_nll(
    const std::vector<int64_t>& input_ids) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (input_ids.size() < 2) return {0.0, 0};
    const std::vector<double> hidden =
        forward_hidden(input_ids, 0, false);
    const ModelConfig& cfg = bundle_->config();
    const int64_t rows = static_cast<int64_t>(input_ids.size()) - 1;
    const int64_t h = cfg.hidden_size;
    const int64_t v = cfg.vocab_size;
    double nll = 0.0;
    int64_t scored = 0;
    for (int64_t i = 0; i < rows; ++i) {
        const int64_t target = input_ids[static_cast<size_t>(i + 1)];
        if (target < 0 || target >= v) continue;
        std::vector<double> row = matmul(
            hidden.data() + static_cast<size_t>(i) * h, 1, h,
            lm_head_t_.data(), v);
        logit_softcap(row, cfg.final_logit_softcapping);
        const double mx =
            *std::max_element(row.begin(), row.end());
        double se = 0.0;
        for (const double x : row) se += std::exp(x - mx);
        nll += (mx + std::log(se)) - row[static_cast<size_t>(target)];
        ++scored;
    }
    return {nll, scored};
}

std::vector<double> NativeInferenceEngine::forward_last_logits(
    const std::vector<int64_t>& input_ids,
    int64_t position_offset,
    bool append_cache) {
    const std::vector<double> hidden = forward_hidden(input_ids, position_offset, append_cache);
    const ModelConfig& cfg = bundle_->config();
    const int64_t rows = static_cast<int64_t>(input_ids.size());
    const double* last = hidden.data() + static_cast<size_t>((rows - 1) * cfg.hidden_size);
    std::vector<double> logits =
        matmul(last, 1, cfg.hidden_size, lm_head_t_.data(), cfg.vocab_size);
    logit_softcap(logits, cfg.final_logit_softcapping);
    return logits;
}

static double hidden_rms(
    const std::vector<double>& hidden, int64_t seq, int64_t hidden_size) {
    double sum_sq = 0.0;
    for (double value : hidden) sum_sq += value * value;
    const int64_t count = seq * hidden_size;
    return count > 0 ? std::sqrt(sum_sq / static_cast<double>(count)) : 0.0;
}

std::vector<double> NativeInferenceEngine::layer_metrics(
    const std::vector<int64_t>& input_ids) {
    std::vector<double> trace;
    forward_hidden(input_ids, 0, false, &trace);
    return trace;
}

std::vector<double> NativeInferenceEngine::module_metrics(
    const std::vector<int64_t>& input_ids) {
    std::vector<double> trace;
    forward_hidden(input_ids, 0, false, nullptr, &trace);
    return trace;
}

std::vector<double> NativeInferenceEngine::forward_hidden(
    const std::vector<int64_t>& input_ids,
    int64_t position_offset,
    bool append_cache,
    std::vector<double>* layer_rms,
    std::vector<double>* module_rms) {
    // R9: the single-sequence path is the packed batch path with one span.
    BatchSpan span;
    span.slot = 0;
    span.ids = &input_ids;
    span.position_offset = position_offset;
    span.append_cache = append_cache;
    return forward_batch_hidden({span}, layer_rms, module_rms);
}

// §58 governed lane switch for certification probes. The CUDA
// request flags are atomics consulted per matmul call, so toggling
// between forwards lets one process compare lanes without respawning.
// Production admission stays env-gated (XINGCHENG_CPP_CUDA*); this is
// a probe hook on the same atomics, not a second admission path.
extern "C" void xengine_cuda_lane(int cuda_requested, int bf16_requested) {
    g_cuda_requested.store(cuda_requested != 0);
    g_cuda_bf16_requested.store(bf16_requested != 0);
}
extern "C" int xengine_cuda_lane_state() {
    return (g_cuda_requested.load() ? 1 : 0) |
           (g_cuda_bf16_requested.load() ? 2 : 0);
}
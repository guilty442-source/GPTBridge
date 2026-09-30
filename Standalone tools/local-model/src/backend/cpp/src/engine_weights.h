// engine_weights.h — B94 fragment of engine.cpp (WeightBundle blob/load/tensor accessors).
// Included once by engine.cpp inside namespace xingcheng::inference.
#pragma once

struct WeightBundle::Blob {
    std::vector<unsigned char> fallback;
    const unsigned char* data = nullptr;
    size_t size = 0;
#ifdef _WIN32
    HANDLE file = INVALID_HANDLE_VALUE;
    HANDLE mapping = nullptr;
    void* view = nullptr;
#endif

    ~Blob() {
#ifdef _WIN32
        if (view != nullptr) UnmapViewOfFile(view);
        if (mapping != nullptr) CloseHandle(mapping);
        if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
#endif
    }
};

WeightBundle::~WeightBundle() = default;
WeightBundle::WeightBundle(WeightBundle&&) noexcept = default;
WeightBundle& WeightBundle::operator=(WeightBundle&&) noexcept = default;

std::unique_ptr<WeightBundle::Blob> map_readonly_file(
    const std::filesystem::path& path,
    int64_t max_bytes) {
    auto blob = std::make_unique<WeightBundle::Blob>();
#ifdef _WIN32
    blob->file = CreateFileW(
        path.wstring().c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (blob->file == INVALID_HANDLE_VALUE) {
        throw InferenceError("BUNDLE_FILE_UNREADABLE:" + path.string());
    }
    LARGE_INTEGER file_size{};
    if (!GetFileSizeEx(blob->file, &file_size) ||
        file_size.QuadPart < 0 || file_size.QuadPart > max_bytes) {
        throw InferenceError("BUNDLE_FILE_TOO_LARGE");
    }
    blob->size = static_cast<size_t>(file_size.QuadPart);
    if (blob->size > 0) {
        blob->mapping = CreateFileMappingW(
            blob->file, nullptr, PAGE_READONLY, 0, 0, nullptr);
        if (blob->mapping == nullptr) {
            throw InferenceError("BUNDLE_MMAP_FAILED");
        }
        blob->view = MapViewOfFile(blob->mapping, FILE_MAP_READ, 0, 0, 0);
        if (blob->view == nullptr) {
            throw InferenceError("BUNDLE_MMAP_VIEW_FAILED");
        }
        blob->data = static_cast<const unsigned char*>(blob->view);
    }
    return blob;
#else
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    if (!input) throw InferenceError("BUNDLE_FILE_UNREADABLE:" + path.string());
    const std::streamoff size = input.tellg();
    if (size < 0 || size > max_bytes) throw InferenceError("BUNDLE_FILE_TOO_LARGE");
    blob->fallback.resize(static_cast<size_t>(size));
    input.seekg(0, std::ios::beg);
    if (size > 0 &&
        !input.read(reinterpret_cast<char*>(blob->fallback.data()),
                    static_cast<std::streamsize>(size))) {
        throw InferenceError("BUNDLE_FILE_READ_FAILED");
    }
    blob->data = blob->fallback.data();
    blob->size = blob->fallback.size();
    return blob;
#endif
}

// ── Weight bundle (P3b) ───────────────────────────────────────────────

int64_t TensorView::size() const {
    return shape.empty() ? 0 : checked_product(shape);
}

WeightBundle WeightBundle::load(const std::string& manifest_path) {
    WeightBundle bundle;
    const std::filesystem::path manifest_file(manifest_path);
    const JsonValue manifest = JsonParser(read_text(manifest_file, 64 * 1024 * 1024)).parse();
    if (json_string(manifest, "schema_version") != "star-native-inference-bundle/v1") {
        throw InferenceError("BUNDLE_SCHEMA_UNSUPPORTED");
    }

    const JsonValue& config_json = json_field(manifest, "config");
    ModelConfig& cfg = bundle.config_;
    cfg.vocab_size = json_int(config_json, "vocab_size");
    cfg.hidden_size = json_int(config_json, "hidden_size");
    cfg.intermediate_size = json_int(config_json, "intermediate_size");
    cfg.num_hidden_layers = json_int(config_json, "num_hidden_layers");
    cfg.num_attention_heads = json_int(config_json, "num_attention_heads");
    cfg.num_key_value_heads = json_int(config_json, "num_key_value_heads");
    cfg.head_dim = json_int(config_json, "head_dim");
    cfg.max_position_embeddings = json_int(config_json, "max_position_embeddings");
    cfg.bos_token_id = json_int(config_json, "bos_token_id");
    cfg.eos_token_id = json_int(config_json, "eos_token_id");
    cfg.pad_token_id = json_int(config_json, "pad_token_id");
    cfg.rms_norm_eps = json_number(config_json, "rms_norm_eps");
    cfg.rope_theta = json_number(config_json, "rope_theta");
    cfg.use_swiglu = json_bool(config_json, "use_swiglu");
    cfg.tie_word_embeddings = json_bool(config_json, "tie_word_embeddings");
    cfg.norm_type = json_string(config_json, "norm_type");
    cfg.hidden_act.clear();
    if (const JsonValue* v = json_optional(config_json, "hidden_act")) {
        if (v->type != JsonValue::Type::String)
            throw InferenceError("JSON_STRING_EXPECTED:hidden_act");
        cfg.hidden_act = v->string;
    }
    if (cfg.hidden_act.empty()) {
        // HF config name for the same field.
        if (const JsonValue* v =
                json_optional(config_json, "hidden_activation")) {
            if (v->type != JsonValue::Type::String)
                throw InferenceError("JSON_STRING_EXPECTED:hidden_activation");
            cfg.hidden_act = v->string;
        }
    }
    if (cfg.hidden_act.empty()) {
        throw InferenceError("JSON_FIELD_MISSING:hidden_act");
    }
    cfg.position_embedding_type = json_string(config_json, "position_embedding_type");
    cfg.use_moe = json_bool(config_json, "use_moe");
    // MoE shape fields are optional in the manifest: bundles exported
    // before R5 predate them and always carry use_moe=false.
    if (const JsonValue* v = json_optional(config_json, "moe_num_experts")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:moe_num_experts");
        cfg.moe_num_experts = static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "moe_top_k")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:moe_top_k");
        cfg.moe_top_k = static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "moe_layer_interval")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:moe_layer_interval");
        cfg.moe_layer_interval = static_cast<int64_t>(v->number);
    }
    // v26 fine-grained/shared-expert fields — optional for the same
    // backward-compat reason as the R5 shape fields above.
    if (const JsonValue* v = json_optional(config_json, "moe_num_shared_experts")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:moe_num_shared_experts");
        cfg.moe_num_shared_experts = static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "moe_expert_intermediate_size")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:moe_expert_intermediate_size");
        cfg.moe_expert_intermediate_size = static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "moe_shared_intermediate_size")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:moe_shared_intermediate_size");
        cfg.moe_shared_intermediate_size = static_cast<int64_t>(v->number);
    }
    // Vision early-fusion fields — optional; absent means text-only
    // (use_vision=false), so pre-vision bundles load unchanged.
    if (const JsonValue* v = json_optional(config_json, "use_vision")) {
        if (v->type != JsonValue::Type::Bool)
            throw InferenceError("JSON_BOOL_EXPECTED:use_vision");
        cfg.use_vision = v->boolean;
    }
    if (const JsonValue* v = json_optional(config_json, "vision_patch_dim")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:vision_patch_dim");
        cfg.vision_patch_dim = static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "vision_max_patches")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:vision_max_patches");
        cfg.vision_max_patches = static_cast<int64_t>(v->number);
    }
    // v27 fused-hybrid fields — optional; absent means the legacy dense
    // all-full-attention layout, so pre-v27 bundles load unchanged.
    if (const JsonValue* v = json_optional(config_json, "full_attention_interval")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:full_attention_interval");
        cfg.full_attention_interval = static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "attn_output_gate")) {
        if (v->type != JsonValue::Type::Bool)
            throw InferenceError("JSON_BOOL_EXPECTED:attn_output_gate");
        cfg.attn_output_gate = v->boolean;
    }
    if (const JsonValue* v = json_optional(config_json, "qk_norm")) {
        if (v->type != JsonValue::Type::Bool)
            throw InferenceError("JSON_BOOL_EXPECTED:qk_norm");
        cfg.qk_norm = v->boolean;
    }
    if (const JsonValue* v = json_optional(config_json, "shared_expert_gate")) {
        if (v->type != JsonValue::Type::Bool)
            throw InferenceError("JSON_BOOL_EXPECTED:shared_expert_gate");
        cfg.shared_expert_gate = v->boolean;
    }
    if (const JsonValue* v = json_optional(config_json, "moe_router_sigmoid")) {
        if (v->type != JsonValue::Type::Bool)
            throw InferenceError("JSON_BOOL_EXPECTED:moe_router_sigmoid");
        cfg.moe_router_sigmoid = v->boolean;
    }
    if (const JsonValue* v = json_optional(config_json, "partial_rotary_factor")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_NUM_EXPECTED:partial_rotary_factor");
        cfg.partial_rotary_factor = v->number;
    }
    // v29 Qwen3-Coder YaRN fields — optional; absent means plain rope
    // (use_yarn()==false), so older bundles load unchanged.
    if (const JsonValue* v = json_optional(config_json, "yarn_factor")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_NUM_EXPECTED:yarn_factor");
        cfg.yarn_factor = v->number;
    }
    if (const JsonValue* v = json_optional(config_json,
                                         "yarn_original_max_position_embeddings")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError(
                "JSON_INT_EXPECTED:yarn_original_max_position_embeddings");
        cfg.yarn_original_max_position_embeddings =
            static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "yarn_beta_fast")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_NUM_EXPECTED:yarn_beta_fast");
        cfg.yarn_beta_fast = v->number;
    }
    if (const JsonValue* v = json_optional(config_json, "yarn_beta_slow")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_NUM_EXPECTED:yarn_beta_slow");
        cfg.yarn_beta_slow = v->number;
    }
    if (const JsonValue* v = json_optional(config_json,
                                         "yarn_attention_factor")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_NUM_EXPECTED:yarn_attention_factor");
        cfg.yarn_attention_factor = v->number;
    }
    for (const auto& field : {
             "linear_num_key_heads", "linear_key_head_dim",
             "linear_num_value_heads", "linear_value_head_dim",
             "linear_conv_kernel_dim"}) {
        if (const JsonValue* v = json_optional(config_json, field)) {
            if (v->type != JsonValue::Type::Number)
                throw InferenceError(
                    std::string("JSON_INT_EXPECTED:") + field);
            const int64_t value = static_cast<int64_t>(v->number);
            if (std::string(field) == "linear_num_key_heads")
                cfg.linear_num_key_heads = value;
            else if (std::string(field) == "linear_key_head_dim")
                cfg.linear_key_head_dim = value;
            else if (std::string(field) == "linear_num_value_heads")
                cfg.linear_num_value_heads = value;
            else if (std::string(field) == "linear_value_head_dim")
                cfg.linear_value_head_dim = value;
            else
                cfg.linear_conv_kernel_dim = value;
        }
    }
    // Fail closed on architecture axes the engine cannot execute: the
    // trainer serializes Gemma-A4B hybrid-locality fields into XCN5/6
    // checkpoints (sliding-window local attention, unified K==V, per-axis
    // rope, post-proj norms, gelu FFN, logit softcap). A manifest that
    // declares any of them non-default must never be silently run under
    // dense-layer semantics.
    for (const auto& field : {
             "global_attention_interval", "sliding_window_size",
             "num_global_kv_heads"}) {
        if (const JsonValue* v = json_optional(config_json, field)) {
            if (v->type != JsonValue::Type::Number)
                throw InferenceError(
                    std::string("JSON_INT_EXPECTED:") + field);
            if (static_cast<int64_t>(v->number) > 0)
                throw InferenceError(
                    std::string("MODEL_AXIS_UNSUPPORTED:") + field);
        }
    }
    for (const auto& field : {
             "k_eq_v_global", "use_post_attn_norm", "use_post_ffw_norm"}) {
        if (const JsonValue* v = json_optional(config_json, field)) {
            if (v->type != JsonValue::Type::Bool)
                throw InferenceError(
                    std::string("JSON_BOOL_EXPECTED:") + field);
            if (v->boolean)
                throw InferenceError(
                    std::string("MODEL_AXIS_UNSUPPORTED:") + field);
        }
    }
    for (const auto& field : {
             "local_rope_proportion", "global_rope_proportion",
             "local_base_frequency", "global_base_frequency",
             "final_logit_softcap"}) {
        if (const JsonValue* v = json_optional(config_json, field)) {
            if (v->type != JsonValue::Type::Number)
                throw InferenceError(
                    std::string("JSON_NUM_EXPECTED:") + field);
            if (v->number > 0.0)
                throw InferenceError(
                    std::string("MODEL_AXIS_UNSUPPORTED:") + field);
        }
    }
    if (const JsonValue* v = json_optional(config_json, "ffn_activation")) {
        if (v->type != JsonValue::Type::String)
            throw InferenceError("JSON_STR_EXPECTED:ffn_activation");
        if (v->string != "silu")
            throw InferenceError(
                "MODEL_AXIS_UNSUPPORTED:ffn_activation");
    }
    cfg.quantization = json_string(config_json, "quantization");
    // Gemma4 profile — every field optional; the profile activates
    // when layer_types is present (per-layer-type attention) or the
    // manifest declares the gemma4 family (layer_types then required
    // by validation, fail-closed when absent).
    if (const JsonValue* v = json_optional(config_json, "model_type")) {
        if (v->type != JsonValue::Type::String)
            throw InferenceError("JSON_STRING_EXPECTED:model_type");
        cfg.model_family = v->string;
    }
    if (cfg.model_family.empty()) {
        if (const JsonValue* v =
                json_optional(config_json, "model_family")) {
            if (v->type != JsonValue::Type::String)
                throw InferenceError("JSON_STRING_EXPECTED:model_family");
            cfg.model_family = v->string;
        }
    }
    if (const JsonValue* v = json_optional(config_json, "layer_types")) {
        if (v->type != JsonValue::Type::Array)
            throw InferenceError("JSON_ARRAY_EXPECTED:layer_types");
        for (const JsonValue& item : v->array) {
            if (item.type != JsonValue::Type::String)
                throw InferenceError("JSON_STRING_EXPECTED:layer_types");
            cfg.layer_types.push_back(item.string);
        }
    }
    if (const JsonValue* v = json_optional(config_json, "sliding_window")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:sliding_window");
        cfg.sliding_window = static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "global_head_dim")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:global_head_dim");
        cfg.global_head_dim = static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "rope_parameters")) {
        if (v->type != JsonValue::Type::Object)
            throw InferenceError("JSON_OBJECT_EXPECTED:rope_parameters");
        if (const JsonValue* full = json_optional(*v, "full_attention")) {
            if (full->type == JsonValue::Type::Object) {
                if (const JsonValue* t = json_optional(*full, "rope_theta"))
                    cfg.rope_theta_full = t->number;
                if (const JsonValue* f = json_optional(*full, "partial_rotary_factor"))
                    cfg.rope_partial_factor_full = f->number;
            }
        }
        // sliding_attention.rope_theta overrides the top-level
        // rope_theta for sliding layers when present.
        if (const JsonValue* sw = json_optional(*v, "sliding_attention")) {
            if (sw->type == JsonValue::Type::Object) {
                if (const JsonValue* t = json_optional(*sw, "rope_theta"))
                    cfg.rope_theta = t->number;
            }
        }
    }
    if (const JsonValue* v = json_optional(config_json, "num_kv_shared_layers")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:num_kv_shared_layers");
        cfg.num_kv_shared_layers = static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "hidden_size_per_layer_input")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:hidden_size_per_layer_input");
        cfg.hidden_size_per_layer_input = static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "vocab_size_per_layer_input")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_INT_EXPECTED:vocab_size_per_layer_input");
        cfg.vocab_size_per_layer_input = static_cast<int64_t>(v->number);
    }
    if (const JsonValue* v = json_optional(config_json, "final_logit_softcapping")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_NUMBER_EXPECTED:final_logit_softcapping");
        cfg.final_logit_softcapping = v->number;
    }
    if (const JsonValue* v = json_optional(config_json, "embedding_scale")) {
        if (v->type == JsonValue::Type::Number) {
            cfg.embedding_scale = v->number;
        } else if (v->type == JsonValue::Type::String &&
                   v->string == "sqrt_hidden_size") {
            cfg.embedding_scale = -1.0;   // resolved in load()
        } else {
            throw InferenceError("JSON_NUMBER_EXPECTED:embedding_scale");
        }
    }
    if (const JsonValue* v = json_optional(config_json, "attention_scale")) {
        if (v->type != JsonValue::Type::Number)
            throw InferenceError("JSON_NUMBER_EXPECTED:attention_scale");
        cfg.attention_scale = v->number;
    }
    if (const JsonValue* v =
            json_optional(config_json, "query_pre_attn_scalar")) {
        // HF name: attention scale = query_pre_attn_scalar ** -0.5.
        if (v->type != JsonValue::Type::Number || v->number <= 0.0)
            throw InferenceError("JSON_NUMBER_EXPECTED:query_pre_attn_scalar");
        if (cfg.attention_scale == 0.0) {
            cfg.attention_scale = std::pow(v->number, -0.5);
        }
    }
    if (const JsonValue* v = json_optional(config_json, "attention_k_eq_v")) {
        if (v->type != JsonValue::Type::Bool)
            throw InferenceError("JSON_BOOL_EXPECTED:attention_k_eq_v");
        cfg.attention_k_eq_v = v->boolean;
    }
    if (const JsonValue* v = json_optional(config_json, "use_double_wide_mlp")) {
        if (v->type != JsonValue::Type::Bool)
            throw InferenceError("JSON_BOOL_EXPECTED:use_double_wide_mlp");
        cfg.use_double_wide_mlp = v->boolean;
    }

    const std::string weights_name = json_string(manifest, "weights_file");
    bundle.weights_sha256_ = json_string(manifest, "weights_sha256");
    const std::filesystem::path weights_path = manifest_file.parent_path() / weights_name;
    bundle.blob_ = map_readonly_file(weights_path, 16LL * 1024 * 1024 * 1024);
    bundle.weights_bytes_ = static_cast<int64_t>(bundle.blob_->size);
    if (sha256_hex(bundle.blob_->data, bundle.blob_->size) != bundle.weights_sha256_) {
        throw InferenceError("BUNDLE_WEIGHTS_SHA256_MISMATCH");
    }

    const JsonValue& tensors = json_field(manifest, "tensors");
    if (tensors.type != JsonValue::Type::Object) {
        throw InferenceError("BUNDLE_TENSORS_OBJECT_EXPECTED");
    }
    for (const auto& [name, info] : tensors.object) {
        if (info.type != JsonValue::Type::Object) {
            throw InferenceError("TENSOR_INFO_INVALID");
        }
        const std::string dtype = json_string(info, "dtype");
        if (dtype != "float64" && dtype != "int8" && dtype != "int4_packed") {
            throw InferenceError("TENSOR_DTYPE_UNSUPPORTED:" + name);
        }
        const std::string endian = json_string(info, "endianness");
        if (endian != "little") {
            throw InferenceError("TENSOR_ENDIANNESS_UNSUPPORTED:" + name);
        }
        TensorInfo item;
        item.offset = json_int(info, "offset");
        item.bytes = json_int(info, "bytes");
        item.shape = json_shape(json_field(info, "shape"));
        const int64_t elements = checked_product(item.shape);
        int64_t expected_bytes = elements * 8;
        if (dtype == "int8") {
            expected_bytes = elements;
        } else if (dtype == "int4_packed") {
            if (item.shape.size() != 2) {
                throw InferenceError("TENSOR_INT4_SHAPE_UNSUPPORTED:" + name);
            }
            const int64_t rows = elements / item.shape.back();
            expected_bytes = rows * ((item.shape.back() + 1) / 2);
        }
        if (item.offset < 0 || item.bytes != expected_bytes ||
            item.offset > bundle.weights_bytes_ ||
            item.bytes > bundle.weights_bytes_ - item.offset) {
            throw InferenceError("TENSOR_BOUNDS_INVALID:" + name);
        }
        TensorView view;
        view.shape = item.shape;
        if (dtype == "float64") {
            view.data = reinterpret_cast<const double*>(
                bundle.blob_->data + item.offset);
        } else {
            // Weight-only per-tensor symmetric quantization (mirrors
            // kernels/quant.py): dequantize once at load into owned fp64
            // storage so every downstream GEMM is unchanged.
            const JsonValue* scale_v = json_optional(info, "scale");
            if (scale_v == nullptr ||
                scale_v->type != JsonValue::Type::Number ||
                !(scale_v->number > 0.0)) {
                throw InferenceError("TENSOR_SCALE_INVALID:" + name);
            }
            const double scale = scale_v->number;
            const unsigned char* raw = bundle.blob_->data + item.offset;
            bundle.owned_tensors_.emplace_back(
                static_cast<size_t>(elements));
            std::vector<double>& dst = bundle.owned_tensors_.back();
            if (dtype == "int8") {
                for (int64_t i = 0; i < elements; ++i) {
                    dst[static_cast<size_t>(i)] =
                        static_cast<double>(
                            reinterpret_cast<const int8_t*>(raw)[i]) * scale;
                }
            } else {
                // int4_packed: two 4-bit values per byte along the last dim
                // (low nibble = even index, high nibble = odd), shifted +8.
                const int64_t last = item.shape.back();
                const int64_t rows = elements / last;
                const int64_t packed_row = (last + 1) / 2;
                for (int64_t r = 0; r < rows; ++r) {
                    const unsigned char* prow = raw + r * packed_row;
                    double* drow = dst.data() + r * last;
                    for (int64_t c = 0; c < last; ++c) {
                        const unsigned char byte = prow[c / 2];
                        const int64_t nibble =
                            (c % 2 == 0) ? (byte & 0x0F) : (byte >> 4);
                        drow[c] = static_cast<double>(nibble - 8) * scale;
                    }
                }
            }
            view.data = dst.data();
        }
        bundle.tensors_.emplace(name, item);
        bundle.views_.emplace(name, view);
    }
    return bundle;
}

const TensorView& WeightBundle::tensor(const std::string& name) const {
    const auto it = views_.find(name);
    if (it == views_.end()) {
        throw InferenceError("TENSOR_MISSING:" + name);
    }
    return it->second;
}

bool WeightBundle::has_tensor(const std::string& name) const {
    return views_.find(name) != views_.end();
}

std::vector<std::string> WeightBundle::tensor_names() const {
    std::vector<std::string> names;
    names.reserve(views_.size());
    for (const auto& [name, unused] : views_) {
        (void)unused;
        names.push_back(name);
    }
    std::sort(names.begin(), names.end());
    return names;
}

// ── Tokenizer (P3c) ───────────────────────────────────────────────────

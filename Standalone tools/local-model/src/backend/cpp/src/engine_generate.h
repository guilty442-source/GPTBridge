// engine_generate.h — B94 fragment of engine.cpp (sample/generate*/describe/parse_generated_output).
// Included once by engine.cpp inside namespace xingcheng::inference.
#pragma once

std::vector<std::vector<double>> NativeInferenceEngine::forward_batch_last_logits(
    const std::vector<BatchSpan>& spans) {
    // Packed forward → gather each span's last-position hidden row → one
    // lm_head GEMM for the whole batch → slice back per span.
    const std::vector<double> hidden = forward_batch_hidden(spans);
    const ModelConfig& cfg = bundle_->config();
    const int64_t hidden_size = cfg.hidden_size;
    std::vector<double> last_rows;
    last_rows.reserve(static_cast<size_t>(spans.size() * hidden_size));
    int64_t base = 0;
    for (const BatchSpan& span : spans) {
        const int64_t seq = static_cast<int64_t>(span.ids->size()) +
            span.vision_num_patches;
        const double* last =
            hidden.data() + static_cast<size_t>((base + seq - 1) * hidden_size);
        last_rows.insert(last_rows.end(), last, last + hidden_size);
        base += seq;
    }
    std::vector<double> logits = matmul(
        last_rows.data(), static_cast<int64_t>(spans.size()),
        hidden_size, lm_head_t_.data(), cfg.vocab_size);
    logit_softcap(logits, cfg.final_logit_softcapping);
    std::vector<std::vector<double>> out(spans.size());
    for (size_t i = 0; i < spans.size(); ++i) {
        const double* row = logits.data() + i * cfg.vocab_size;
        out[i].assign(row, row + cfg.vocab_size);
    }
    return out;
}

std::vector<double> NativeInferenceEngine::forward_vision_logits(
    const std::vector<int64_t>& input_ids,
    const std::vector<double>& patches,
    int64_t num_patches) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    BatchSpan span;
    span.slot = 0;
    span.ids = &input_ids;
    span.position_offset = 0;
    span.append_cache = false;
    span.vision_patches = &patches;
    span.vision_num_patches = num_patches;
    const std::vector<BatchSpan> spans{span};
    std::vector<std::vector<double>> out = forward_batch_last_logits(spans);
    if (out.empty()) throw InferenceError("VISION_NO_OUTPUT");
    return out[0];
}

int64_t NativeInferenceEngine::sample_next(
    const std::vector<double>& logits,
    const std::vector<int64_t>& previous,
    const SamplingConfig& sampling,
    uint64_t& rng_state) const {
    const bool greedy = !sampling.do_sample || sampling.temperature <= 0.0;
    if (greedy && sampling.repetition_penalty == 1.0) {
        // Fast path — argmax over the raw logits, zero per-token copy.
        return static_cast<int64_t>(std::distance(
            logits.begin(), std::max_element(logits.begin(), logits.end())));
    }
    if (greedy) {
        // Repetition penalty, greedy: apply the penalty lazily during the
        // argmax scan — the winner is identical to adjusting a full copy,
        // without the vocab-size allocation per token.
        std::unordered_set<int64_t> seen(previous.begin(), previous.end());
        int64_t best = -1;
        double best_val = -std::numeric_limits<double>::infinity();
        for (size_t i = 0; i < logits.size(); ++i) {
            double v = logits[i];
            if (seen.count(static_cast<int64_t>(i))) {
                v = v > 0 ? v / sampling.repetition_penalty
                          : v * sampling.repetition_penalty;
            }
            if (best < 0 || v > best_val) {
                best = static_cast<int64_t>(i);
                best_val = v;
            }
        }
        return best;
    }
    std::vector<double> adjusted = logits;
    if (sampling.repetition_penalty != 1.0) {
        std::unordered_set<int64_t> seen(previous.begin(), previous.end());
        for (const int64_t token : seen) {
            if (token >= 0 && token < static_cast<int64_t>(adjusted.size())) {
                double& value = adjusted[static_cast<size_t>(token)];
                value = value > 0 ? value / sampling.repetition_penalty
                                  : value * sampling.repetition_penalty;
            }
        }
    }
    for (double& value : adjusted) value /= sampling.temperature;
    if (sampling.top_k > 0 && sampling.top_k < static_cast<int64_t>(adjusted.size())) {
        std::vector<double> sorted = adjusted;
        std::nth_element(
            sorted.begin(), sorted.begin() + static_cast<std::ptrdiff_t>(sampling.top_k), sorted.end(),
            std::greater<double>());
        const double threshold = sorted[static_cast<size_t>(sampling.top_k)];
        for (double& value : adjusted) {
            if (value < threshold) value = -std::numeric_limits<double>::infinity();
        }
    }
    if (sampling.top_p > 0.0 && sampling.top_p < 1.0) {
        // Progressive exact-semantics top-p: grow a sorted candidate
        // prefix (nth_element partition + prefix sort) until cumulative
        // mass reaches top_p. Typical runs order ~10² entries instead of
        // the full vocab's O(V log V) sort; the keep set and cumulative
        // sum order are identical to sorting the entire order descending.
        // The comparator's index tie-break makes equal-valued entries
        // deterministic regardless of partitioning.
        std::vector<size_t> order(adjusted.size());
        for (size_t i = 0; i < order.size(); ++i) order[i] = i;
        const auto by_value_desc = [&](size_t a, size_t b) {
            return adjusted[a] > adjusted[b] ||
                   (adjusted[a] == adjusted[b] && a < b);
        };
        const double max_value = *std::max_element(adjusted.begin(), adjusted.end());
        double total = 0.0;
        for (const double value : adjusted) total += std::exp(value - max_value);
        std::vector<bool> keep(adjusted.size(), false);
        double cumulative = 0.0;
        size_t consumed = 0;
        size_t window = std::min<size_t>(order.size(), 64);
        bool reached = false;
        while (true) {
            std::nth_element(
                order.begin(),
                order.begin() + static_cast<std::ptrdiff_t>(window - 1),
                order.end(), by_value_desc);
            std::sort(order.begin(),
                      order.begin() + static_cast<std::ptrdiff_t>(window),
                      by_value_desc);
            for (; consumed < window; ++consumed) {
                keep[order[consumed]] = true;
                cumulative +=
                    std::exp(adjusted[order[consumed]] - max_value) / total;
                if (cumulative >= sampling.top_p) {
                    ++consumed;
                    reached = true;
                    break;
                }
            }
            if (reached || window >= order.size()) break;
            window = std::min(order.size(), window * 4);
        }
        for (size_t i = 0; i < adjusted.size(); ++i) {
            if (!keep[i]) adjusted[i] = -std::numeric_limits<double>::infinity();
        }
    }
    const double max_value = *std::max_element(adjusted.begin(), adjusted.end());
    double total = 0.0;
    for (const double value : adjusted) total += std::exp(value - max_value);
    std::mt19937_64 rng(rng_state);
    std::uniform_real_distribution<double> uniform(0.0, total);
    const double target = uniform(rng);
    rng_state = rng();
    double cumulative = 0.0;
    for (size_t i = 0; i < adjusted.size(); ++i) {
        cumulative += std::exp(adjusted[i] - max_value);
        if (target <= cumulative) return static_cast<int64_t>(i);
    }
    return static_cast<int64_t>(adjusted.size() - 1);
}

std::vector<int64_t> NativeInferenceEngine::generate(
    const std::vector<int64_t>& prompt_ids,
    int64_t max_new_tokens,
    const SamplingConfig& sampling) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (prompt_ids.empty()) throw InferenceError("PROMPT_EMPTY");
    if (max_new_tokens <= 0) return {};
    const ModelConfig& cfg = bundle_->config();
    if (static_cast<int64_t>(prompt_ids.size()) + max_new_tokens > cfg.max_position_embeddings) {
        throw InferenceError("SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS");
    }
    reset_cache();
    sequence_ = prompt_ids;
    std::vector<int64_t> generated;
    generated.reserve(static_cast<size_t>(max_new_tokens));
    uint64_t rng_state = sampling.seed ? sampling.seed : 0x9E3779B97F4A7C15ULL;

    // P3d prefix reuse: restore the longest cached prompt prefix so only the
    // suffix is recomputed. The snapshot stores per-layer K/V slices; values
    // are deterministic, so a restored cache is bit-identical to recompute.
    // The per-head slice stride matches the KV pool element stride — Gemma4
    // hybrid layers keep a uniform max_head_dim row width.
    // HybridPrefixCache v2: a prefix entry is KV + DeltaStateSnapshot +
    // boundary logits bound to a compatibility context hash and an
    // isolation scope. A hybrid (DeltaNet) hit restores the recurrent
    // state alongside the KV blocks; a pure-attention bundle carries no
    // delta blob. Cross-scope or context-incompatible entries are never
    // served (§15/§22 — a mismatch is a miss, not best-effort reuse).
    const bool prefix_ok = true;
    const bool hybrid = cfg.has_linear_layers();
    const std::string& ctx_hash = prefix_ctx_hash();
    int64_t prefix_len = 0;
    size_t hit_index = prefix_cache_.size();
    for (size_t i = 0; prefix_ok && i < prefix_cache_.size(); ++i) {
        const PrefixEntry& entry = prefix_cache_[i];
        if (entry.scope_id != prefix_scope_ ||
            entry.ctx_sha256 != ctx_hash ||
            (hybrid && entry.delta_state.empty()))
            continue;
        const int64_t len = static_cast<int64_t>(entry.tokens.size());
        if (len > 0 && len <= static_cast<int64_t>(prompt_ids.size()) &&
            std::equal(
                entry.tokens.begin(), entry.tokens.end(), prompt_ids.begin()) &&
            len > prefix_len) {
            prefix_len = len;
            hit_index = i;
        }
    }
    if (hit_index != prefix_cache_.size()) {
        PrefixEntry& hit = prefix_cache_[hit_index];
        restore_prefix_state(hit);
        hit.tick = ++prefix_tick_;
        ++prefix_hits_;
    } else {
        ++prefix_misses_;
    }

    // Always forward at least the final prompt token so logits exist. On
    // a full-prefix hit the snapshot carries those logits — replaying
    // them keeps the restored KV verbatim (recomputing the boundary
    // token would read the dequantized prefix under KV-INT8 and overwrite
    // its K/V with ulp-drifted values, breaking bit-identical restore).
    std::vector<double> next_logits;
    const bool full_hit = hit_index != prefix_cache_.size() &&
        prefix_len == static_cast<int64_t>(prompt_ids.size());
    if (full_hit && !prefix_cache_[hit_index].logits.empty()) {
        next_logits = prefix_cache_[hit_index].logits;
    } else {
        int64_t forward_begin = prefix_len;
        int64_t forward_offset = prefix_len;
        if (forward_begin == static_cast<int64_t>(prompt_ids.size())) {
            forward_begin -= 1;
            forward_offset = prefix_len - 1;
            kv_lens_[0] = forward_offset;
        }
        std::vector<int64_t> suffix(
            prompt_ids.begin() + forward_begin, prompt_ids.end());
        next_logits =
            forward_last_logits(suffix, forward_offset, true);
    }

    // Snapshot the prompt prefix for future reuse (bounded, LRU-evicted).
    if (prefix_ok && prefix_cache_max_entries_ > 0 && kv_lens_[0] > 0) {
        PrefixEntry entry = capture_prefix_state(prompt_ids, next_logits);
        const int64_t entry_bytes =
            static_cast<int64_t>(
                entry.k.size() + entry.v.size() +
                entry.delta_state.size() +
                entry.logits.size() * sizeof(double));
        if (entry_bytes <= prefix_cache_max_bytes_) {
            auto existing = std::find_if(
                prefix_cache_.begin(), prefix_cache_.end(),
                [&](const PrefixEntry& entry) {
                    return entry.tokens == prompt_ids &&
                           entry.scope_id == prefix_scope_ &&
                           entry.ctx_sha256 == ctx_hash;
                });
            if (existing != prefix_cache_.end()) {
                existing->tick = ++prefix_tick_;
            } else {
                int64_t total_bytes = entry_bytes;
                for (const PrefixEntry& entry : prefix_cache_) {
                    total_bytes += static_cast<int64_t>(
                        entry.k.size() + entry.v.size() +
                        entry.delta_state.size() +
                        entry.logits.size() * sizeof(double));
                }
                while (
                    (!prefix_cache_.empty() &&
                     static_cast<int64_t>(prefix_cache_.size()) >=
                         prefix_cache_max_entries_) ||
                    (!prefix_cache_.empty() &&
                     total_bytes > prefix_cache_max_bytes_)) {
                    auto oldest = std::min_element(
                        prefix_cache_.begin(), prefix_cache_.end(),
                        [](const PrefixEntry& a, const PrefixEntry& b) {
                            return a.tick < b.tick;
                        });
                    total_bytes -= static_cast<int64_t>(
                        oldest->k.size() + oldest->v.size() +
                        oldest->delta_state.size() +
                        oldest->logits.size() * sizeof(double));
                    prefix_cache_.erase(oldest);
                }
                entry.tick = ++prefix_tick_;
                prefix_cache_.push_back(std::move(entry));
            }
        }
    }
    return decode_continue(
        std::move(next_logits), max_new_tokens, sampling, rng_state,
        generated);
}

NativeInferenceEngine::PrefixEntry
NativeInferenceEngine::capture_prefix_state(
    const std::vector<int64_t>& tokens,
    const std::vector<double>& logits) {
    // One boundary state = tokens + raw pool KV bytes + (hybrid) XSST
    // delta snapshot + post-boundary logits + compat context + scope.
    // Capturing the on-pool element format keeps a restored prefix
    // bit-identical under every storage format (KV-INT8 included).
    const ModelConfig& cfg = bundle_->config();
    const int64_t len = static_cast<int64_t>(tokens.size());
    const int64_t head_bytes = kv_elem_stride_bytes_;
    const int64_t row_bytes = cfg.num_key_value_heads * head_bytes;
    PrefixEntry entry;
    entry.tokens = tokens;
    entry.k.resize(static_cast<size_t>(
        cfg.num_hidden_layers * len * row_bytes));
    entry.v.resize(static_cast<size_t>(
        cfg.num_hidden_layers * len * row_bytes));
    for (int64_t layer = 0; layer < cfg.num_hidden_layers; ++layer) {
        // Linear (DeltaNet) layers own no KV — their context lives in
        // the delta snapshot; the kv pool has no rows to read there.
        if (cfg.is_linear_layer(layer)) continue;
        for (int64_t position = 0; position < len; ++position) {
            for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                const int64_t off =
                    (layer * len + position) * row_bytes +
                    h * head_bytes;
                std::memcpy(
                    entry.k.data() + off,
                    kv_slot_bytes(0, true, layer, position, h),
                    static_cast<size_t>(head_bytes));
                std::memcpy(
                    entry.v.data() + off,
                    kv_slot_bytes(0, false, layer, position, h),
                    static_cast<size_t>(head_bytes));
            }
        }
    }
    entry.logits = logits;
    if (cfg.has_linear_layers()) {
        entry.delta_state = snapshot_delta_state(
            bundle_->architecture_generation());
    }
    entry.ctx_sha256 = prefix_ctx_hash();
    entry.scope_id = prefix_scope_;
    return entry;
}

void NativeInferenceEngine::restore_prefix_state(
    const PrefixEntry& entry) {
    const ModelConfig& cfg = bundle_->config();
    const int64_t len = static_cast<int64_t>(entry.tokens.size());
    const int64_t head_bytes = kv_elem_stride_bytes_;
    const int64_t row_bytes = cfg.num_key_value_heads * head_bytes;
    const int64_t expect =
        cfg.num_hidden_layers * len * row_bytes;
    if (static_cast<int64_t>(entry.k.size()) != expect ||
        static_cast<int64_t>(entry.v.size()) != expect) {
        throw InferenceError("PREFIX_STATE_INCOMPATIBLE:kv-bytes");
    }
    for (int64_t position = 0; position < len; ++position) {
        kv_ensure_position(0, position);
    }
    for (int64_t layer = 0; layer < cfg.num_hidden_layers; ++layer) {
        if (cfg.is_linear_layer(layer)) continue;
        for (int64_t position = 0; position < len; ++position) {
            for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                const int64_t off =
                    (layer * len + position) * row_bytes +
                    h * head_bytes;
                kv_restore_bytes(
                    0, true, layer, position, h, entry.k.data() + off);
                kv_restore_bytes(
                    0, false, layer, position, h, entry.v.data() + off);
            }
        }
    }
    if (cfg.has_linear_layers()) {
        if (entry.delta_state.empty())
            throw InferenceError("DELTA_PREFIX_STATE_INVALID:empty");
        restore_delta_state(
            entry.delta_state, bundle_->architecture_generation());
    }
    kv_lens_[0] = len;
}

std::vector<int64_t> NativeInferenceEngine::decode_continue(
    std::vector<double> next_logits, int64_t max_new_tokens,
    const SamplingConfig& sampling, uint64_t rng_state,
    std::vector<int64_t>& generated) {
    const ModelConfig& cfg = bundle_->config();
    // Byte-spelled turn end: SFT weights terminate turns by emitting the
    // literal text "<|eot|>" (the bundle vocab carries no dedicated
    // token), so the token-id EOS alone never fires. Governed callers
    // truncate the visible reply at that marker — stopping here skips
    // the ramble the model would generate past turn end (saves decode
    // steps; the visible reply is unchanged).
    std::string turn_tail;
    turn_tail.reserve(64);
    for (int64_t step = 0; step < max_new_tokens; ++step) {
        const int64_t token = sample_next(
            next_logits, sequence_, sampling, rng_state);
        generated.push_back(token);
        sequence_.push_back(token);
        turn_tail += tokenizer_->decode({token}, false);
        if (turn_tail.size() > 64) {
            turn_tail.erase(0, turn_tail.size() - 64);
        }
        if (token == cfg.eos_token_id || step + 1 >= max_new_tokens ||
            (turn_tail.size() >= 7 &&
             turn_tail.compare(turn_tail.size() - 7, 7, "<|eot|>") == 0)) {
            break;
        }
        next_logits = forward_last_logits({token}, kv_lens_[0], true);
    }
    return generated;
}

// --- star-prefill-artifact/v1 (Prefill/Decode disaggregation) ---------
//
// Layout (little-endian):
//   "XPA1" u32 ver | u32 meta_len + meta JSON | u64 token_n + i64 ids |
//   u64 kv_n + k bytes | u64 kv_n + v bytes | u64 delta_n + XSST blob |
//   u64 logits_n + f64 logits | sha256(payload) hex 64B
// Tensor state travels as raw pool bytes — never JSON (§32).

std::string NativeInferenceEngine::prefill_artifact(
    const std::vector<int64_t>& prompt_ids,
    const std::string& request_id) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (prompt_ids.empty()) throw InferenceError("PROMPT_EMPTY");
    const ModelConfig& cfg = bundle_->config();
    if (static_cast<int64_t>(prompt_ids.size()) >
        cfg.max_position_embeddings) {
        throw InferenceError("SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS");
    }
    reset_cache();
    sequence_ = prompt_ids;
    const std::vector<double> logits =
        forward_last_logits(prompt_ids, 0, true);
    PrefixEntry entry = capture_prefix_state(prompt_ids, logits);

    std::ostringstream meta;
    meta << "{\"format\":\"star-prefill-artifact/v1\","
         << "\"request_id\":\"" << request_id << "\","
         << "\"generation\":\""
         << bundle_->architecture_generation() << "\","
         << "\"model_hash\":\"" << bundle_->weights_sha256() << "\","
         << "\"tokenizer_hash\":\"" << tokenizer_sha256_ << "\","
         << "\"sequence_length\":" << prompt_ids.size() << ","
         << "\"hybrid\":" << (cfg.has_linear_layers() ? 1 : 0) << ","
         << "\"kv_precision\":\"" << (kv_int8_ ? "int8" : "fp64")
         << "\"}";
    const std::string meta_json = meta.str();

    std::string out;
    auto put_u32 = [&out](uint32_t v) {
        out.append(reinterpret_cast<const char*>(&v), 4);
    };
    auto put_u64 = [&out](uint64_t v) {
        out.append(reinterpret_cast<const char*>(&v), 8);
    };
    out.append("XPA1", 4);
    put_u32(1);
    put_u32(static_cast<uint32_t>(meta_json.size()));
    out.append(meta_json);
    put_u64(static_cast<uint64_t>(entry.tokens.size()));
    for (int64_t t : entry.tokens) {
        out.append(reinterpret_cast<const char*>(&t), 8);
    }
    put_u64(static_cast<uint64_t>(entry.k.size()));
    if (!entry.k.empty())
        out.append(entry.k.data(), entry.k.size());
    put_u64(static_cast<uint64_t>(entry.v.size()));
    if (!entry.v.empty())
        out.append(entry.v.data(), entry.v.size());
    put_u64(static_cast<uint64_t>(entry.delta_state.size()));
    out.append(entry.delta_state);
    put_u64(static_cast<uint64_t>(entry.logits.size()));
    out.append(reinterpret_cast<const char*>(entry.logits.data()),
               entry.logits.size() * sizeof(double));
    out.append(sha256_hex(
        reinterpret_cast<const unsigned char*>(out.data()),
        out.size()));
    return out;
}

std::vector<int64_t> NativeInferenceEngine::generate_from_artifact(
    const std::string& artifact, int64_t max_new_tokens,
    const SamplingConfig& sampling) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (max_new_tokens <= 0) return {};
    const ModelConfig& cfg = bundle_->config();
    if (artifact.size() < 4 + 4 + 64 ||
        artifact.compare(0, 4, "XPA1") != 0)
        throw InferenceError("PREFILL_ARTIFACT_INVALID:magic");
    const std::string sha = sha256_hex(
        reinterpret_cast<const unsigned char*>(artifact.data()),
        artifact.size() - 64);
    if (artifact.compare(artifact.size() - 64, 64, sha) != 0)
        throw InferenceError("PREFILL_ARTIFACT_INVALID:hash");
    size_t pos = 4;
    auto rd_u32 = [&]() -> uint32_t {
        uint32_t v;
        std::memcpy(&v, artifact.data() + pos, 4);
        pos += 4;
        return v;
    };
    auto rd_u64 = [&]() -> uint64_t {
        uint64_t v;
        std::memcpy(&v, artifact.data() + pos, 8);
        pos += 8;
        return v;
    };
    const uint32_t version = rd_u32();
    if (version != 1)
        throw InferenceError("PREFILL_ARTIFACT_INVALID:version");
    const uint32_t meta_len = rd_u32();
    const std::string meta_json = artifact.substr(pos, meta_len);
    pos += meta_len;
    const JsonValue meta = JsonParser(meta_json).parse();
    auto jstr = [&](const char* key) -> std::string {
        const JsonValue* v = json_optional(meta, key);
        return v && v->type == JsonValue::Type::String
                   ? v->string : std::string();
    };
    if (jstr("format") != "star-prefill-artifact/v1")
        throw InferenceError("PREFILL_ARTIFACT_INVALID:format");
    if (jstr("generation") != bundle_->architecture_generation())
        throw InferenceError("PD_GENERATION_MISMATCH");
    if (jstr("model_hash") != bundle_->weights_sha256())
        throw InferenceError("PD_MODEL_MISMATCH");
    if (jstr("tokenizer_hash") != tokenizer_sha256_)
        throw InferenceError("PD_MODEL_MISMATCH:tokenizer");
    const bool meta_hybrid =
        json_number(meta, "hybrid") != 0.0;
    if (meta_hybrid != cfg.has_linear_layers())
        throw InferenceError("PREFILL_ARTIFACT_INVALID:arch");
    const bool meta_kv_int8 =
        jstr("kv_precision") == "int8";
    if (meta_kv_int8 != kv_int8_)
        throw InferenceError("PREFILL_ARTIFACT_INVALID:kv-precision");

    PrefixEntry entry;
    const uint64_t token_n = rd_u64();
    entry.tokens.resize(token_n);
    for (uint64_t i = 0; i < token_n; ++i) {
        int64_t t;
        std::memcpy(&t, artifact.data() + pos, 8);
        pos += 8;
        entry.tokens[i] = t;
    }
    const uint64_t k_n = rd_u64();
    entry.k.assign(artifact.data() + pos,
                   artifact.data() + pos + k_n);
    pos += k_n;
    const uint64_t v_n = rd_u64();
    entry.v.assign(artifact.data() + pos,
                   artifact.data() + pos + v_n);
    pos += v_n;
    const uint64_t d_n = rd_u64();
    entry.delta_state = artifact.substr(pos, d_n);
    pos += d_n;
    const uint64_t l_n = rd_u64();
    entry.logits.resize(l_n);
    if (l_n > 0) {
        std::memcpy(entry.logits.data(), artifact.data() + pos,
                    l_n * sizeof(double));
        pos += l_n * sizeof(double);
    }
    if (pos + 64 != artifact.size())
        throw InferenceError("PREFILL_ARTIFACT_INVALID:trailing");
    if (static_cast<int64_t>(entry.tokens.size()) +
            max_new_tokens > cfg.max_position_embeddings)
        throw InferenceError("SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS");
    if (entry.logits.empty())
        throw InferenceError("PREFILL_ARTIFACT_INVALID:logits");

    reset_cache();
    sequence_ = entry.tokens;
    restore_prefix_state(entry);
    // Decode resumes from the artifact's boundary logits — prefill is
    // never re-run (§27).
    std::vector<int64_t> generated;
    generated.reserve(static_cast<size_t>(max_new_tokens));
    uint64_t rng_state =
        sampling.seed ? sampling.seed : 0x9E3779B97F4A7C15ULL;
    return decode_continue(
        std::move(entry.logits), max_new_tokens, sampling, rng_state,
        generated);
}

std::vector<std::vector<int64_t>> NativeInferenceEngine::generate_batch(
    const std::vector<std::vector<int64_t>>& prompts,
    int64_t max_new_tokens,
    const SamplingConfig& sampling) {
    // R9 continuous batching: packed prefill over all prompts, then decode
    // steps pack every still-active sequence's token into one forward.
    // A sequence leaves the active set on EOS (or the shared step cap) and
    // its KV blocks return to the pool immediately — later sequences never
    // wait for earlier ones to finish.
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    if (prompts.empty()) return {};
    if (max_new_tokens <= 0) return {};
    if (static_cast<int64_t>(prompts.size()) > kMaxBatchSeqs) {
        throw InferenceError("BATCH_SIZE_EXCEEDED");
    }
    const ModelConfig& cfg = bundle_->config();
    reset_cache();

    struct SeqState {
        int64_t slot = -1;
        std::vector<int64_t> prompt;
        std::vector<int64_t> generated;
        std::vector<int64_t> context;
        std::vector<double> logits;
        std::string tail;
        bool done = false;
    };
    std::vector<SeqState> seqs(prompts.size());
    size_t allocated = 0;
    try {
        for (size_t i = 0; i < prompts.size(); ++i) {
            if (prompts[i].empty()) throw InferenceError("PROMPT_EMPTY");
            if (static_cast<int64_t>(prompts[i].size()) + max_new_tokens >
                cfg.max_position_embeddings) {
                throw InferenceError("SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS");
            }
            seqs[i].prompt = prompts[i];
            seqs[i].context = prompts[i];
            seqs[i].slot = kv_alloc_slot();
            ++allocated;
        }
    } catch (...) {
        for (size_t i = 0; i < allocated; ++i) kv_free_slot(seqs[i].slot);
        throw;
    }

    // Prefill: every sequence packs into one forward.
    {
        std::vector<BatchSpan> spans;
        spans.reserve(seqs.size());
        for (SeqState& seq : seqs) {
            BatchSpan span;
            span.slot = seq.slot;
            span.ids = &seq.prompt;
            span.position_offset = 0;
            span.append_cache = true;
            spans.push_back(span);
        }
        std::vector<std::vector<double>> logits =
            forward_batch_last_logits(spans);
        for (size_t i = 0; i < seqs.size(); ++i) {
            seqs[i].logits = std::move(logits[i]);
        }
    }

    const uint64_t base_seed =
        sampling.seed ? sampling.seed : 0x9E3779B97F4A7C15ULL;
    std::vector<uint64_t> rng(seqs.size());
    for (size_t i = 0; i < seqs.size(); ++i) {
        rng[i] = base_seed + static_cast<uint64_t>(i) * 0x9E3779B97F4A7C15ULL;
    }

    std::vector<int64_t> step_tokens(seqs.size());
    for (int64_t step = 0; step < max_new_tokens; ++step) {
        int64_t active = 0;
        for (size_t i = 0; i < seqs.size(); ++i) {
            SeqState& seq = seqs[i];
            if (seq.done) continue;
            const int64_t token =
                sample_next(seq.logits, seq.context, sampling, rng[i]);
            seq.generated.push_back(token);
            seq.context.push_back(token);
            step_tokens[i] = token;
            seq.tail += tokenizer_->decode({token}, false);
            if (seq.tail.size() > 64) {
                seq.tail.erase(0, seq.tail.size() - 64);
            }
            if (token == cfg.eos_token_id || step + 1 >= max_new_tokens ||
                (seq.tail.size() >= 7 &&
                 seq.tail.compare(seq.tail.size() - 7, 7, "<|eot|>") == 0)) {
                seq.done = true;
                kv_free_slot(seq.slot);  // continuous: release mid-batch
            } else {
                ++active;
            }
        }
        if (active == 0) break;
        // One-token span per active sequence — packed into a single forward.
        std::vector<std::vector<int64_t>> one_token(
            static_cast<size_t>(active), std::vector<int64_t>(1));
        std::vector<BatchSpan> spans;
        spans.reserve(static_cast<size_t>(active));
        size_t w = 0;
        for (SeqState& seq : seqs) {
            if (seq.done) continue;
            one_token[w][0] =
                step_tokens[static_cast<size_t>(&seq - seqs.data())];
            BatchSpan span;
            span.slot = seq.slot;
            span.ids = &one_token[w];
            span.position_offset = kv_lens_[static_cast<size_t>(seq.slot)];
            span.append_cache = true;
            spans.push_back(span);
            ++w;
        }
        std::vector<std::vector<double>> logits =
            forward_batch_last_logits(spans);
        w = 0;
        for (SeqState& seq : seqs) {
            if (seq.done) continue;
            seq.logits = std::move(logits[w++]);
        }
    }

    std::vector<std::vector<int64_t>> out(seqs.size());
    for (size_t i = 0; i < seqs.size(); ++i) {
        out[i] = std::move(seqs[i].generated);
        if (!seqs[i].done) kv_free_slot(seqs[i].slot);
    }
    return out;
}

std::string NativeInferenceEngine::generate_text(
    const std::string& prompt,
    int64_t max_new_tokens,
    const SamplingConfig& sampling) {
    return decode(generate(encode(prompt), max_new_tokens, sampling));
}

int64_t NativeInferenceEngine::kv_memory_bytes() const {
    return gptbridge_kv_pool_memory_bytes(kv_pool_);
}

int64_t NativeInferenceEngine::memory_bytes() const {
    int64_t lin_bytes = 0;
    for (const auto& slot_states : lin_states_) {
        for (const LinLayerState& st : slot_states) {
            lin_bytes += static_cast<int64_t>(
                (st.conv_tail.size() + st.s.size()) * sizeof(double));
        }
    }
    return (bundle_ ? bundle_->weights_bytes() : 0) +
           kv_memory_bytes() + lin_bytes;
}

void NativeInferenceEngine::set_kv_memory_limit(int64_t bytes) {
    kv_limit_bytes_ = std::max<int64_t>(0, bytes);
    if (kv_pool_ != nullptr &&
        !gptbridge_kv_pool_set_limit(kv_pool_, kv_limit_bytes_)) {
        throw InferenceError("KV_MEMORY_LIMIT_EXCEEDED");
    }
}

const std::string& NativeInferenceEngine::prefix_ctx_hash() {
    // §15/§16 CanonicalPrefixHash context: model weights hash +
    // tokenizer hash + manifest generation + KV precision profile.
    // Computed once per load; any component change invalidates every
    // cached entry (a ctx mismatch is always a miss, never a
    // best-effort reuse).
    if (prefix_ctx_sha256_.empty() && loaded()) {
        const ModelConfig& cfg = bundle_->config();
        std::string material = "star-prefix-ctx/v1";
        material += "|model=" + bundle_->weights_sha256();
        material += "|tok=" + tokenizer_sha256_;
        material += "|gen=" + bundle_->architecture_generation();
        material += "|arch=xc-fused-1";
        material += cfg.has_linear_layers() ? "|hybrid=1" : "|hybrid=0";
        material += kv_int8_ ? "|kv=int8" : "|kv=fp64";
        prefix_ctx_sha256_ = sha256_hex(
            reinterpret_cast<const unsigned char*>(material.data()),
            material.size());
    }
    return prefix_ctx_sha256_;
}

void NativeInferenceEngine::set_prefix_scope(
    const std::string& scope_id) {
    if (scope_id.empty())
        throw InferenceError("PREFIX_SCOPE_MISMATCH:empty-scope");
    if (scope_id.size() > 256)
        throw InferenceError("PREFIX_SCOPE_MISMATCH:scope-too-long");
    prefix_scope_ = scope_id;
}

int64_t NativeInferenceEngine::invalidate_prefix_scope(
    const std::string& scope_id) {
    // §19 revision invalidation: a changed PDF revision / chunk hash /
    // RAG index revision produces a new RagPrefixManifest hash, and the
    // caller drops the stale scope's entries outright — the old KV /
    // Delta state must never be served under an updated document set.
    int64_t removed = 0;
    auto it = prefix_cache_.begin();
    while (it != prefix_cache_.end()) {
        if (it->scope_id == scope_id) {
            it = prefix_cache_.erase(it);
            ++removed;
        } else {
            ++it;
        }
    }
    return removed;
}

void NativeInferenceEngine::set_prefix_cache_limit(
    int64_t max_entries, int64_t max_bytes) {
    prefix_cache_max_entries_ = std::max<int64_t>(0, max_entries);
    prefix_cache_max_bytes_ = std::max<int64_t>(0, max_bytes);
    while (static_cast<int64_t>(prefix_cache_.size()) >
               prefix_cache_max_entries_ &&
           !prefix_cache_.empty()) {
        auto oldest = std::min_element(
            prefix_cache_.begin(), prefix_cache_.end(),
            [](const PrefixEntry& a, const PrefixEntry& b) {
                return a.tick < b.tick;
            });
        prefix_cache_.erase(oldest);
    }
}

void NativeInferenceEngine::mem_note(int64_t seq_tokens) {
    int64_t total = (bundle_ ? bundle_->weights_bytes() : 0) +
                    kv_memory_bytes();
    for (const auto& slot_states : lin_states_) {
        for (const LinLayerState& st : slot_states) {
            total += static_cast<int64_t>(
                (st.conv_tail.size() + st.s.size()) * sizeof(double));
        }
    }
    if (seq_tokens > 1)
        mem_prefill_peak_ = std::max(mem_prefill_peak_, total);
    else
        mem_decode_peak_ = std::max(mem_decode_peak_, total);
}

NativeInferenceEngine::MemoryReport
NativeInferenceEngine::memory_report() const {
    MemoryReport r;
    r.weight_bytes = bundle_ ? bundle_->weights_bytes() : 0;
    r.kv_bytes = kv_memory_bytes();
    for (const PrefixEntry& e : prefix_cache_) {
        r.prefix_cache_bytes += static_cast<int64_t>(
            e.tokens.size() * sizeof(int64_t) +
            e.k.size() + e.v.size() +
            e.logits.size() * sizeof(double));
    }
    for (const auto& slot_states : lin_states_) {
        for (const LinLayerState& st : slot_states) {
            r.recurrent_state_bytes += static_cast<int64_t>(
                (st.conv_tail.size() + st.s.size()) * sizeof(double));
        }
    }
    r.vision_bytes = static_cast<int64_t>(
        vision_patch_proj_t_.size() * sizeof(double) +
        fs_.vision_prefix.capacity() * sizeof(double));
    // Workspace: capacity of the persistent scratch arena — the
    // buffers whose size the steady-state forward actually holds.
    auto cap = [](const std::vector<double>& v) {
        return static_cast<int64_t>(v.capacity() * sizeof(double));
    };
    r.workspace_bytes =
        cap(fs_.hidden) + cap(fs_.normed) + cap(fs_.qkv) +
        cap(fs_.attn_flat) + cap(fs_.attn_out) +
        cap(fs_.mlp_in) + cap(fs_.mlp_out) + cap(fs_.gate_up) +
        cap(fs_.moe_gate_up) + cap(fs_.moe_act) +
        cap(fs_.moe_grouped_out) + cap(fs_.lin_fused) +
        cap(fs_.lin_conv_in) + cap(fs_.lin_conv_pad) +
        cap(fs_.lin_o) + cap(fs_.lin_on);
    r.prefill_peak_bytes = mem_prefill_peak_;
    r.decode_peak_bytes = mem_decode_peak_;
    return r;
}

std::vector<double>
NativeInferenceEngine::recurrent_state_norms() const {
    std::vector<double> norms;
    if (lin_states_.empty()) return norms;
    const std::vector<LinLayerState>& slot0 = lin_states_[0];
    for (const LinLayerState& st : slot0) {
        if (st.s.empty()) continue;
        double sq = 0.0;
        for (double v : st.s) sq += v * v;
        norms.push_back(
            std::sqrt(sq / static_cast<double>(st.s.size())));
    }
    return norms;
}

// --- DeltaStateSnapshot (versioned, hashed, generation/model-bound) ---
//
// Layout (little-endian):
//   "XSST" u32 ver | u32 gen_len + gen bytes | u32 hash_len +
//   weights_sha256 bytes | u32 slot_count |
//   per slot: u32 layer_count { per layer: u64 conv_n + conv doubles |
//   u64 s_n + s doubles | i64 tokens } | sha256(payload) hex 64B
// KV / prefix cache are policy-evictable and never serialized here.

std::string NativeInferenceEngine::snapshot_delta_state(
    const std::string& generation) const {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    std::string out;
    auto put_u32 = [&out](uint32_t v) {
        out.append(reinterpret_cast<const char*>(&v), 4);
    };
    auto put_u64 = [&out](uint64_t v) {
        out.append(reinterpret_cast<const char*>(&v), 8);
    };
    auto put_i64 = [&out](int64_t v) {
        out.append(reinterpret_cast<const char*>(&v), 8);
    };
    auto put_doubles = [&](const std::vector<double>& v) {
        put_u64(static_cast<uint64_t>(v.size()));
        if (!v.empty())
            out.append(reinterpret_cast<const char*>(v.data()),
                       v.size() * sizeof(double));
    };
    out.append("XSST", 4);
    put_u32(1);
    put_u32(static_cast<uint32_t>(generation.size()));
    out.append(generation);
    const std::string model_hash =
        bundle_ ? bundle_->weights_sha256() : std::string();
    put_u32(static_cast<uint32_t>(model_hash.size()));
    out.append(model_hash);
    put_u32(static_cast<uint32_t>(lin_states_.size()));
    for (const auto& slot_states : lin_states_) {
        put_u32(static_cast<uint32_t>(slot_states.size()));
        for (const LinLayerState& st : slot_states) {
            put_doubles(st.conv_tail);
            put_doubles(st.s);
            put_i64(st.tokens);
        }
    }
    const std::string sha = sha256_hex(
        reinterpret_cast<const unsigned char*>(out.data()),
        out.size());
    out.append(sha);
    return out;
}

std::string NativeInferenceEngine::delta_state_sha256(
    const std::string& blob) {
    if (blob.size() < 4 + 4 + 64 ||
        blob.compare(0, 4, "XSST") != 0)
        throw InferenceError("SEQUENCE_STATE_INVALID:magic");
    return sha256_hex(
        reinterpret_cast<const unsigned char*>(blob.data()),
        blob.size() - 64);
}

void NativeInferenceEngine::restore_delta_state(
    const std::string& blob, const std::string& generation) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    const std::string sha = delta_state_sha256(blob);
    if (blob.compare(blob.size() - 64, 64, sha) != 0)
        throw InferenceError("SEQUENCE_STATE_INVALID:hash");
    size_t pos = 4;  // skip "XSST" magic
    auto rd_u32 = [&blob, &pos]() -> uint32_t {
        uint32_t v;
        std::memcpy(&v, blob.data() + pos, 4);
        pos += 4;
        return v;
    };
    auto rd_u64 = [&blob, &pos]() -> uint64_t {
        uint64_t v;
        std::memcpy(&v, blob.data() + pos, 8);
        pos += 8;
        return v;
    };
    auto rd_i64 = [&blob, &pos]() -> int64_t {
        int64_t v;
        std::memcpy(&v, blob.data() + pos, 8);
        pos += 8;
        return v;
    };
    uint32_t version = 0;
    pos = 4;
    version = rd_u32();
    if (version != 1)
        throw InferenceError("SEQUENCE_STATE_INVALID:version");
    const uint32_t gen_len = rd_u32();
    const std::string gen = blob.substr(pos, gen_len);
    pos += gen_len;
    if (gen != generation)
        throw InferenceError("STATE_GENERATION_MISMATCH");
    const uint32_t hash_len = rd_u32();
    const std::string model_hash = blob.substr(pos, hash_len);
    pos += hash_len;
    const std::string current =
        bundle_ ? bundle_->weights_sha256() : std::string();
    if (model_hash != current)
        throw InferenceError("STATE_MODEL_MISMATCH");
    const uint32_t slot_count = rd_u32();
    std::vector<std::vector<LinLayerState>> states(slot_count);
    for (uint32_t s = 0; s < slot_count; ++s) {
        const uint32_t layer_count = rd_u32();
        states[s].resize(layer_count);
        for (uint32_t l = 0; l < layer_count; ++l) {
            LinLayerState& st = states[s][l];
            const uint64_t conv_n = rd_u64();
            st.conv_tail.resize(conv_n);
            if (conv_n > 0) {
                std::memcpy(st.conv_tail.data(), blob.data() + pos,
                            conv_n * sizeof(double));
                pos += conv_n * sizeof(double);
            }
            const uint64_t s_n = rd_u64();
            st.s.resize(s_n);
            if (s_n > 0) {
                std::memcpy(st.s.data(), blob.data() + pos,
                            s_n * sizeof(double));
                pos += s_n * sizeof(double);
            }
            st.tokens = rd_i64();
        }
    }
    if (pos + 64 != blob.size())
        throw InferenceError("SEQUENCE_STATE_INVALID:trailing");
    lin_states_ = std::move(states);
    // Slot bookkeeping must match: ensure vectors parallel to
    // lin_states_ are at least as wide.
    while (kv_block_tables_.size() < lin_states_.size())
        kv_alloc_slot();
}

std::string NativeInferenceEngine::describe() const {
    if (!loaded()) return "{\"loaded\":false}";
    const ModelConfig& cfg = bundle_->config();
    std::ostringstream out;
    out << "{\"loaded\":true,"
        << "\"schema\":\"star-native-inference-engine/v1\","
        << "\"vocab_size\":" << cfg.vocab_size << ","
        << "\"hidden_size\":" << cfg.hidden_size << ","
        << "\"layers\":" << cfg.num_hidden_layers << ","
        << "\"heads\":" << cfg.num_attention_heads << ","
        << "\"kv_heads\":" << cfg.num_key_value_heads << ","
        << "\"weights_bytes\":" << bundle_->weights_bytes() << ","
        << "\"kv_memory_bytes\":" << kv_memory_bytes() << ","
        << "\"prefix_cache_entries\":"
        << static_cast<int64_t>(prefix_cache_.size()) << ","
        << "\"prefix_cache_hits\":" << prefix_hits_ << ","
        << "\"prefix_cache_misses\":" << prefix_misses_ << "}";
    return out.str();
}

std::string parse_generated_output(const std::string& text, int64_t max_json_bytes) {
    if (max_json_bytes <= 0) max_json_bytes = 64 * 1024;
    const std::string open = "<tool_call>";
    const std::string close = "</tool_call>";
    const size_t start = text.find(open);
    if (start == std::string::npos) {
        return "{\"schema\":\"star-inference-output/v1\",\"text\":\"" +
            json_escape(text) + "\",\"tool_call\":null}";
    }
    const size_t json_start = start + open.size();
    const size_t end = text.find(close, json_start);
    if (end == std::string::npos) {
        throw InferenceError("TOOL_CALL_UNCLOSED");
    }
    const std::string payload = text.substr(json_start, end - json_start);
    if (static_cast<int64_t>(payload.size()) > max_json_bytes) {
        throw InferenceError("TOOL_CALL_JSON_TOO_LARGE");
    }
    try {
        JsonParser(payload).parse();  // bounded, fail-closed validation
    } catch (const InferenceError&) {
        throw InferenceError("TOOL_CALL_JSON_INVALID");
    }
    std::string cleaned = text.substr(0, start) + text.substr(end + close.size());
    const size_t eot = cleaned.find("<|eot|>");
    if (eot != std::string::npos) cleaned.erase(eot);
    return "{\"schema\":\"star-inference-output/v1\",\"text\":\"" +
        json_escape(cleaned) + "\",\"tool_call\":" + payload + "}";
}

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
    // v27 fused hybrid: linear-attention layers fold context into the
    // per-slot DeltaNet state (S + conv tail) that a KV-only snapshot
    // cannot reconstruct — a hit would silently serve wrong-context
    // outputs, so the cache is bypassed for hybrid bundles entirely.
    const bool prefix_ok = !cfg.has_linear_layers();
    int64_t prefix_len = 0;
    size_t hit_index = prefix_cache_.size();
    for (size_t i = 0; prefix_ok && i < prefix_cache_.size(); ++i) {
        const PrefixEntry& entry = prefix_cache_[i];
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
        for (int64_t position = 0; position < prefix_len; ++position) {
            kv_ensure_position(0, position);
        }
        // Raw-byte restore: the snapshot holds the on-pool element
        // format, so the bytes land verbatim — under KV-INT8 this is the
        // difference between a bit-identical restore and a requantized
        // (ulp-drifted) prefix.
        const int64_t head_bytes = kv_elem_stride_bytes_;
        const int64_t row_bytes =
            cfg.num_key_value_heads * head_bytes;
        for (int64_t layer = 0; layer < cfg.num_hidden_layers; ++layer) {
            for (int64_t position = 0; position < prefix_len; ++position) {
                for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                    const int64_t off =
                        (layer * prefix_len + position) * row_bytes +
                        h * head_bytes;
                    kv_restore_bytes(
                        0, true, layer, position, h, hit.k.data() + off);
                    kv_restore_bytes(
                        0, false, layer, position, h, hit.v.data() + off);
                }
            }
        }
        hit.tick = ++prefix_tick_;
        ++prefix_hits_;
        kv_lens_[0] = prefix_len;
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
        const int64_t store_len = kv_lens_[0];
        const int64_t head_bytes = kv_elem_stride_bytes_;
        const int64_t entry_bytes =
            2 * cfg.num_hidden_layers * store_len *
                cfg.num_key_value_heads * head_bytes +
            static_cast<int64_t>(next_logits.size()) *
                static_cast<int64_t>(sizeof(double));
        if (entry_bytes <= prefix_cache_max_bytes_) {
            auto existing = std::find_if(
                prefix_cache_.begin(), prefix_cache_.end(),
                [&](const PrefixEntry& entry) {
                    return entry.tokens == prompt_ids;
                });
            if (existing != prefix_cache_.end()) {
                existing->tick = ++prefix_tick_;
            } else {
                int64_t total_bytes = entry_bytes;
                for (const PrefixEntry& entry : prefix_cache_) {
                    total_bytes += static_cast<int64_t>(
                        entry.k.size() + entry.v.size() +
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
                        oldest->logits.size() * sizeof(double));
                    prefix_cache_.erase(oldest);
                }
                PrefixEntry entry;
                entry.tokens = prompt_ids;
                // Snapshot the raw pool bytes (one element per
                // layer/position/head): fp64 vectors verbatim, or the
                // packed int8 payload + scale under KV-INT8 — restoring
                // the stored representation keeps hits bit-identical.
                const int64_t row_bytes =
                    cfg.num_key_value_heads * head_bytes;
                entry.k.resize(
                    static_cast<size_t>(
                        cfg.num_hidden_layers * store_len * row_bytes));
                entry.v.resize(
                    static_cast<size_t>(
                        cfg.num_hidden_layers * store_len * row_bytes));
                for (int64_t layer = 0; layer < cfg.num_hidden_layers; ++layer) {
                    for (int64_t position = 0; position < store_len; ++position) {
                        for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                            const int64_t off =
                                (layer * store_len + position) * row_bytes +
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
                entry.logits = next_logits;
                entry.tick = ++prefix_tick_;
                prefix_cache_.push_back(std::move(entry));
            }
        }
    }
    // Byte-spelled turn end: SFT weights terminate turns by emitting the
    // literal text "<|eot|>" (the bundle vocab carries no dedicated token),
    // so the token-id EOS alone never fires. Governed callers truncate the
    // visible reply at that marker — stopping here skips the ramble the
    // model would generate past turn end (saves decode steps; the visible
    // reply is unchanged).
    std::string turn_tail;
    turn_tail.reserve(64);
    const bool dbg = std::getenv("XC_DBG") != nullptr;
    for (int64_t step = 0; step < max_new_tokens; ++step) {
        if (dbg) {
            double s = 0.0;
            for (double x : next_logits) s += x;
            std::fprintf(stderr,
                "[dbg] step=%lld len=%lld lsum=%.9g l0=%.9g\n",
                (long long)step, (long long)kv_lens_[0], s,
                next_logits.empty() ? 0.0 : next_logits[0]);
        }
        const int64_t token = sample_next(next_logits, sequence_, sampling, rng_state);
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

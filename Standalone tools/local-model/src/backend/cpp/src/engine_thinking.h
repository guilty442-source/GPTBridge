// engine_thinking.h — fragment of engine.cpp (native thinking lane).
// Included once by engine.cpp inside namespace xingcheng::inference.
//
// Latent-space reasoning (Native Thinking):
//   1. prompt prefill on slot 0
//   2. N continuous-thought steps — the last hidden state is fed back as
//      the next input embedding (BatchSpan::embed_override); the thought
//      never detokenizes, so hypotheses form inside latent space
//   3. K parallel hypothesis branches fork the KV state onto private
//      slots and decode with independent sampling
//   4. CoVe self-verification: each branch is scored by mean token
//      logprob (the model's own confidence over its hypothesis); the
//      highest-confidence branch supplies the final answer
// All bounds are fail-closed (think_steps<=32, branches<=8, slots<=64).
#pragma once

namespace {
// log p(tok | logits row) — numerically stable log-softmax element.
double token_logprob(const std::vector<double>& logits, int64_t tok) {
    const double mx = *std::max_element(logits.begin(), logits.end());
    double se = 0.0;
    for (const double x : logits) se += std::exp(x - mx);
    return logits[static_cast<size_t>(tok)] - mx - std::log(se);
}
}  // namespace

std::vector<double> NativeInferenceEngine::forward_hidden_span(
    const BatchSpan& span) {
    return forward_batch_hidden({span});
}

std::vector<double> NativeInferenceEngine::forward_last_logits_span(
    const BatchSpan& span) {
    const std::vector<double> hidden = forward_batch_hidden({span});
    const ModelConfig& cfg = bundle_->config();
    const int64_t rows = static_cast<int64_t>(span.ids->size());
    const double* last = hidden.data() +
        static_cast<size_t>((rows - 1) * cfg.hidden_size);
    std::vector<double> logits =
        matmul(last, 1, cfg.hidden_size, lm_head_t_.data(), cfg.vocab_size);
    logit_softcap(logits, cfg.final_logit_softcapping);
    return logits;
}

void NativeInferenceEngine::kv_copy_slot(
    int64_t dst, int64_t src, int64_t count) {
    const ModelConfig& cfg = bundle_->config();
    if (dst < 0 || src < 0 ||
        dst >= static_cast<int64_t>(kv_block_tables_.size()) ||
        src >= static_cast<int64_t>(kv_block_tables_.size()) ||
        !kv_slot_active_[static_cast<size_t>(dst)] ||
        !kv_slot_active_[static_cast<size_t>(src)]) {
        throw InferenceError("KV_SLOT_INVALID");
    }
    std::vector<double> row(static_cast<size_t>(cfg.max_head_dim()));
    for (int64_t pos = 0; pos < count; ++pos) {
        kv_ensure_position(dst, pos);
        for (int64_t layer = 0; layer < cfg.num_hidden_layers; ++layer) {
            for (int64_t h = 0; h < cfg.num_key_value_heads; ++h) {
                kv_read_head(src, true, layer, pos, h, row.data());
                kv_write(dst, true, layer, pos, h, row.data());
                kv_read_head(src, false, layer, pos, h, row.data());
                kv_write(dst, false, layer, pos, h, row.data());
            }
        }
    }
    kv_lens_[static_cast<size_t>(dst)] = count;
}

NativeInferenceEngine::ThinkingResult
NativeInferenceEngine::generate_thinking(
    const std::vector<int64_t>& prompt_ids,
    int64_t think_steps,
    int64_t branches,
    int64_t max_new_tokens,
    const SamplingConfig& sampling) {
    if (!loaded()) throw InferenceError("ENGINE_NOT_LOADED");
    const ModelConfig& cfg = bundle_->config();
    if (prompt_ids.empty()) throw InferenceError("INPUT_EMPTY");
    if (think_steps < 0 || think_steps > 32) {
        throw InferenceError("THINK_STEPS_OUT_OF_BOUNDS");
    }
    if (branches < 1 || branches > 8) {
        throw InferenceError("THINK_BRANCHES_OUT_OF_BOUNDS");
    }
    if (max_new_tokens <= 0 ||
        static_cast<int64_t>(prompt_ids.size()) + think_steps +
            max_new_tokens > cfg.max_position_embeddings) {
        throw InferenceError("SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS");
    }
    reset_cache();
    sequence_ = prompt_ids;
    ThinkingResult result;
    result.think_steps = think_steps;
    result.branch_scores.assign(static_cast<size_t>(branches), 0.0);
    result.branch_ids.assign(static_cast<size_t>(branches), {});

    // Prefill once on slot 0: KV written, last hidden row seeds the first
    // continuous thought.
    std::vector<double> last_thought;
    {
        const std::vector<double> hidden =
            forward_hidden(prompt_ids, 0, true);
        last_thought.assign(
            hidden.end() - cfg.hidden_size, hidden.end());
    }

    // Latent iterations: each step feeds the previous hidden row back as
    // the input embedding and occupies one KV position.
    std::vector<int64_t> latent_marker{0};
    for (int64_t step = 0; step < think_steps; ++step) {
        BatchSpan span;
        span.slot = 0;
        span.ids = &latent_marker;
        span.position_offset = kv_lens_[0];
        span.append_cache = true;
        span.embed_override = &last_thought;
        const std::vector<double> hidden = forward_hidden_span(span);
        last_thought.assign(hidden.begin(), hidden.end());
    }

    // Final-thought logits seed every branch's first sampled token.
    std::vector<double> seed_logits = matmul(
        last_thought.data(), 1, cfg.hidden_size, lm_head_t_.data(),
        cfg.vocab_size);
    logit_softcap(seed_logits, cfg.final_logit_softcapping);

    uint64_t rng_state =
        sampling.seed ? sampling.seed : 0x9E3779B97F4A7C15ULL;
    std::vector<int64_t> slots(static_cast<size_t>(branches), -1);
    try {
        for (int64_t b = 0; b < branches; ++b) {
            const int64_t slot = kv_alloc_slot();
            slots[static_cast<size_t>(b)] = slot;
            kv_copy_slot(slot, 0, kv_lens_[0]);

            std::vector<int64_t> previous = prompt_ids;
            uint64_t rng_b = rng_state +
                0x9E3779B97F4A7C15ULL * static_cast<uint64_t>(b + 1);
            double lp_sum = 0.0;
            int64_t tok = sample_next(
                seed_logits, previous, sampling, rng_b);
            previous.push_back(tok);
            result.branch_ids[static_cast<size_t>(b)].push_back(tok);
            lp_sum += token_logprob(seed_logits, tok);

            while (static_cast<int64_t>(
                       result.branch_ids[static_cast<size_t>(b)].size()) <
                       max_new_tokens &&
                   tok != cfg.eos_token_id) {
                std::vector<int64_t> feed{tok};
                BatchSpan span;
                span.slot = slot;
                span.ids = &feed;
                span.position_offset = kv_lens_[static_cast<size_t>(slot)];
                span.append_cache = true;
                const std::vector<double> logits =
                    forward_last_logits_span(span);
                tok = sample_next(logits, previous, sampling, rng_b);
                previous.push_back(tok);
                result.branch_ids[static_cast<size_t>(b)].push_back(tok);
                lp_sum += token_logprob(logits, tok);
            }
            const double count = static_cast<double>(
                result.branch_ids[static_cast<size_t>(b)].size());
            result.branch_scores[static_cast<size_t>(b)] =
                count > 0 ? lp_sum / count : 0.0;
        }
    } catch (...) {
        for (const int64_t slot : slots) {
            if (slot >= 0) kv_free_slot(slot);
        }
        throw;
    }
    for (const int64_t slot : slots) kv_free_slot(slot);

    // CoVe verdict: the branch the model itself was most confident about.
    int64_t best = 0;
    for (int64_t b = 1; b < branches; ++b) {
        if (result.branch_scores[static_cast<size_t>(b)] >
            result.branch_scores[static_cast<size_t>(best)]) {
            best = b;
        }
    }
    result.chosen_branch = best;
    result.answer_ids = result.branch_ids[static_cast<size_t>(best)];
    return result;
}

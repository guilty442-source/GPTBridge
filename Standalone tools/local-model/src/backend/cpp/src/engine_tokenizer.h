// engine_tokenizer.h — ByteLevelBPETokenizer implementation, delegating
// to the Rust-owned tokenizer through the xtok/v1 C ABI (xtok_abi.h,
// xcorpus.dll). B81: tokenizer is a Rust capability; this TU owns only
// the handle plumbing. Fail-closed: no xcorpus.dll or a failed symbol
// resolution means TOKENIZER_BACKEND_UNAVAILABLE — the retired in-process
// C++ tokenizer is gone and never silently re-enters the lane.
// Included once by engine.cpp inside namespace xingcheng::inference.
#pragma once

#include "xtok_abi.h"

ByteLevelBPETokenizer::~ByteLevelBPETokenizer() {
    if (xtok_ != nullptr && xtok::available()) {
        xtok::api().free(xtok_);
        xtok_ = nullptr;
    }
}

ByteLevelBPETokenizer::ByteLevelBPETokenizer(
    ByteLevelBPETokenizer&& o) noexcept : xtok_(o.xtok_) {
    o.xtok_ = nullptr;
}

ByteLevelBPETokenizer& ByteLevelBPETokenizer::operator=(
    ByteLevelBPETokenizer&& o) noexcept {
    if (this != &o) {
        if (xtok_ != nullptr && xtok::available()) xtok::api().free(xtok_);
        xtok_ = o.xtok_;
        o.xtok_ = nullptr;
    }
    return *this;
}

ByteLevelBPETokenizer ByteLevelBPETokenizer::load(
    const std::string& tokenizer_json_path) {
    if (!xtok::available()) {
        // Rust owner lane absent — fail closed, never degrade to a
        // private tokenizer.
        throw InferenceError("TOKENIZER_BACKEND_UNAVAILABLE:xcorpus.dll");
    }
    ByteLevelBPETokenizer t;
    t.xtok_ = xtok::api().load(
        reinterpret_cast<const uint8_t*>(tokenizer_json_path.data()),
        tokenizer_json_path.size());
    if (t.xtok_ == nullptr) {
        throw InferenceError("TOKENIZER_LOAD_FAILED");
    }
    return t;
}

std::vector<int64_t> ByteLevelBPETokenizer::encode(
    const std::string& text,
    bool add_bos,
    bool add_eos,
    int64_t max_length) const {
    if (xtok_ == nullptr) throw InferenceError("TOKENIZER_HANDLE_NULL");
    const int64_t need = xtok::api().encode(
        xtok_, reinterpret_cast<const uint8_t*>(text.data()),
        text.size(), add_bos ? 1 : 0, add_eos ? 1 : 0, max_length,
        nullptr, 0);
    if (need < 0) throw InferenceError("TOKENIZER_ENCODE_FAILED");
    std::vector<int64_t> ids(static_cast<size_t>(need));
    const int64_t got = xtok::api().encode(
        xtok_, reinterpret_cast<const uint8_t*>(text.data()),
        text.size(), add_bos ? 1 : 0, add_eos ? 1 : 0, max_length,
        ids.data(), ids.size());
    if (got < 0 || got != need) {
        throw InferenceError("TOKENIZER_ENCODE_FAILED");
    }
    return ids;
}

std::string ByteLevelBPETokenizer::decode(
    const std::vector<int64_t>& ids,
    bool skip_special) const {
    if (xtok_ == nullptr) throw InferenceError("TOKENIZER_HANDLE_NULL");
    const int64_t need = xtok::api().decode(
        xtok_, ids.data(), ids.size(), skip_special ? 1 : 0,
        nullptr, 0);
    if (need < 0) throw InferenceError("TOKENIZER_DECODE_FAILED");
    std::string bytes(static_cast<size_t>(need), '\0');
    const int64_t got = xtok::api().decode(
        xtok_, ids.data(), ids.size(), skip_special ? 1 : 0,
        reinterpret_cast<uint8_t*>(bytes.data()), bytes.size());
    if (got < 0 || got != need) {
        throw InferenceError("TOKENIZER_DECODE_FAILED");
    }
    return bytes;
}

int64_t ByteLevelBPETokenizer::vocab_size() const {
    if (xtok_ == nullptr) throw InferenceError("TOKENIZER_HANDLE_NULL");
    const int64_t n = xtok::api().vocab_size(xtok_);
    if (n < 0) throw InferenceError("TOKENIZER_VOCAB_FAILED");
    return n;
}

// ── Engine (P3d–P3f) ──────────────────────────────────────────────────

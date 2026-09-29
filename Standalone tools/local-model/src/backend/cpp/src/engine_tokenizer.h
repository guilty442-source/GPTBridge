// engine_tokenizer.h — B94 fragment of engine.cpp (ByteLevelBPETokenizer).
// Included once by engine.cpp inside namespace xingcheng::inference.
#pragma once

ByteLevelBPETokenizer ByteLevelBPETokenizer::load(const std::string& tokenizer_json_path) {
    ByteLevelBPETokenizer tokenizer;
    const JsonValue root = JsonParser(read_text(tokenizer_json_path, 64 * 1024 * 1024)).parse();
    const JsonValue& model = json_field(root, "model");
    const JsonValue& vocab = json_field(model, "vocab");
    if (vocab.type != JsonValue::Type::Object) {
        throw InferenceError("TOKENIZER_VOCAB_INVALID");
    }
    int64_t max_id = -1;
    for (const auto& [token, id_json] : vocab.object) {
        if (id_json.type != JsonValue::Type::Number || id_json.number < 0 ||
            std::floor(id_json.number) != id_json.number) {
            throw InferenceError("TOKENIZER_VOCAB_ID_INVALID");
        }
        const int64_t id = static_cast<int64_t>(id_json.number);
        tokenizer.vocab_.emplace(token, id);
        max_id = std::max(max_id, id);
    }
    tokenizer.vocab_size_ = max_id + 1;
    tokenizer.id_to_token_.assign(static_cast<size_t>(tokenizer.vocab_size_), "");
    for (const auto& [token, id] : tokenizer.vocab_) {
        tokenizer.id_to_token_[static_cast<size_t>(id)] = token;
    }

    const JsonValue* merges = json_optional(model, "merges");
    if (merges != nullptr) {
        if (merges->type != JsonValue::Type::Array) {
            throw InferenceError("TOKENIZER_MERGES_INVALID");
        }
        for (const JsonValue& item : merges->array) {
            std::pair<std::string, std::string> pair;
            if (item.type == JsonValue::Type::String) {
                const size_t split = item.string.find(' ');
                if (split == std::string::npos || split == 0 ||
                    split + 1 >= item.string.size()) {
                    throw InferenceError("TOKENIZER_MERGE_FORMAT_INVALID");
                }
                pair = std::make_pair(
                    item.string.substr(0, split),
                    item.string.substr(split + 1));
            } else if (
                item.type == JsonValue::Type::Array && item.array.size() == 2 &&
                item.array[0].type == JsonValue::Type::String &&
                item.array[1].type == JsonValue::Type::String) {
                // HF tokenizers serializes merges as ["left","right"] pairs.
                pair = std::make_pair(item.array[0].string, item.array[1].string);
            } else {
                throw InferenceError("TOKENIZER_MERGE_INVALID");
            }
            tokenizer.merge_rank_.emplace(
                pair.first + "\x1f" + pair.second,
                static_cast<int64_t>(tokenizer.merges_.size()));
            tokenizer.merges_.push_back(std::move(pair));
        }
    }

    std::vector<int> printable;
    for (int b = 33; b <= 126; ++b) printable.push_back(b);
    for (int b = 161; b <= 172; ++b) printable.push_back(b);
    for (int b = 174; b <= 255; ++b) printable.push_back(b);
    std::unordered_set<int> printable_set(printable.begin(), printable.end());
    tokenizer.byte_to_token_.assign(256, "");
    int extra = 0;
    for (int b = 0; b < 256; ++b) {
        uint32_t cp = static_cast<uint32_t>(b);
        if (!printable_set.count(b)) {
            cp = static_cast<uint32_t>(256 + extra++);
        }
        append_utf8(tokenizer.byte_to_token_[static_cast<size_t>(b)], cp);
        tokenizer.token_to_byte_[tokenizer.byte_to_token_[static_cast<size_t>(b)]] =
            static_cast<unsigned char>(b);
    }

    const std::vector<std::string> specials = {
        "<|pad|>", "<|bos|>", "<|eos|>", "<|unk|>", "<|system|>",
        "<|user|>", "<|assistant|>", "<|tool|>", "<|eot|>",
    };
    for (const std::string& text : specials) {
        const auto it = tokenizer.vocab_.find(text);
        if (it != tokenizer.vocab_.end()) {
            tokenizer.special_tokens_.push_back({text, it->second});
            tokenizer.special_ids_.insert(it->second);
        }
    }
    std::sort(
        tokenizer.special_tokens_.begin(), tokenizer.special_tokens_.end(),
        [](const SpecialToken& a, const SpecialToken& b) { return a.text.size() > b.text.size(); });
    return tokenizer;
}

std::vector<int64_t> ByteLevelBPETokenizer::encode(
    const std::string& text,
    bool add_bos,
    bool add_eos,
    int64_t max_length) const {
    std::vector<int64_t> ids;
    if (add_bos) ids.push_back(1);

    size_t pos = 0;
    while (pos < text.size()) {
        const SpecialToken* matched = nullptr;
        for (const SpecialToken& special : special_tokens_) {
            if (text.compare(pos, special.text.size(), special.text) == 0) {
                matched = &special;
                break;
            }
        }
        if (matched != nullptr) {
            ids.push_back(matched->id);
            pos += matched->text.size();
            continue;
        }
        size_t end = pos;
        while (end < text.size()) {
            bool is_special = false;
            for (const SpecialToken& special : special_tokens_) {
                if (text.compare(end, special.text.size(), special.text) == 0) {
                    is_special = true;
                    break;
                }
            }
            if (is_special) break;
            ++end;
        }
        const std::vector<int64_t> segment = encode_segment(text.substr(pos, end - pos));
        ids.insert(ids.end(), segment.begin(), segment.end());
        pos = end;
    }

    if (add_eos) ids.push_back(2);
    if (max_length > 0 && static_cast<int64_t>(ids.size()) > max_length) {
        ids.resize(static_cast<size_t>(max_length));
        if (add_eos && !ids.empty()) ids.back() = 2;
    }
    return ids;
}

std::vector<int64_t> ByteLevelBPETokenizer::encode_segment(const std::string& text) const {
    std::vector<std::string> pieces;
    pieces.reserve(text.size());
    for (const unsigned char byte : text) {
        pieces.push_back(byte_to_token_[byte]);
    }
    while (pieces.size() > 1) {
        int64_t best_rank = std::numeric_limits<int64_t>::max();
        size_t best_index = pieces.size();
        for (size_t i = 0; i + 1 < pieces.size(); ++i) {
            const auto it = merge_rank_.find(pieces[i] + "\x1f" + pieces[i + 1]);
            if (it != merge_rank_.end() && it->second < best_rank) {
                best_rank = it->second;
                best_index = i;
            }
        }
        if (best_index == pieces.size()) break;
        pieces[best_index] += pieces[best_index + 1];
        pieces.erase(pieces.begin() + static_cast<std::ptrdiff_t>(best_index + 1));
    }
    std::vector<int64_t> ids;
    ids.reserve(pieces.size());
    for (const std::string& piece : pieces) {
        const auto it = vocab_.find(piece);
        ids.push_back(it == vocab_.end() ? 3 : it->second);
    }
    return ids;
}

std::string ByteLevelBPETokenizer::decode(
    const std::vector<int64_t>& ids,
    bool skip_special) const {
    const std::unordered_set<int64_t>& special_ids = special_ids_;
    std::string bytes;
    for (const int64_t id : ids) {
        if (id < 0 || id >= static_cast<int64_t>(id_to_token_.size())) {
            if (!skip_special) bytes.push_back(' ');
            continue;
        }
        const std::string& token = id_to_token_[static_cast<size_t>(id)];
        if (skip_special && special_ids.count(id)) continue;
        for (size_t i = 0; i < token.size();) {
            const unsigned char lead = static_cast<unsigned char>(token[i]);
            size_t width = 1;
            if ((lead & 0xE0) == 0xC0) width = 2;
            else if ((lead & 0xF0) == 0xE0) width = 3;
            else if ((lead & 0xF8) == 0xF0) width = 4;
            if (i + width > token.size()) {
                bytes.push_back(' ');
                break;
            }
            const std::string unit = token.substr(i, width);
            const auto it = token_to_byte_.find(unit);
            if (it != token_to_byte_.end()) {
                bytes.push_back(static_cast<char>(it->second));
            } else {
                bytes.append(unit);
            }
            i += width;
        }
    }
    return sanitize_utf8(bytes);
}

// ── Engine (P3d–P3f) ──────────────────────────────────────────────────

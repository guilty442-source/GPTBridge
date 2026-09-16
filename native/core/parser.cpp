// parser.cpp — parsing compute core (A221/E186).
//
// Owns parsing compute (token estimation, text analysis).
// Python owns all memory; C++ borrows raw pointers + length.
// No C++ exceptions cross the ABI boundary (noexcept).
// No unbounded allocation; no per-request thread pool.
#include "parser.hpp"

#include <cctype>

namespace gptbridge_native_parser {

namespace {

// Inline classification: is this char a word char (alnum or underscore)?
inline bool is_word_char(unsigned char c) noexcept {
    return (c >= 'a' && c <= 'z') ||
           (c >= 'A' && c <= 'Z') ||
           (c >= '0' && c <= '9') ||
           (c == '_');
}

// Inline classification: is this char whitespace?
inline bool is_space_char(unsigned char c) noexcept {
    return c == ' ' || c == '\t' || c == '\n' ||
           c == '\r' || c == '\v' || c == '\f';
}

}  // namespace

int64_t token_estimate(const char* text, int64_t text_len) noexcept {
    if (text == nullptr || text_len <= 0) {
        return 0;
    }

    int64_t count = 0;
    bool in_word = false;
    bool in_punct = false;

    for (int64_t i = 0; i < text_len; ++i) {
        unsigned char c = static_cast<unsigned char>(text[i]);

        if (is_word_char(c)) {
            if (!in_word) {
                ++count;  // start of a new word token
                in_word = true;
            }
            in_punct = false;
        } else if (is_space_char(c)) {
            in_word = false;
            in_punct = false;
        } else {
            // Punctuation: each non-word, non-space char is its own token
            if (!in_punct) {
                ++count;
            } else {
                // Consecutive punctuation chars are separate tokens
                // (matching the regex [^\w\s] which matches each char)
                ++count;
            }
            in_word = false;
            in_punct = true;
        }
    }

    return count;
}

int batch_token_estimate(
    const char* const* texts_ptr,
    const int64_t* text_lens,
    int64_t count,
    int64_t* results_ptr) noexcept {
    if (texts_ptr == nullptr || text_lens == nullptr ||
        results_ptr == nullptr || count <= 0) {
        return 1;  // invalid arguments
    }

    for (int64_t i = 0; i < count; ++i) {
        results_ptr[i] = token_estimate(texts_ptr[i], text_lens[i]);
    }

    return 0;  // success
}

}  // namespace gptbridge_native_parser

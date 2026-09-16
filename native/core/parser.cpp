// parser.cpp — parsing compute core (A221/E186).
//
// Owns parsing compute (token estimation, text analysis).
// Python owns all memory; C++ borrows raw pointers + length.
// No C++ exceptions cross the ABI boundary (noexcept).
// No unbounded allocation; no per-request thread pool.
#include "parser.hpp"
#include "memory.hpp"

#include <cctype>

namespace gptbridge_native_parser {

namespace mem = gptbridge_native_mem;

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
    // text: BORROWED_READONLY — Python owns the UTF-8 buffer.
    const auto view = mem::borrow_const(text, text_len);
    if (!view.valid() || text_len <= 0) {
        return 0;
    }

    int64_t count = 0;
    bool in_word = false;

    for (int64_t i = 0; i < text_len; ++i) {
        unsigned char c = static_cast<unsigned char>(text[i]);

        if (is_word_char(c)) {
            if (!in_word) {
                ++count;  // start of a new word token
                in_word = true;
            }
        } else if (is_space_char(c)) {
            in_word = false;
        } else {
            // Punctuation: each non-word, non-space char is its own token
            // (matching the regex [^\w\s] which matches each char).
            ++count;
            in_word = false;
        }
    }

    return count;
}

int batch_token_estimate(
    const char* const* texts_ptr,
    const int64_t* text_lens,
    int64_t count,
    int64_t* results_ptr) noexcept {
    // texts_ptr/text_lens: BORROWED_READONLY;
    // results_ptr: CALLER_PROVIDED_OUTPUT.
    const auto out = mem::caller_output(results_ptr, count);
    if (texts_ptr == nullptr || text_lens == nullptr ||
        !out.valid() || count <= 0) {
        return 1;  // invalid arguments
    }

    for (int64_t i = 0; i < count; ++i) {
        results_ptr[i] = token_estimate(texts_ptr[i], text_lens[i]);
    }

    return 0;  // success
}

}  // namespace gptbridge_native_parser

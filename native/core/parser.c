/*
 * parser.c — parsing compute core (A221/E186).  Pure C23.
 *
 * Owns parsing compute (token estimation, text analysis).
 * Python owns all memory; C borrows raw pointers + length.
 * No unbounded allocation; no per-request thread pool.
 */
#include "parser.h"
#include "memory.h"

/* Inline classification: is this char a word char (alnum or underscore)? */
static int is_word_char(unsigned char c) {
    return (c >= 'a' && c <= 'z') ||
           (c >= 'A' && c <= 'Z') ||
           (c >= '0' && c <= '9') ||
           (c == '_');
}

/* Inline classification: is this char whitespace? */
static int is_space_char(unsigned char c) {
    return c == ' ' || c == '\t' || c == '\n' ||
           c == '\r' || c == '\v' || c == '\f';
}

int64_t gptbridge_native_parser_token_estimate(
    const char* text, int64_t text_len) {
    /* text: BORROWED_READONLY — Python owns the UTF-8 buffer. */
    const gptbridge_native_mem_const_view view =
        gptbridge_native_mem_borrow_const(text, text_len);
    int64_t count = 0;
    int in_word = 0;
    int64_t i;

    if (!gptbridge_native_mem_const_view_valid(view) || text_len <= 0) {
        return 0;
    }

    for (i = 0; i < text_len; ++i) {
        unsigned char c = (unsigned char)text[i];

        if (is_word_char(c)) {
            if (!in_word) {
                ++count;  /* start of a new word token */
                in_word = 1;
            }
        } else if (is_space_char(c)) {
            in_word = 0;
        } else {
            /* Punctuation: each non-word, non-space char is its own token
             * (matching the regex [^\w\s] which matches each char). */
            ++count;
            in_word = 0;
        }
    }

    return count;
}

int gptbridge_native_parser_batch_token_estimate(
    const char* const* texts_ptr,
    const int64_t* text_lens,
    int64_t count,
    int64_t* results_ptr) {
    /* texts_ptr/text_lens: BORROWED_READONLY;
     * results_ptr: CALLER_PROVIDED_OUTPUT. */
    const gptbridge_native_mem_out_view out =
        gptbridge_native_mem_caller_output(results_ptr, count);
    int64_t i;

    if (texts_ptr == NULL || text_lens == NULL ||
        !gptbridge_native_mem_mut_view_valid(out) || count <= 0) {
        return 1;  /* invalid arguments */
    }

    for (i = 0; i < count; ++i) {
        results_ptr[i] =
            gptbridge_native_parser_token_estimate(texts_ptr[i], text_lens[i]);
    }

    return 0;  /* success */
}

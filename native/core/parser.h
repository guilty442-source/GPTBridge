/*
 * parser.h — private header for parsing compute core (A221/E186).
 *
 * Private to native/core/.  Never application-facing; the pybind11
 * binding (_binding.cpp) includes this to call the compute functions.
 * Python owns all memory; C borrows raw pointers + length.
 * Pure C compute core — no exceptions exist at this layer.
 */
#ifndef GPTBRIDGE_NATIVE_PARSER_H
#define GPTBRIDGE_NATIVE_PARSER_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Count tokens (words + punctuation) in text.
 * Mirrors the Python regex r"\b\w+\b|[^\w\s]" token estimator.
 * Returns the token count; always succeeds. */
int64_t gptbridge_native_parser_token_estimate(
    const char* text, int64_t text_len);

/* Count tokens in a batch of texts.  texts_ptr points to an array of
 * const char* pointers, text_lens points to an array of lengths.
 * results_ptr points to a pre-allocated array of int64_t results.
 * Returns 0 on success, non-zero on invalid arguments. */
int gptbridge_native_parser_batch_token_estimate(
    const char* const* texts_ptr,
    const int64_t* text_lens,
    int64_t count,
    int64_t* results_ptr);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* GPTBRIDGE_NATIVE_PARSER_H */

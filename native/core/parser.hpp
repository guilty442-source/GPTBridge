// parser.hpp — private header for parsing compute core (A221/E186).
//
// Private to native/core/.  Never application-facing; the pybind11
// binding (_binding.cpp) includes this to call the compute functions.
// Python owns all memory; C++ borrows raw pointers + length.
//
// No C++ exceptions cross the ABI boundary (noexcept).
#ifndef GPTBRIDGE_NATIVE_PARSER_HPP
#define GPTBRIDGE_NATIVE_PARSER_HPP

#include <cstdint>
#include <cstddef>

namespace gptbridge_native_parser {

// Count tokens (words + punctuation) in text.
// Mirrors the Python regex r"\b\w+\b|[^\w\s]" token estimator.
// Returns the token count; always succeeds (noexcept).
int64_t token_estimate(const char* text, int64_t text_len) noexcept;

// Count tokens in a batch of texts.  texts_ptr points to an array of
// const char* pointers, text_lens points to an array of lengths.
// results_ptr points to a pre-allocated array of int64_t results.
// Returns 0 on success, non-zero on invalid arguments.
int batch_token_estimate(
    const char* const* texts_ptr,
    const int64_t* text_lens,
    int64_t count,
    int64_t* results_ptr) noexcept;

}  // namespace gptbridge_native_parser

#endif  // GPTBRIDGE_NATIVE_PARSER_HPP

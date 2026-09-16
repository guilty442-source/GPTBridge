// vector.cpp — vector compute core (A221/E186).
//
// Owns vector compute (dot product, similarity, norm).
// Python owns all memory; C++ borrows raw pointers + length.
// No C++ exceptions cross the ABI boundary (noexcept).
// No unbounded allocation; no per-request thread pool.
#include "vector.hpp"

#include <cmath>

namespace gptbridge_native_vector {

double dot(const double* a, const double* b, int64_t dim) noexcept {
    if (a == nullptr || b == nullptr || dim <= 0) {
        return 0.0;
    }

    double result = 0.0;
    // Cache-friendly linear scan; compiler may auto-vectorize.
    for (int64_t i = 0; i < dim; ++i) {
        result += a[i] * b[i];
    }
    return result;
}

double l2_norm(const double* a, int64_t dim) noexcept {
    if (a == nullptr || dim <= 0) {
        return 0.0;
    }

    double sum_sq = 0.0;
    for (int64_t i = 0; i < dim; ++i) {
        const double v = a[i];
        sum_sq += v * v;
    }
    return std::sqrt(sum_sq);
}

double cosine_similarity(
    const double* a, const double* b, int64_t dim) noexcept {
    if (a == nullptr || b == nullptr || dim <= 0) {
        return 0.0;
    }

    double dot_val = 0.0;
    double norm_a = 0.0;
    double norm_b = 0.0;

    // Single pass: dot + both norms (cache-friendly, one scan)
    for (int64_t i = 0; i < dim; ++i) {
        const double va = a[i];
        const double vb = b[i];
        dot_val += va * vb;
        norm_a += va * va;
        norm_b += vb * vb;
    }

    norm_a = std::sqrt(norm_a);
    norm_b = std::sqrt(norm_b);

    if (norm_a == 0.0 || norm_b == 0.0) {
        return 0.0;
    }

    return dot_val / (norm_a * norm_b);
}

int batch_dot(
    const double* const* a_ptrs,
    const double* const* b_ptrs,
    int64_t count,
    int64_t dim,
    double* results) noexcept {
    if (a_ptrs == nullptr || b_ptrs == nullptr ||
        results == nullptr || count <= 0 || dim <= 0) {
        return 1;  // invalid arguments
    }

    for (int64_t i = 0; i < count; ++i) {
        results[i] = dot(a_ptrs[i], b_ptrs[i], dim);
    }

    return 0;  // success
}

}  // namespace gptbridge_native_vector

// vector.cpp — vector compute core (A221/E186).
//
// Owns vector compute (dot product, similarity, norm).
// Python owns all memory; C++ borrows raw pointers + length.
// No C++ exceptions cross the ABI boundary (noexcept).
// No unbounded allocation; no per-request thread pool.
#include "vector.hpp"
#include "memory.hpp"

#include <cmath>

namespace gptbridge_native_vector {

namespace mem = gptbridge_native_mem;

double dot(const double* a, const double* b, int64_t dim) noexcept {
    // BORROWED_READONLY x2 — Python owns both vectors.
    const auto va = mem::borrow_const(a, dim);
    const auto vb = mem::borrow_const(b, dim);
    if (!va.valid() || !vb.valid() || dim <= 0) {
        return 0.0;
    }

    // 4-way unrolled accumulators: breaks the FP dependency chain so the
    // compiler can exploit FMA/ILP without global fast-math reassociation.
    double acc0 = 0.0, acc1 = 0.0, acc2 = 0.0, acc3 = 0.0;
    int64_t i = 0;
    for (; i + 4 <= dim; i += 4) {
        acc0 += a[i] * b[i];
        acc1 += a[i + 1] * b[i + 1];
        acc2 += a[i + 2] * b[i + 2];
        acc3 += a[i + 3] * b[i + 3];
    }
    for (; i < dim; ++i) {
        acc0 += a[i] * b[i];
    }
    return (acc0 + acc1) + (acc2 + acc3);
}

double l2_norm(const double* a, int64_t dim) noexcept {
    if (a == nullptr || dim <= 0) {
        return 0.0;
    }

    double acc0 = 0.0, acc1 = 0.0, acc2 = 0.0, acc3 = 0.0;
    int64_t i = 0;
    for (; i + 4 <= dim; i += 4) {
        const double v0 = a[i], v1 = a[i + 1], v2 = a[i + 2], v3 = a[i + 3];
        acc0 += v0 * v0;
        acc1 += v1 * v1;
        acc2 += v2 * v2;
        acc3 += v3 * v3;
    }
    for (; i < dim; ++i) {
        acc0 += a[i] * a[i];
    }
    return std::sqrt((acc0 + acc1) + (acc2 + acc3));
}

double cosine_similarity(
    const double* a, const double* b, int64_t dim) noexcept {
    if (a == nullptr || b == nullptr || dim <= 0) {
        return 0.0;
    }

    // Single pass: dot + both norms (cache-friendly, 4-way unrolled).
    double dot0 = 0.0, dot1 = 0.0, dot2 = 0.0, dot3 = 0.0;
    double na0 = 0.0, na1 = 0.0, na2 = 0.0, na3 = 0.0;
    double nb0 = 0.0, nb1 = 0.0, nb2 = 0.0, nb3 = 0.0;

    int64_t i = 0;
    for (; i + 4 <= dim; i += 4) {
        const double a0 = a[i], a1 = a[i + 1], a2 = a[i + 2], a3 = a[i + 3];
        const double b0 = b[i], b1 = b[i + 1], b2 = b[i + 2], b3 = b[i + 3];
        dot0 += a0 * b0;
        dot1 += a1 * b1;
        dot2 += a2 * b2;
        dot3 += a3 * b3;
        na0 += a0 * a0;
        na1 += a1 * a1;
        na2 += a2 * a2;
        na3 += a3 * a3;
        nb0 += b0 * b0;
        nb1 += b1 * b1;
        nb2 += b2 * b2;
        nb3 += b3 * b3;
    }
    for (; i < dim; ++i) {
        const double va = a[i];
        const double vb = b[i];
        dot0 += va * vb;
        na0 += va * va;
        nb0 += vb * vb;
    }

    const double dot_val = (dot0 + dot1) + (dot2 + dot3);
    const double norm_a = std::sqrt((na0 + na1) + (na2 + na3));
    const double norm_b = std::sqrt((nb0 + nb1) + (nb2 + nb3));

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
    // a_ptrs/b_ptrs: BORROWED_READONLY pointer arrays;
    // results: CALLER_PROVIDED_OUTPUT (caller allocated, we write).
    const auto out = mem::caller_output(results, count);
    if (a_ptrs == nullptr || b_ptrs == nullptr ||
        !out.valid() || count <= 0 || dim <= 0) {
        return 1;  // invalid arguments
    }

    for (int64_t i = 0; i < count; ++i) {
        results[i] = dot(a_ptrs[i], b_ptrs[i], dim);
    }

    return 0;  // success
}

}  // namespace gptbridge_native_vector

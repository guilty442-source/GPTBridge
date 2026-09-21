/*
 * vector.h — private header for vector compute core (A221/E186).
 *
 * Private to native/core/.  Never application-facing; the pybind11
 * binding (_binding.cpp) includes this to call the compute functions.
 * Python owns all memory; C borrows raw pointers + length.
 * Pure C compute core — no exceptions exist at this layer.
 */
#ifndef GPTBRIDGE_NATIVE_VECTOR_H
#define GPTBRIDGE_NATIVE_VECTOR_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Dot product of two float vectors.
 * Returns 0.0 if dims mismatch or pointers are null. */
double gptbridge_native_vector_dot(
    const double* a, const double* b, int64_t dim);

/* L2 norm of a float vector.
 * Returns 0.0 if pointer is null or dim <= 0. */
double gptbridge_native_vector_l2_norm(const double* a, int64_t dim);

/* Cosine similarity of two float vectors.
 * Returns 0.0 if dims mismatch, pointers are null, or either norm is 0. */
double gptbridge_native_vector_cosine_similarity(
    const double* a, const double* b, int64_t dim);

/* Batch dot product: N pairs of vectors.
 * a_ptrs and b_ptrs point to arrays of N pointers; each vector has dim
 * elements.  results points to a pre-allocated array of N doubles.
 * Returns 0 on success, non-zero on invalid arguments. */
int gptbridge_native_vector_batch_dot(
    const double* const* a_ptrs,
    const double* const* b_ptrs,
    int64_t count,
    int64_t dim,
    double* results);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* GPTBRIDGE_NATIVE_VECTOR_H */

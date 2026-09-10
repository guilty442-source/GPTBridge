/*
 * gptbridge_native.h — sole public C header and application-facing native
 * contract (A221/E186).
 *
 * Declares the canonical native interface for platform resource primitives.
 * The C bridge (native/bridge/gptbridge_native.c) implements these functions.
 * Python pybind11 bindings call through this header; they do not duplicate
 * bridge logic (A220/E185).
 */
#ifndef GPTBRIDGE_NATIVE_H
#define GPTBRIDGE_NATIVE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Whether the current platform is Windows. */
int gptbridge_native_is_windows(void);

/* High-resolution monotonic clock in fractional seconds. */
double gptbridge_native_monotonic_seconds(void);

/* Process working-set size in bytes, or -1 on error / unsupported. */
int64_t gptbridge_native_working_set_bytes(void);

/* Process private (non-shared) memory usage in bytes, or -1 on error. */
int64_t gptbridge_native_private_bytes(void);

/* Empty the process working set; returns 1 on success, 0 otherwise. */
int gptbridge_native_release_working_set(void);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* GPTBRIDGE_NATIVE_H */

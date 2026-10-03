#ifndef GPTBRIDGE_GIT_ENGINE_H
#define GPTBRIDGE_GIT_ENGINE_H

/* git-engine-native M0: stable C ABI for the native git plane successor.
 * Ownership: C++23 executes, C owns this contract. Fixed-width types only.
 * M0 scope is read-only probing; this header exposes no mutating verb. */

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* M0 contract version. Bumped only by a governed amendment. */
#define GE_VERSION_M0 0

/* Probe outcome. Truncation is reported inside ge_probe_t, never as error. */
typedef enum ge_status {
    GE_OK = 0,
    GE_BAD_PATH = 1,
    GE_GIT_MISSING = 2,
    GE_TIMEOUT = 3,
    GE_FAILED = 4
} ge_status_t;

/* Bounded worktree snapshot derived from `git status --porcelain=v1`. */
typedef struct ge_probe {
    int32_t clean;     /* 1 when zero dirty entries were observed */
    int32_t truncated; /* 1 when the output cap cut the listing short */
    int64_t dirty;     /* porcelain entries observed (capped) */
    int64_t staged;    /* entries with a staged (index) modification */
    int64_t untracked; /* `??` entries */
} ge_probe_t;

/* Output safety bound for one capture (bytes). */
#define GE_OUTPUT_CAP_BYTES 65536

/* M1 read-only verbs: diff summary and log tip (additive; still no writes). */
typedef struct ge_diff {
    int32_t truncated; /* 1 when the numstat listing hit the line cap */
    int64_t files;     /* paths with a numeric or binary delta */
    int64_t insertions;
    int64_t deletions;
} ge_diff_t;

typedef struct ge_log {
    int32_t truncated;  /* 1 when history exceeds the sampled depth */
    int64_t count;      /* commits observed within the sampled depth */
    char latest[41];    /* tip commit hash (40 hex + NUL), empty when none */
} ge_log_t;

/* Diff of worktree plus index against HEAD (`git diff HEAD --numstat`). */
ge_status_t ge_diff_summary(const wchar_t* dir_w, ge_diff_t* out, int64_t timeout_ms);

/* Tip history probe (`git log --format=%H`, bounded depth). */
ge_status_t ge_log_latest(const wchar_t* dir_w, ge_log_t* out, int64_t timeout_ms);

int32_t ge_version(void);

/* Read-only probe of one worktree directory.
 * dir_w: absolute directory path (wide). timeout_ms is clamped to
 * [1000, 120000]. Never writes, never stages, never locks the git plane. */
ge_status_t ge_probe_worktree(const wchar_t* dir_w, ge_probe_t* out, int64_t timeout_ms);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* GPTBRIDGE_GIT_ENGINE_H */

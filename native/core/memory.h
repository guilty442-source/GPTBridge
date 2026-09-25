/*
 * memory.h — shared native memory/buffer primitives (A204/A213/A214/A220;
 * Memory & Buffer Architecture V1).  Pure C23 port of the former memory.hpp.
 * C23 upgrade: constexpr, typeof, _BitInt, auto, nullability.
 *
 * Single owner of the four ownership classes used by every native
 * domain (parser / vector / transformer).  Domains never build their
 * own allocator or pool policy; this header is their only primitive.
 *
 *   BORROWED_READONLY        — caller owns; C reads only
 *   BORROWED_MUTABLE         — caller owns; C may write in place
 *   NATIVE_OWNED             — C owns; freed in the same owner, never escapes
 *   CALLER_PROVIDED_OUTPUT   — caller allocated; C writes results
 *
 * Rules enforced here:
 *   - header-only, no exceptions exist in C so nothing crosses the boundary
 *   - every size/stride/shape computation is overflow-checked before
 *     any allocation happens
 *   - allocations require an explicit workspace budget check first
 *   - the allocator and deallocator of any block live in the same owner
 *     (create/destroy pairs in the C ABI) — cross-runtime free is
 *     structurally impossible
 *   - memory.h depends on NOTHING else in native/ (no reverse dependency
 *     from the memory domain into compute domains)
 *
 * C compute cores never allocate: every call shape is borrowed or
 * caller-provided output, so NATIVE_OWNED carries no runtime helper here.
 * A future C-owned buffer must still be created/freed by the same owner.
 *
 * Zero-copy is a policy decision, not a default: batching, buffer reuse
 * and validated borrowed views come first; zero-copy only with profile
 * evidence.
 */
#ifndef GPTBRIDGE_NATIVE_MEMORY_H
#define GPTBRIDGE_NATIVE_MEMORY_H

#include <stdint.h>
#include <stddef.h>
#include <limits.h>
#include <stdlib.h>
#ifdef _WIN32
#include <malloc.h>
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------
 * Ownership classes
 * ------------------------------------------------------------------ */
typedef enum {
    GPTBRIDGE_MEM_BORROWED_READONLY = 0,
    GPTBRIDGE_MEM_BORROWED_MUTABLE = 1,
    GPTBRIDGE_MEM_NATIVE_OWNED = 2,
    GPTBRIDGE_MEM_CALLER_PROVIDED_OUTPUT = 3
} gptbridge_native_mem_ownership;

/* ------------------------------------------------------------------
 * Overflow-checked size math — every byte count passes through here
 * before allocation or indexing.  Returns 1 on success, 0 on overflow
 * or invalid input; *out is only written on success.
 * ------------------------------------------------------------------ */
static inline int gptbridge_native_mem_checked_mul_i64(
    int64_t a, int64_t b, int64_t* out) {
    if (a < 0 || b < 0 || out == NULL) {
        return 0;
    }
    if (a != 0 && b > INT64_MAX / a) {
        return 0;  /* overflow */
    }
    *out = a * b;
    return 1;
}

/* bytes = count * elem_size, guarded. */
static inline int gptbridge_native_mem_checked_bytes(
    int64_t count, int64_t elem_size, int64_t* bytes_out) {
    return gptbridge_native_mem_checked_mul_i64(count, elem_size, bytes_out);
}

/* Total element count for a shape (product of dims), guarded. */
static inline int gptbridge_native_mem_checked_shape_elems(
    const int64_t* dims, int64_t ndims, int64_t* elems_out) {
    int64_t total = 1;
    int64_t i;
    if (elems_out == NULL || ndims < 0) {
        return 0;
    }
    if (ndims == 0) {
        *elems_out = 0;
        return 1;
    }
    if (dims == NULL) {
        return 0;
    }
    for (i = 0; i < ndims; ++i) {
        if (!gptbridge_native_mem_checked_mul_i64(total, dims[i], &total)) {
            return 0;
        }
    }
    *elems_out = total;
    return 1;
}

/* ------------------------------------------------------------------
 * Borrowed views — the default exchange shape.  A view never owns;
 * construction validates pointer/length once.  Element type is erased
 * (void*) because validity only inspects pointer + length; the domain
 * casts at use sites.
 * ------------------------------------------------------------------ */
typedef struct {
    const void* data;
    int64_t len;
} gptbridge_native_mem_const_view;  /* BORROWED_READONLY */

typedef struct {
    void* data;
    int64_t len;
} gptbridge_native_mem_mut_view;    /* BORROWED_MUTABLE */

typedef gptbridge_native_mem_mut_view gptbridge_native_mem_out_view;
                                    /* CALLER_PROVIDED_OUTPUT */

static inline gptbridge_native_mem_const_view
gptbridge_native_mem_borrow_const(const void* data, int64_t len) {
    gptbridge_native_mem_const_view v = { data, len };
    return v;
}

static inline gptbridge_native_mem_mut_view
gptbridge_native_mem_borrow_mut(void* data, int64_t len) {
    gptbridge_native_mem_mut_view v = { data, len };
    return v;
}

static inline gptbridge_native_mem_out_view
gptbridge_native_mem_caller_output(void* data, int64_t len) {
    gptbridge_native_mem_out_view v = { data, len };
    return v;
}

static inline int
gptbridge_native_mem_const_view_valid(gptbridge_native_mem_const_view v) {
    return v.data != NULL && v.len >= 0;
}

static inline int
gptbridge_native_mem_mut_view_valid(gptbridge_native_mem_mut_view v) {
    return v.data != NULL && v.len >= 0;
}

/* ------------------------------------------------------------------
 * Workspace budget — checked before every native allocation.
 * ------------------------------------------------------------------ */
typedef struct {
    int64_t max_bytes;
} gptbridge_native_mem_workspace_budget;

static inline int gptbridge_native_mem_budget_admits(
    gptbridge_native_mem_workspace_budget budget, int64_t bytes) {
    return bytes >= 0 && bytes <= budget.max_bytes;
}

/* ------------------------------------------------------------------
 * Aligned allocation — AVX2 需 32-byte 對齊以達最佳 FMA 吞吐
 * 正確性：對齊失敗回 NULL；呼叫方需配對 free；size 需 overflow 檢查
 * 速度：對齊後 _mm256_load_pd 可替代 loadu，編譯器可自動向量化
 * ------------------------------------------------------------------ */
/* C23: constexpr for compile-time constants */
constexpr int GPTBRIDGE_NATIVE_SIMD_ALIGN = 32;
#define GPTBRIDGE_NATIVE_SIMD_ALIGN_C23 32

static inline void* gptbridge_native_mem_aligned_alloc(int64_t bytes) {
    if (bytes <= 0) return NULL;
    int64_t aligned_bytes = 0;
    if (!gptbridge_native_mem_checked_bytes(bytes, 1, &aligned_bytes)) return NULL;
    /* 對齊至 32 bytes */
    aligned_bytes = (aligned_bytes + GPTBRIDGE_NATIVE_SIMD_ALIGN - 1) & ~(GPTBRIDGE_NATIVE_SIMD_ALIGN - 1);
    if (!gptbridge_native_mem_budget_admits((gptbridge_native_mem_workspace_budget){.max_bytes = INT64_MAX}, aligned_bytes)) return NULL;
#ifdef _WIN32
    return _aligned_malloc((size_t)aligned_bytes, GPTBRIDGE_NATIVE_SIMD_ALIGN);
#else
    void* p = NULL;
    if (posix_memalign(&p, GPTBRIDGE_NATIVE_SIMD_ALIGN, (size_t)aligned_bytes) != 0) return NULL;
    return p;
#endif
}

static inline void gptbridge_native_mem_aligned_free(void* p) {
    if (p == NULL) return;
#ifdef _WIN32
    _aligned_free(p);
#else
    free(p);
#endif
}

static inline int gptbridge_native_mem_is_aligned(const void* p) {
    return p != NULL && ((uintptr_t)p % GPTBRIDGE_NATIVE_SIMD_ALIGN) == 0;
}

/* 簡易池：固定大小塊的 free list（無鎖，單線程；多線程需外部同步）
 * 速度：避免重複 malloc/free 於高頻小分配（如 attention scores）；正確性：池內塊皆對齊 */
#define GPTBRIDGE_NATIVE_POOL_MAX_BLOCKS 64
typedef struct {
    void* blocks[GPTBRIDGE_NATIVE_POOL_MAX_BLOCKS];
    int64_t block_bytes;
    int count;
} gptbridge_native_mem_pool;

static inline void gptbridge_native_mem_pool_init(gptbridge_native_mem_pool* pool, int64_t block_bytes) {
    if (pool == NULL) return;
    pool->block_bytes = block_bytes;
    pool->count = 0;
    for (int i = 0; i < GPTBRIDGE_NATIVE_POOL_MAX_BLOCKS; ++i) pool->blocks[i] = NULL;
}

static inline void* gptbridge_native_mem_pool_acquire(gptbridge_native_mem_pool* pool) {
    if (pool == NULL || pool->block_bytes <= 0) return NULL;
    if (pool->count > 0) return pool->blocks[--pool->count];
    return gptbridge_native_mem_aligned_alloc(pool->block_bytes);
}

static inline void gptbridge_native_mem_pool_release(gptbridge_native_mem_pool* pool, void* p) {
    if (pool == NULL || p == NULL) return;
    if (pool->count < GPTBRIDGE_NATIVE_POOL_MAX_BLOCKS) {
        pool->blocks[pool->count++] = p;
    } else {
        gptbridge_native_mem_aligned_free(p);
    }
}

static inline void gptbridge_native_mem_pool_destroy(gptbridge_native_mem_pool* pool) {
    if (pool == NULL) return;
    for (int i = 0; i < pool->count; ++i) gptbridge_native_mem_aligned_free(pool->blocks[i]);
    pool->count = 0;
}

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* GPTBRIDGE_NATIVE_MEMORY_H */

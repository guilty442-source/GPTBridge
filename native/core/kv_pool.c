/* C-owned KV block pool. Allocator and deallocator live in this C owner. */
#include "gptbridge_kv_pool.h"

#include <limits.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    double* key;
    double* value;
    int active;
} kv_block;

struct gptbridge_kv_pool {
    int64_t block_stride;
    int64_t limit_bytes;
    int64_t memory_bytes;
    int32_t capacity;
    int32_t count;
    int32_t* free_ids;
    int32_t free_count;
    kv_block* blocks;
};

static int checked_bytes(int64_t stride, int64_t* out) {
    if (stride <= 0 || stride > INT64_MAX / (2 * (int64_t)sizeof(double))) return 0;
    *out = stride * 2 * (int64_t)sizeof(double);
    return 1;
}

static int grow(gptbridge_kv_pool* pool) {
    if (pool->count < pool->capacity) return 1;
    int32_t next = pool->capacity > 0 ? pool->capacity * 2 : 8;
    if (next <= pool->capacity) return 0;
    kv_block* blocks = (kv_block*)realloc(pool->blocks, (size_t)next * sizeof(kv_block));
    if (!blocks) return 0;
    memset(blocks + pool->capacity, 0, (size_t)(next - pool->capacity) * sizeof(kv_block));
    pool->blocks = blocks;
    pool->capacity = next;
    return 1;
}

gptbridge_kv_pool* gptbridge_kv_pool_create(int64_t block_stride, int64_t limit_bytes) {
    int64_t block_bytes = 0;
    if (!checked_bytes(block_stride, &block_bytes) || limit_bytes < 0) return NULL;
    gptbridge_kv_pool* pool = (gptbridge_kv_pool*)calloc(1, sizeof(*pool));
    if (!pool) return NULL;
    pool->block_stride = block_stride;
    pool->limit_bytes = limit_bytes;
    return pool;
}

void gptbridge_kv_pool_destroy(gptbridge_kv_pool* pool) {
    if (!pool) return;
    for (int32_t i = 0; i < pool->count; ++i) {
        free(pool->blocks[i].key);
        free(pool->blocks[i].value);
    }
    free(pool->blocks);
    free(pool->free_ids);
    free(pool);
}

int gptbridge_kv_pool_set_limit(gptbridge_kv_pool* pool, int64_t limit_bytes) {
    if (!pool || limit_bytes < 0 || (limit_bytes > 0 && pool->memory_bytes > limit_bytes)) return 0;
    pool->limit_bytes = limit_bytes;
    return 1;
}

int32_t gptbridge_kv_pool_alloc(gptbridge_kv_pool* pool) {
    if (!pool) return -1;
    if (pool->free_count > 0) {
        int32_t id = pool->free_ids[--pool->free_count];
        pool->blocks[id].active = 1;
        memset(pool->blocks[id].key, 0, (size_t)pool->block_stride * sizeof(double));
        memset(pool->blocks[id].value, 0, (size_t)pool->block_stride * sizeof(double));
        return id;
    }
    int64_t block_bytes = pool->block_stride * 2 * (int64_t)sizeof(double);
    if (pool->limit_bytes > 0 && pool->memory_bytes > pool->limit_bytes - block_bytes) return -1;
    if (!grow(pool)) return -1;
    double* key = (double*)calloc((size_t)pool->block_stride, sizeof(double));
    double* value = (double*)calloc((size_t)pool->block_stride, sizeof(double));
    if (!key || !value) { free(key); free(value); return -1; }
    int32_t id = pool->count++;
    pool->blocks[id].key = key;
    pool->blocks[id].value = value;
    pool->blocks[id].active = 1;
    pool->memory_bytes += block_bytes;
    return id;
}

void gptbridge_kv_pool_release(gptbridge_kv_pool* pool, int32_t block_id) {
    if (!pool || block_id < 0 || block_id >= pool->count || !pool->blocks[block_id].active) return;
    pool->blocks[block_id].active = 0;
    int32_t* ids = (int32_t*)realloc(pool->free_ids, (size_t)(pool->free_count + 1) * sizeof(int32_t));
    if (ids) { pool->free_ids = ids; pool->free_ids[pool->free_count++] = block_id; }
}

double* gptbridge_kv_pool_data(gptbridge_kv_pool* pool, int32_t block_id, int key_cache) {
    if (!pool || block_id < 0 || block_id >= pool->count || !pool->blocks[block_id].active) return NULL;
    return key_cache ? pool->blocks[block_id].key : pool->blocks[block_id].value;
}

int64_t gptbridge_kv_pool_memory_bytes(const gptbridge_kv_pool* pool) {
    return pool ? pool->memory_bytes : 0;
}

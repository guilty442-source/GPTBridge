/* C-owned KV block pool ABI (G41/P3f memory ownership migration). */
#ifndef GPTBRIDGE_KV_POOL_H
#define GPTBRIDGE_KV_POOL_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct gptbridge_kv_pool gptbridge_kv_pool;

gptbridge_kv_pool* gptbridge_kv_pool_create(int64_t block_stride, int64_t limit_bytes);
void gptbridge_kv_pool_destroy(gptbridge_kv_pool* pool);
int gptbridge_kv_pool_set_limit(gptbridge_kv_pool* pool, int64_t limit_bytes);
int32_t gptbridge_kv_pool_alloc(gptbridge_kv_pool* pool);
void gptbridge_kv_pool_release(gptbridge_kv_pool* pool, int32_t block_id);
double* gptbridge_kv_pool_data(gptbridge_kv_pool* pool, int32_t block_id, int key_cache);
int64_t gptbridge_kv_pool_memory_bytes(const gptbridge_kv_pool* pool);

#ifdef __cplusplus
}
#endif

#endif

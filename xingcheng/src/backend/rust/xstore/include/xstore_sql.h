#ifndef GPTBRIDGE_XSTORE_SQL_H
#define GPTBRIDGE_XSTORE_SQL_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef struct XstoreSqlBuffer {
    const uint8_t *data;
    size_t length;
} XstoreSqlBuffer;
/* Input is caller-owned readable UTF-8 JSON. Max 1 MiB; no NUL required.
 * Output is engine-owned UTF-8 JSON, max 8 MiB. Read exactly length bytes.
 * A null output means allocation/lock failure or 16 outstanding buffers.
 * Release exactly once, using this engine's free function. Never libc free.
 * Schema: {"format":"xstore-native-sql-request/v1","store":"...",
 *          "sql":"SELECT ... WHERE column = $1","params":[...]}
 * Read-only: unsupported statements fail closed. No external SQL process. */
XstoreSqlBuffer xstore_sql_query_json(const uint8_t *data, size_t length);
int32_t xstore_sql_buffer_free(const uint8_t *data);
#ifdef __cplusplus
}
#endif
#endif

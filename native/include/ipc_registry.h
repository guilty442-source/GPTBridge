/* ipc_registry.h — E2 傳輸/註冊面 C 原型（ §10.65 E2 ）

對應 Python `ipc_server` + `backend_gateway_request_registry`：
  - IPC 傳輸：位元流路由（受治理本機傳輸，與 Python 治理主宰介接）
  - 請求登錄：request_id / backend_generation / status / cancellation

純 C11，無 C++，shadow 模式（Python 為權威，逐筆比對）。
*/
#ifndef GPTBRIDGE_IPC_REGISTRY_H
#define GPTBRIDGE_IPC_REGISTRY_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GPTBRIDGE_IPC_MAX_REQUESTS 128
#define GPTBRIDGE_IPC_ID_MAX 64

typedef enum {
    GPTBRIDGE_REQ_CREATED = 0,
    GPTBRIDGE_REQ_QUEUED = 1,
    GPTBRIDGE_REQ_RUNNING = 2,
    GPTBRIDGE_REQ_COMPLETED = 3,
    GPTBRIDGE_REQ_FAILED = 4,
    GPTBRIDGE_REQ_CANCELLED = 5,
    GPTBRIDGE_REQ_TIMED_OUT = 6,
    GPTBRIDGE_REQ_INTERRUPTED = 7
} gptbridge_req_status_t;

typedef struct {
    char request_id[GPTBRIDGE_IPC_ID_MAX];
    char backend_id[GPTBRIDGE_IPC_ID_MAX];
    int32_t backend_generation;
    gptbridge_req_status_t status;
    int64_t created_at_ms;
    int64_t started_at_ms;
    int64_t completed_at_ms;
    int64_t timeout_ms;
    int32_t cancelled; /* 0/1 */
} gptbridge_ipc_request_t;

typedef struct {
    gptbridge_ipc_request_t reqs[GPTBRIDGE_IPC_MAX_REQUESTS];
    int32_t count;
} gptbridge_ipc_registry_t;

int gptbridge_ipc_registry_init(gptbridge_ipc_registry_t* r);
int gptbridge_ipc_registry_create(gptbridge_ipc_registry_t* r, const char* request_id, int32_t generation, int64_t now_ms);
/* 對齊 Python _VALID_REQUEST_TRANSITIONS（嚴格有向圖）＋終止鎖。
   回 1 合法、0 非法轉移、-1 終止態鎖定。同態回 1（Python 跳過驗證）。 */
int gptbridge_ipc_registry_transition_ok(gptbridge_req_status_t from,
                                         gptbridge_req_status_t to);
/* set_status：嚴格轉移語義（Python update()）。
   回 1 成功；0 找不到；-1 終止態鎖定；-2 非法轉移；-3 未知狀態值。 */
int gptbridge_ipc_registry_set_status(gptbridge_ipc_registry_t* r, const char* request_id, gptbridge_req_status_t s, int64_t now_ms);
/* merge_status：Python upsert legacy 語義——無條件覆寫 status＋時間戳
   bookkeeping，不驗轉移表（持久化回放也用此路徑）。回 1/0。 */
int gptbridge_ipc_registry_merge_status(gptbridge_ipc_registry_t* r, const char* request_id, gptbridge_req_status_t s, int64_t now_ms);
/* request_cancel：Python request_cancel——僅立 cancelled 旗標，
   status 不變（實際 CANCELLED 由後續 set_status 走嚴格表）。回 1/0。 */
int gptbridge_ipc_registry_request_cancel(gptbridge_ipc_registry_t* r, const char* request_id);
int gptbridge_ipc_registry_set_backend(gptbridge_ipc_registry_t* r, const char* request_id, const char* backend_id);
int gptbridge_ipc_registry_cancel(gptbridge_ipc_registry_t* r, const char* request_id, int64_t now_ms); /* 冪等 */
int gptbridge_ipc_registry_set_timeout(gptbridge_ipc_registry_t* r, const char* request_id, int64_t timeout_ms);
/* deadline = created_at_ms + timeout_ms; 0 when no timeout was declared */
int64_t gptbridge_ipc_registry_deadline_ms(const gptbridge_ipc_registry_t* r, const char* request_id);
const gptbridge_ipc_request_t* gptbridge_ipc_registry_find(const gptbridge_ipc_registry_t* r, const char* request_id);
int gptbridge_ipc_registry_count(const gptbridge_ipc_registry_t* r);

/* IPC 傳輸 stub：與 runtime_core 的 ipc 互補，此處為請求級傳輸 */
typedef struct {
    char payload[512];
    uint64_t seq;
} gptbridge_ipc_transport_msg_t;

int gptbridge_ipc_transport_send(const gptbridge_ipc_transport_msg_t* msg);
int gptbridge_ipc_transport_recv(gptbridge_ipc_transport_msg_t* out);

#ifdef __cplusplus
}
#endif

#endif /* GPTBRIDGE_IPC_REGISTRY_H */

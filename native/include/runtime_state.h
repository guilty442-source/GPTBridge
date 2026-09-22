/* runtime_state.h — E3 啟動面：模組雙軸狀態登錄簿 C 原型（ §10.65 E3 ）

對應 Python `core_system/runtime_state_registry.py`（star-runtime-state/v1）
的決策自由執行語義：

  - 運行軸 7 態：STARTING/READY/DEGRADED/RECOVERING/FAILED/STOPPING/STOPPED
  - 能力軸 4 態：AVAILABLE/UNAVAILABLE/DISABLED/NOT_INSTALLED
  - RECOVERING → recovery_attempts 自增；READY/STARTING 清除 last_error
  - record_error 截斷 500 字元（Python `error[:500]`）
  - aggregate：雙軸計數＋排序 failed 清單，局部故障不擴散
  - 持久化（atomic JSON 寫盤）由呼叫方負責——C 層不做 I/O

純 C11，無 C++，無 I/O；時間戳字串由呼叫方注入。
shadow 模式：與 Python 並行比對，Python 為權威。
*/
#ifndef GPTBRIDGE_RUNTIME_STATE_H
#define GPTBRIDGE_RUNTIME_STATE_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GPTBRIDGE_RS_MAX_MODULES 64
#define GPTBRIDGE_RS_ID_MAX 64
#define GPTBRIDGE_RS_HEALTH_MAX 32
#define GPTBRIDGE_RS_RELEASE_MAX 64
#define GPTBRIDGE_RS_TIME_MAX 32
#define GPTBRIDGE_RS_ERR_MAX 512 /* Python record_error 截 500 + NUL 餘裕 */

typedef enum {
    GPTBRIDGE_RT_UNKNOWN = 0,
    GPTBRIDGE_RT_STARTING = 1,
    GPTBRIDGE_RT_READY = 2,
    GPTBRIDGE_RT_DEGRADED = 3,
    GPTBRIDGE_RT_RECOVERING = 4,
    GPTBRIDGE_RT_FAILED = 5,
    GPTBRIDGE_RT_STOPPING = 6,
    GPTBRIDGE_RT_STOPPED = 7
} gptbridge_runtime_state_t;

typedef enum {
    GPTBRIDGE_CAP_UNKNOWN = 0,
    GPTBRIDGE_CAP_AVAILABLE = 1,
    GPTBRIDGE_CAP_UNAVAILABLE = 2,
    GPTBRIDGE_CAP_DISABLED = 3,
    GPTBRIDGE_CAP_NOT_INSTALLED = 4
} gptbridge_capability_state_t;

typedef struct {
    char module_id[GPTBRIDGE_RS_ID_MAX];
    uint8_t runtime_state;    /* gptbridge_runtime_state_t */
    uint8_t capability_state; /* gptbridge_capability_state_t */
    char health[GPTBRIDGE_RS_HEALTH_MAX];
    char release_id[GPTBRIDGE_RS_RELEASE_MAX];
    char last_heartbeat[GPTBRIDGE_RS_TIME_MAX];
    char last_error[GPTBRIDGE_RS_ERR_MAX];
    char updated_at[GPTBRIDGE_RS_TIME_MAX];
    int32_t recovery_attempts;
    uint8_t in_use;
} gptbridge_rs_record_t;

typedef struct {
    gptbridge_rs_record_t records[GPTBRIDGE_RS_MAX_MODULES];
    int32_t count;
} gptbridge_rs_registry_t;

/* 名稱→枚舉（對齊 Python RUNTIME_STATES／CAPABILITY_STATES frozenset 校驗）；
   未知名稱回傳 *_UNKNOWN（=0），呼叫方應 fail-closed。 */
uint8_t gptbridge_rs_runtime_from_name(const char* name);
uint8_t gptbridge_rs_capability_from_name(const char* name);
const char* gptbridge_rs_runtime_name(uint8_t state);
const char* gptbridge_rs_capability_name(uint8_t state);

void gptbridge_rs_init(gptbridge_rs_registry_t* reg);

/* set_runtime_state：state 必為合法運行態（UNKNOWN→回 0 拒絕）。
   health/release/error 為 NULL 表示不更新（Python Optional 語義）。
   RECOVERING → recovery_attempts++；READY/STARTING 且 error==NULL →
   清 last_error。now_str 為呼叫方注入的 UTC 字串。回 1 成功。 */
int gptbridge_rs_set_runtime(gptbridge_rs_registry_t* reg,
                             const char* module_id,
                             uint8_t runtime_state,
                             const char* health,
                             const char* release_id,
                             const char* error,
                             const char* now_str);

int gptbridge_rs_set_capability(gptbridge_rs_registry_t* reg,
                                const char* module_id,
                                uint8_t capability_state,
                                const char* now_str);

int gptbridge_rs_heartbeat(gptbridge_rs_registry_t* reg,
                           const char* module_id,
                           const char* now_str);

/* last_error 截斷 500 字元（Python error[:500]） */
int gptbridge_rs_record_error(gptbridge_rs_registry_t* reg,
                              const char* module_id,
                              const char* error,
                              const char* now_str);

const gptbridge_rs_record_t* gptbridge_rs_find(
    const gptbridge_rs_registry_t* reg, const char* module_id);

/* aggregate：雙軸計數＋排序 failed 清單（局部故障不擴散）。
   by_runtime 長度 >=8（索引=枚舉值），by_capability >=5。
   failed_ids/failed_cap/failed_count 可為 NULL（僅計數）。回傳模組數。 */
int32_t gptbridge_rs_aggregate(const gptbridge_rs_registry_t* reg,
                               int32_t by_runtime[8],
                               int32_t by_capability[5],
                               char failed_ids[][GPTBRIDGE_RS_ID_MAX],
                               int32_t failed_cap,
                               int32_t* failed_count);

#ifdef __cplusplus
}
#endif

#endif /* GPTBRIDGE_RUNTIME_STATE_H */

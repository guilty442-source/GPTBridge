/* outbox.h — E1 執行面：state outbox 游標/窗口 C 原型（ §10.65 E1 ）

對應 Python `tasks/state_outbox.py` 的決策自由執行語義（A195 DELIVERY）：
  - per-session 游標：acked（客戶端 ack，單調）/ sent_upto / last_attempt
  - 有界投遞窗口 (acked, acked+WINDOW]；未 ack 逾 RETRY 後自 acked 重送
  - hello：跨 backend_generation 重置游標至 latest（不回放全歷史）
  - 期限驅動：next_retry_deadline 回傳最早重送期限（無固定輪詢）
  - prune floor：min(acked) 或 latest（無 session 時）

純 C11，無 C++，無 I/O；事件存取與實際投遞由呼叫方負責（SQLite 留 Python）。
shadow 模式：與 Python 並行比對，Python 為權威。
*/
#ifndef GPTBRIDGE_OUTBOX_H
#define GPTBRIDGE_OUTBOX_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GPTBRIDGE_OB_MAX_SESSIONS 32
#define GPTBRIDGE_OB_ID_MAX 64
#define GPTBRIDGE_OB_DELIVERY_WINDOW 200
#define GPTBRIDGE_OB_DRAIN_BATCH 100
#define GPTBRIDGE_OB_RETRY_MS 2000

typedef struct {
    char session_id[GPTBRIDGE_OB_ID_MAX];
    int64_t acked;
    int64_t sent_upto;
    int64_t last_attempt_ms;
} gptbridge_ob_session_t;

typedef struct {
    gptbridge_ob_session_t sessions[GPTBRIDGE_OB_MAX_SESSIONS];
    int32_t count;
} gptbridge_ob_registry_t;

int gptbridge_ob_init(gptbridge_ob_registry_t* r);
int gptbridge_ob_register(gptbridge_ob_registry_t* r, const char* session_id);
int gptbridge_ob_unregister(gptbridge_ob_registry_t* r, const char* session_id);

/* hello：generation 不符 → 游標重置至 latest_sequence（reset=1）；回傳生效游標或 -1 */
int64_t gptbridge_ob_hello(gptbridge_ob_registry_t* r,
                           const char* session_id,
                           int64_t cursor,
                           int32_t generation_matches,
                           int64_t latest_sequence,
                           int32_t* out_reset);

/* ack：單調不回退；回傳 1=前移，0=忽略/未知 */
int gptbridge_ob_ack(gptbridge_ob_registry_t* r, const char* session_id, int64_t cursor);

/* resync：游標重置（acked=sent_upto=cursor） */
int gptbridge_ob_resync(gptbridge_ob_registry_t* r, const char* session_id, int64_t cursor);

/* 投遞計畫：計算本次 fetch 範圍（start_after 與上限 limit）；0=無可送 */
int gptbridge_ob_drain_plan(gptbridge_ob_registry_t* r,
                            const char* session_id,
                            int64_t now_ms,
                            int64_t retry_ms,
                            int64_t* out_start_after,
                            int64_t* out_limit);

/* 標記已送至 seq（sent_upto 前移）＋ last_attempt 更新 */
int gptbridge_ob_mark_sent(gptbridge_ob_registry_t* r,
                           const char* session_id,
                           int64_t seq,
                           int64_t now_ms);

/* 最早重送期限（無 → 回傳 0，out 為 0） */
int gptbridge_ob_next_retry_deadline(const gptbridge_ob_registry_t* r,
                                     int64_t retry_ms,
                                     int64_t* out_deadline_ms);

/* prune floor：有 session → min(acked)；無 → latest_sequence */
int64_t gptbridge_ob_prune_floor(const gptbridge_ob_registry_t* r,
                                 int64_t latest_sequence);

const gptbridge_ob_session_t* gptbridge_ob_find(const gptbridge_ob_registry_t* r,
                                                const char* session_id);

#ifdef __cplusplus
}
#endif

#endif /* GPTBRIDGE_OUTBOX_H */

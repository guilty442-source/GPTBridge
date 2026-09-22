/* maintenance.h — E1 執行面：維護排程 tick C 原型（ §10.65 E1 ）

對應 Python `MaintenanceController`/`SchedulerConfig` 的決策自由執行語義：
  - tick 期限驅動（30s tick），有界佇列（max 100），job 年齡上限（3600s）
  - 准入等級：M0 恆入／M1 idle 才入／M2 需授權旗標／M3 僅候選（不自動執行）
  - 有界重試：max_attempts＋backoff（base×attempt）；generation 不匹配即拒
  - TTL 探針快取：同一 tick 共享一次探測結果（含失敗快取，fail-closed 不變）

純 C11，無 C++，無 I/O；實際維護動作由呼叫方 executor 注入。
shadow 模式：與 Python 並行比對，Python 為權威。
*/
#ifndef GPTBRIDGE_MAINTENANCE_H
#define GPTBRIDGE_MAINTENANCE_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GPTBRIDGE_MT_MAX_JOBS 100
#define GPTBRIDGE_MT_ID_MAX 64

typedef enum {
    GPTBRIDGE_MT_PLANNED = 0,
    GPTBRIDGE_MT_QUEUED = 1,
    GPTBRIDGE_MT_RUNNING = 2,
    GPTBRIDGE_MT_VERIFYING = 3,
    GPTBRIDGE_MT_DEFERRED = 4,
    GPTBRIDGE_MT_COMPLETED = 5,
    GPTBRIDGE_MT_FAILED = 6,
    GPTBRIDGE_MT_CANCELLED = 7  /* 准入後遭 Python 側下游閘門否決（budget/lease/cooldown） */
} gptbridge_mt_status_t;

typedef enum {
    GPTBRIDGE_MT_M0 = 0, /* 恆入 */
    GPTBRIDGE_MT_M1 = 1, /* 僅 idle */
    GPTBRIDGE_MT_M2 = 2, /* 需授權＋M1 健康閘 */
    GPTBRIDGE_MT_M3 = 3  /* 僅候選：不自動執行 */
} gptbridge_mt_class_t;

typedef struct {
    char job_id[GPTBRIDGE_MT_ID_MAX];
    char action_id[GPTBRIDGE_MT_ID_MAX];
    gptbridge_mt_class_t risk_class;
    int32_t priority;             /* 小值先排 */
    gptbridge_mt_status_t status;
    int64_t generation;
    int32_t attempt_count;
    int64_t scheduled_at_ms;
    int64_t next_attempt_ms;
} gptbridge_mt_job_t;

typedef struct {
    gptbridge_mt_job_t jobs[GPTBRIDGE_MT_MAX_JOBS];
    int32_t count;
    int64_t tick_interval_ms;
    int64_t max_job_age_ms;
    int32_t max_retry_attempts;
    int64_t retry_backoff_ms;
    int64_t last_tick_ms;
    int64_t current_generation;
} gptbridge_mt_t;

/* TTL 探針快取槽（供同一 tick 內共享一次探測結果） */
typedef struct {
    int64_t fetched_at_ms;
    int32_t valid;
    int32_t probe_ok;
    void* payload; /* 呼叫方持有 */
} gptbridge_mt_cache_t;

int gptbridge_mt_init(gptbridge_mt_t* mt,
                      int64_t tick_interval_ms,
                      int64_t max_job_age_ms,
                      int32_t max_retry_attempts,
                      int64_t retry_backoff_ms,
                      int64_t current_generation);

/* 准入：依風險等級與旗標判定；回傳 1=入佇列，0=拒（fail-closed）。
   system_blocked 非零時全部等級皆拒 — 對齊 Python evaluate_policy 的
   全域阻斷（recovery / shutdown_draining / cooldown / lease_conflict，
   由呼叫方收斂為單一旗標注入）。 */
int gptbridge_mt_admit(gptbridge_mt_t* mt,
                       const gptbridge_mt_job_t* job,
                       int32_t system_idle,
                       int32_t authorized,
                       int32_t system_blocked,
                       int64_t now_ms);

/* 取下一個到期 job（priority 小者先；過期 job 直接標 FAILED 並跳過） */
gptbridge_mt_job_t* gptbridge_mt_next_due(gptbridge_mt_t* mt, int64_t now_ms);

/* 結果回報：成功 → COMPLETED；失敗 → 未達上限 DEFERRED＋backoff，達上限 FAILED */
int gptbridge_mt_complete(gptbridge_mt_t* mt, const char* job_id);
int gptbridge_mt_fail(gptbridge_mt_t* mt, const char* job_id, int64_t now_ms);

/* 撤回：Python 准入後被下游閘門（budget/lease/cooldown）否決的 job —
   標 CANCELLED 退出佇列，槽位可被回收（對齊 Python 端未入隊狀態） */
int gptbridge_mt_cancel(gptbridge_mt_t* mt, const char* job_id);

/* TTL 快取：有效內回傳快取；失效回 0（呼叫方重新探測後 set） */
int gptbridge_mt_cache_get(gptbridge_mt_cache_t* c, int64_t now_ms,
                           int64_t ttl_ms, void** out_payload,
                           int32_t* out_ok);
void gptbridge_mt_cache_set(gptbridge_mt_cache_t* c, int64_t now_ms,
                            int32_t probe_ok, void* payload);

#ifdef __cplusplus
}
#endif

#endif /* GPTBRIDGE_MAINTENANCE_H */

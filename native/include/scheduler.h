/* scheduler.h — E1 執行面：週期排程器 C 原型（ §10.65 E1 ）

對應 Python `tasks/periodic_scheduler.py` 的 deadline-driven 單迴圈：
  - per-job 間隔 / 超時 / 錯誤隔離 / 狀態可查
  - 單線程事件驅動，無輪詢
  - shadow 模式：與 Python 並行比對，Python 為權威

純 C11，無 C++。
*/
#ifndef GPTBRIDGE_SCHEDULER_H
#define GPTBRIDGE_SCHEDULER_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Bounded capacity sized for the governed automation flow count
 * (17 resident jobs as of 2026-09-23) with ~4x headroom; the cap
 * itself is a P7 hard limit — registration beyond it is refused
 * fail-closed, never grown dynamically. */
#define GPTBRIDGE_SCHED_MAX_JOBS 64
/* 32 was exactly the longest flow name (permission-automation-*),
 * leaving no room for NUL — registrations were silently refused.
 * 64 covers current names with headroom. */
#define GPTBRIDGE_SCHED_NAME_MAX 64

typedef void (*gptbridge_sched_fn)(void* ctx);

typedef struct {
    char name[GPTBRIDGE_SCHED_NAME_MAX];
    int64_t interval_ms;
    int64_t timeout_ms;
    int64_t next_due_ms;
    gptbridge_sched_fn fn;
    void* ctx;
    int32_t run_count;
    int32_t error_count;
    int32_t paused_count;
    int64_t last_run_ms;
    int64_t last_duration_ms;
    int32_t enabled;
    int32_t pausable;
} gptbridge_sched_job_t;

typedef struct {
    gptbridge_sched_job_t jobs[GPTBRIDGE_SCHED_MAX_JOBS];
    int32_t count;
    int64_t now_ms;
} gptbridge_sched_t;

int gptbridge_sched_init(gptbridge_sched_t* s);
/* 註冊錨定 now_ms（對齊 Python register()）：next_due = now_ms + (run_immediately ? 0 : interval_ms) */
int gptbridge_sched_register(gptbridge_sched_t* s, const char* name, int64_t interval_ms, int64_t timeout_ms, int64_t now_ms, int32_t run_immediately, int32_t pausable, gptbridge_sched_fn fn, void* ctx);
int gptbridge_sched_unregister(gptbridge_sched_t* s, const char* name);
/* 執行所有到期 job，返回執行數；paused 非零時 pausable job 延後（next_due = now + interval、paused_count++）而不執行 */
int gptbridge_sched_tick(gptbridge_sched_t* s, int64_t now_ms, int32_t paused);
int gptbridge_sched_job_count(const gptbridge_sched_t* s);
const gptbridge_sched_job_t* gptbridge_sched_find(const gptbridge_sched_t* s, const char* name);

#ifdef __cplusplus
}
#endif

#endif /* GPTBRIDGE_SCHEDULER_H */

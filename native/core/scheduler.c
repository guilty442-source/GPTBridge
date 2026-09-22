/* scheduler.c — E1 執行面：週期排程器 C 原型實作 */
#include "scheduler.h"
#include <string.h>
#include <stdio.h>

int gptbridge_sched_init(gptbridge_sched_t* s) {
    if (!s) return 0;
    memset(s, 0, sizeof(*s));
    s->now_ms = 0;
    return 1;
}

int gptbridge_sched_register(gptbridge_sched_t* s, const char* name, int64_t interval_ms, int64_t timeout_ms, int64_t now_ms, int32_t run_immediately, int32_t pausable, gptbridge_sched_fn fn, void* ctx) {
    if (!s || !name || !fn || s->count >= GPTBRIDGE_SCHED_MAX_JOBS) return 0;
    if (interval_ms <= 0) return 0;
    gptbridge_sched_job_t* j = &s->jobs[s->count];
    strncpy(j->name, name, GPTBRIDGE_SCHED_NAME_MAX - 1);
    j->name[GPTBRIDGE_SCHED_NAME_MAX - 1] = '\0';
    j->interval_ms = interval_ms;
    j->timeout_ms = timeout_ms > 0 ? timeout_ms : interval_ms;
    /* 對齊 Python：run_immediately → 立即到期，否則 now + interval */
    j->next_due_ms = now_ms + (run_immediately ? 0 : interval_ms);
    j->fn = fn;
    j->ctx = ctx;
    j->run_count = 0;
    j->error_count = 0;
    j->paused_count = 0;
    j->last_run_ms = 0;
    j->last_duration_ms = 0;
    j->enabled = 1;
    j->pausable = pausable ? 1 : 0;
    s->count++;
    return 1;
}

int gptbridge_sched_unregister(gptbridge_sched_t* s, const char* name) {
    if (!s || !name) return 0;
    for (int i = 0; i < s->count; ++i) {
        if (strncmp(s->jobs[i].name, name, GPTBRIDGE_SCHED_NAME_MAX) == 0) {
            /* shift-remove：保留註冊順序（對齊 Python dict 序） */
            memmove(&s->jobs[i], &s->jobs[i + 1],
                    (size_t)(s->count - i - 1) * sizeof(gptbridge_sched_job_t));
            s->count--;
            return 1;
        }
    }
    return 0;
}

int gptbridge_sched_tick(gptbridge_sched_t* s, int64_t now_ms, int32_t paused) {
    if (!s) return 0;
    s->now_ms = now_ms;
    int executed = 0;
    for (int i = 0; i < s->count; ++i) {
        gptbridge_sched_job_t* j = &s->jobs[i];
        if (!j->enabled) continue;
        if (now_ms < j->next_due_ms) continue;
        if (paused && j->pausable) {
            /* 對齊 Python §10.64④：管制中延後而非執行（不追趕） */
            j->next_due_ms = now_ms + j->interval_ms;
            j->paused_count++;
            continue;
        }
        int64_t start = now_ms;
        /* 錯誤隔離：fn 內部錯誤不影響其他 job（此處 fn 為純 C，無異常） */
        j->fn(j->ctx);
        j->last_run_ms = start;
        j->last_duration_ms = 0; /* 存根：實際可量測 */
        j->run_count++;
        j->next_due_ms = now_ms + j->interval_ms;
        executed++;
        /* 超時檢查：若 fn 執行超過 timeout_ms，標記（此原型不中斷，僅記錄） */
        if (j->last_duration_ms > j->timeout_ms) j->error_count++;
    }
    return executed;
}

int gptbridge_sched_job_count(const gptbridge_sched_t* s) { return s ? s->count : 0; }

const gptbridge_sched_job_t* gptbridge_sched_find(const gptbridge_sched_t* s, const char* name) {
    if (!s || !name) return NULL;
    for (int i = 0; i < s->count; ++i) {
        if (strncmp(s->jobs[i].name, name, GPTBRIDGE_SCHED_NAME_MAX) == 0) return &s->jobs[i];
    }
    return NULL;
}

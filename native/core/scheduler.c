/* scheduler.c — E1 執行面：週期排程器 C 原型實作 */
#include "scheduler.h"
#include <string.h>
#include <stdio.h>
#include <time.h>

static int64_t _wall_ms(void) {
    /* Wall-clock stamp for duration measurement only — schedule decisions
       stay on the caller-supplied logical now_ms (parity-comparable). */
    struct timespec ts;
    if (timespec_get(&ts, TIME_UTC) == 0) return 0;
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

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
    j->paused_since_ms = 0;
    /* 對齊 Python：starve_after_s = max(300.0, interval_s * 10.0) */
    j->starve_after_ms = interval_ms * 10 > 300000 ? interval_ms * 10 : 300000;
    j->starved_count = 0;
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
        int64_t wall_start = _wall_ms();
        j->fn(j->ctx);
        int64_t wall_end = _wall_ms();
        j->last_run_ms = start;
        j->last_duration_ms = wall_end > wall_start ? wall_end - wall_start : 0;
        j->run_count++;
        j->next_due_ms = now_ms + j->interval_ms;
        executed++;
        /* 超時檢查：fn 執行超過 timeout_ms 標記（同步原型不中斷，僅記錄） */
        if (j->last_duration_ms > j->timeout_ms) j->error_count++;
    }
    return executed;
}

int gptbridge_sched_collect_due(gptbridge_sched_t* s, int64_t now_ms,
                                int32_t paused,
                                char out_names[][GPTBRIDGE_SCHED_NAME_MAX],
                                int32_t max_names) {
    int32_t n = 0;
    if (!s || !out_names || max_names <= 0) return 0;
    s->now_ms = now_ms;
    for (int32_t i = 0; i < s->count; ++i) {
        gptbridge_sched_job_t* j = &s->jobs[i];
        if (!j->enabled) continue;
        if (now_ms < j->next_due_ms) continue;
        if (paused && j->pausable) {
            if (j->paused_since_ms == 0) j->paused_since_ms = now_ms;
            if (now_ms - j->paused_since_ms <= j->starve_after_ms) {
                /* 管制中延後而非執行（不追趕） */
                j->next_due_ms = now_ms + j->interval_ms;
                j->paused_count++;
                continue;
            }
            j->starved_count++; /* starvation 上界：延後是節流非終止 */
        }
        j->paused_since_ms = 0;
        j->next_due_ms = now_ms + j->interval_ms;
        strncpy(out_names[n], j->name, GPTBRIDGE_SCHED_NAME_MAX - 1);
        out_names[n][GPTBRIDGE_SCHED_NAME_MAX - 1] = '\0';
        if (++n >= max_names) break;
    }
    return n;
}

int gptbridge_sched_record(gptbridge_sched_t* s, const char* name,
                           int64_t started_ms, int64_t duration_ms,
                           int32_t error) {
    gptbridge_sched_job_t* j;
    if (!s || !name) return 0;
    j = (gptbridge_sched_job_t*)gptbridge_sched_find(s, name);
    if (!j) return 0;
    j->last_run_ms = started_ms;
    j->last_duration_ms = duration_ms;
    j->run_count++;
    if (error) j->error_count++;
    return 1;
}

int64_t gptbridge_sched_min_due_ms(const gptbridge_sched_t* s) {
    int64_t best = 0;
    if (!s) return 0;
    for (int32_t i = 0; i < s->count; ++i) {
        const gptbridge_sched_job_t* j = &s->jobs[i];
        if (!j->enabled) continue;
        if (best == 0 || j->next_due_ms < best) best = j->next_due_ms;
    }
    return best;
}

int gptbridge_sched_job_count(const gptbridge_sched_t* s) { return s ? s->count : 0; }

const gptbridge_sched_job_t* gptbridge_sched_find(const gptbridge_sched_t* s, const char* name) {
    if (!s || !name) return NULL;
    for (int i = 0; i < s->count; ++i) {
        if (strncmp(s->jobs[i].name, name, GPTBRIDGE_SCHED_NAME_MAX) == 0) return &s->jobs[i];
    }
    return NULL;
}

/* maintenance.c — 維護排程 tick C 原型實作（純 C11） */
#include "maintenance.h"
#include <string.h>

int gptbridge_mt_init(gptbridge_mt_t* mt,
                      int64_t tick_interval_ms,
                      int64_t max_job_age_ms,
                      int32_t max_retry_attempts,
                      int64_t retry_backoff_ms,
                      int64_t current_generation) {
    if (!mt || tick_interval_ms <= 0 || max_job_age_ms <= 0 ||
        max_retry_attempts <= 0 || retry_backoff_ms <= 0) return 0;
    memset(mt, 0, sizeof(*mt));
    mt->tick_interval_ms = tick_interval_ms;
    mt->max_job_age_ms = max_job_age_ms;
    mt->max_retry_attempts = max_retry_attempts;
    mt->retry_backoff_ms = retry_backoff_ms;
    mt->current_generation = current_generation;
    return 1;
}

static gptbridge_mt_job_t* _find(gptbridge_mt_t* mt, const char* job_id) {
    if (!mt || !job_id) return NULL;
    for (int32_t i = 0; i < mt->count; ++i)
        if (strncmp(mt->jobs[i].job_id, job_id, GPTBRIDGE_MT_ID_MAX) == 0)
            return &mt->jobs[i];
    return NULL;
}

int gptbridge_mt_admit(gptbridge_mt_t* mt,
                       const gptbridge_mt_job_t* job,
                       int32_t system_idle,
                       int32_t authorized,
                       int32_t system_blocked,
                       int64_t now_ms) {
    if (!mt || !job || !job->job_id[0]) return 0;
    if (_find(mt, job->job_id)) return 0;             /* 冪等去重 */
    /* generation 不匹配即拒（enforce_generation_match） */
    if (job->generation != mt->current_generation) return 0;
    /* 全域阻斷：對齊 Python evaluate_policy 的 recovery/drain/cooldown/
       lease-conflict 早退 — 所有風險等級一併拒絕（fail-closed） */
    if (system_blocked) return 0;
    switch (job->risk_class) {
        case GPTBRIDGE_MT_M0: break;                          /* 恆入 */
        case GPTBRIDGE_MT_M1: if (!system_idle) return 0; break;
        /* M2：需授權旗標且需通過 M1 健康閘（對齊 Python evaluate_policy：
           M2 = governed_authorization ∧ M1 health checks） */
        case GPTBRIDGE_MT_M2: if (!authorized || !system_idle) return 0; break;
        case GPTBRIDGE_MT_M3: return 0; /* 僅候選：不自動執行 */
        default: return 0;              /* 未知等級 fail-closed */
    }
    /* 准入通過後才佔槽：未滿取新槽，滿了回收終態槽位（對齊 Python 佇列
       只持有活動 job），無可回收槽位才拒（fail-closed） */
    gptbridge_mt_job_t* slot = NULL;
    if (mt->count < GPTBRIDGE_MT_MAX_JOBS) {
        slot = &mt->jobs[mt->count++];
    } else {
        for (int32_t i = 0; i < mt->count; ++i) {
            gptbridge_mt_status_t st = mt->jobs[i].status;
            if (st == GPTBRIDGE_MT_COMPLETED || st == GPTBRIDGE_MT_FAILED ||
                st == GPTBRIDGE_MT_CANCELLED) {
                slot = &mt->jobs[i];
                break;
            }
        }
        if (!slot) return 0;
    }
    *slot = *job;
    slot->status = GPTBRIDGE_MT_QUEUED;
    slot->next_attempt_ms = now_ms;
    return 1;
}

gptbridge_mt_job_t* gptbridge_mt_next_due(gptbridge_mt_t* mt, int64_t now_ms) {
    gptbridge_mt_job_t* best = NULL;
    if (!mt) return NULL;
    for (int32_t i = 0; i < mt->count; ++i) {
        gptbridge_mt_job_t* j = &mt->jobs[i];
        if (j->status != GPTBRIDGE_MT_QUEUED &&
            j->status != GPTBRIDGE_MT_DEFERRED)
            continue;
        if (j->next_attempt_ms > now_ms) continue;
        /* 過期 job：標 FAILED（不執行） */
        if (now_ms - j->scheduled_at_ms > mt->max_job_age_ms) {
            j->status = GPTBRIDGE_MT_FAILED;
            continue;
        }
        if (!best || j->priority < best->priority ||
            (j->priority == best->priority &&
             j->scheduled_at_ms < best->scheduled_at_ms))
            best = j;
    }
    if (best) {
        best->status = GPTBRIDGE_MT_RUNNING;
        best->attempt_count++;
    }
    mt->last_tick_ms = now_ms;
    return best;
}

int gptbridge_mt_complete(gptbridge_mt_t* mt, const char* job_id) {
    gptbridge_mt_job_t* j = _find(mt, job_id);
    if (!j) return 0;
    j->status = GPTBRIDGE_MT_COMPLETED;
    return 1;
}

int gptbridge_mt_fail(gptbridge_mt_t* mt, const char* job_id, int64_t now_ms) {
    gptbridge_mt_job_t* j = _find(mt, job_id);
    if (!j) return 0;
    if (j->attempt_count >= mt->max_retry_attempts) {
        j->status = GPTBRIDGE_MT_FAILED;
        return 1;
    }
    j->status = GPTBRIDGE_MT_DEFERRED;
    j->next_attempt_ms = now_ms + mt->retry_backoff_ms * j->attempt_count;
    return 1;
}

int gptbridge_mt_cancel(gptbridge_mt_t* mt, const char* job_id) {
    gptbridge_mt_job_t* j = _find(mt, job_id);
    if (!j) return 0;
    if (j->status == GPTBRIDGE_MT_COMPLETED || j->status == GPTBRIDGE_MT_FAILED)
        return 0; /* 終態不可撤 */
    j->status = GPTBRIDGE_MT_CANCELLED;
    return 1;
}

int gptbridge_mt_cache_get(gptbridge_mt_cache_t* c, int64_t now_ms,
                           int64_t ttl_ms, void** out_payload,
                           int32_t* out_ok) {
    if (!c || !c->valid || now_ms - c->fetched_at_ms >= ttl_ms) return 0;
    if (out_payload) *out_payload = c->payload;
    if (out_ok) *out_ok = c->probe_ok;
    return 1;
}

void gptbridge_mt_cache_set(gptbridge_mt_cache_t* c, int64_t now_ms,
                            int32_t probe_ok, void* payload) {
    if (!c) return;
    c->fetched_at_ms = now_ms;
    c->valid = 1;
    c->probe_ok = probe_ok;
    c->payload = payload;
}

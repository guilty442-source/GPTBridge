/* outbox.c — state outbox 游標/窗口 C 原型實作（純 C11） */
#include "outbox.h"
#include <string.h>

static gptbridge_ob_session_t* _find_mut(gptbridge_ob_registry_t* r,
                                         const char* session_id) {
    if (!r || !session_id) return NULL;
    for (int32_t i = 0; i < r->count; ++i) {
        if (strncmp(r->sessions[i].session_id, session_id,
                    GPTBRIDGE_OB_ID_MAX) == 0)
            return &r->sessions[i];
    }
    return NULL;
}

int gptbridge_ob_init(gptbridge_ob_registry_t* r) {
    if (!r) return 0;
    memset(r, 0, sizeof(*r));
    return 1;
}

int gptbridge_ob_register(gptbridge_ob_registry_t* r, const char* session_id) {
    if (!r || !session_id || !session_id[0]) return 0;
    if (_find_mut(r, session_id)) return 1; /* 冪等 */
    if (r->count >= GPTBRIDGE_OB_MAX_SESSIONS) return 0; /* 有界 */
    gptbridge_ob_session_t* s = &r->sessions[r->count++];
    memset(s, 0, sizeof(*s));
    strncpy(s->session_id, session_id, GPTBRIDGE_OB_ID_MAX - 1);
    return 1;
}

int gptbridge_ob_unregister(gptbridge_ob_registry_t* r, const char* session_id) {
    if (!r || !session_id) return 0;
    for (int32_t i = 0; i < r->count; ++i) {
        if (strncmp(r->sessions[i].session_id, session_id,
                    GPTBRIDGE_OB_ID_MAX) == 0) {
            r->sessions[i] = r->sessions[r->count - 1];
            r->count--;
            return 1;
        }
    }
    return 0;
}

int64_t gptbridge_ob_hello(gptbridge_ob_registry_t* r,
                           const char* session_id,
                           int64_t cursor,
                           int32_t generation_matches,
                           int64_t latest_sequence,
                           int32_t* out_reset) {
    gptbridge_ob_session_t* s;
    int64_t effective = cursor < 0 ? 0 : cursor;
    int32_t reset = 0;
    if (out_reset) *out_reset = 0;
    if (!r || !session_id) return -1;
    s = _find_mut(r, session_id);
    if (!s) {
        if (!gptbridge_ob_register(r, session_id)) return -1;
        s = _find_mut(r, session_id);
    }
    if (!generation_matches) {
        effective = latest_sequence;
        reset = 1;
    }
    s->acked = effective;
    s->sent_upto = effective;
    s->last_attempt_ms = 0;
    if (out_reset) *out_reset = reset;
    return effective;
}

int gptbridge_ob_ack(gptbridge_ob_registry_t* r, const char* session_id,
                     int64_t cursor) {
    gptbridge_ob_session_t* s = _find_mut(r, session_id);
    if (!s || cursor <= s->acked) return 0; /* 單調不回退 */
    s->acked = cursor;
    return 1;
}

int gptbridge_ob_resync(gptbridge_ob_registry_t* r, const char* session_id,
                        int64_t cursor) {
    gptbridge_ob_session_t* s = _find_mut(r, session_id);
    int64_t c = cursor < 0 ? 0 : cursor;
    if (!s) return 0;
    s->acked = c;
    s->sent_upto = c;
    s->last_attempt_ms = 0;
    return 1;
}

int gptbridge_ob_drain_plan(gptbridge_ob_registry_t* r,
                            const char* session_id,
                            int64_t now_ms,
                            int64_t retry_ms,
                            int64_t* out_start_after,
                            int64_t* out_limit) {
    gptbridge_ob_session_t* s = _find_mut(r, session_id);
    int64_t window_end, start_after;
    if (!s || !out_start_after || !out_limit) return 0;
    window_end = s->acked + GPTBRIDGE_OB_DELIVERY_WINDOW;
    /* 已 ack 的事件絕不重送：起點不得低於 acked */
    start_after = s->sent_upto > s->acked ? s->sent_upto : s->acked;
    /* 未 ack 且逾 retry → 自 acked 重送（at-least-once） */
    if (s->sent_upto > s->acked &&
        now_ms - s->last_attempt_ms >= retry_ms)
        start_after = s->acked;
    if (start_after >= window_end) return 0; /* 窗口滿：背壓 */
    *out_start_after = start_after;
    *out_limit = window_end - start_after;
    if (*out_limit > GPTBRIDGE_OB_DRAIN_BATCH)
        *out_limit = GPTBRIDGE_OB_DRAIN_BATCH;
    return 1;
}

int gptbridge_ob_mark_sent(gptbridge_ob_registry_t* r,
                           const char* session_id,
                           int64_t seq,
                           int64_t now_ms) {
    gptbridge_ob_session_t* s = _find_mut(r, session_id);
    if (!s || seq <= s->sent_upto) return 0;
    s->sent_upto = seq;
    s->last_attempt_ms = now_ms;
    return 1;
}

int gptbridge_ob_next_retry_deadline(const gptbridge_ob_registry_t* r,
                                     int64_t retry_ms,
                                     int64_t* out_deadline_ms) {
    int64_t best = 0;
    int32_t found = 0;
    if (!r || !out_deadline_ms) return 0;
    for (int32_t i = 0; i < r->count; ++i) {
        const gptbridge_ob_session_t* s = &r->sessions[i];
        if (s->sent_upto > s->acked) {
            int64_t d = s->last_attempt_ms + retry_ms;
            if (!found || d < best) { best = d; found = 1; }
        }
    }
    if (!found) return 0;
    *out_deadline_ms = best;
    return 1;
}

int64_t gptbridge_ob_prune_floor(const gptbridge_ob_registry_t* r,
                                 int64_t latest_sequence) {
    int64_t floor;
    if (!r) return 0;
    if (r->count == 0) return latest_sequence;
    floor = r->sessions[0].acked;
    for (int32_t i = 1; i < r->count; ++i)
        if (r->sessions[i].acked < floor) floor = r->sessions[i].acked;
    return floor;
}

const gptbridge_ob_session_t* gptbridge_ob_find(const gptbridge_ob_registry_t* r,
                                                const char* session_id) {
    if (!r || !session_id) return NULL;
    for (int32_t i = 0; i < r->count; ++i) {
        if (strncmp(r->sessions[i].session_id, session_id,
                    GPTBRIDGE_OB_ID_MAX) == 0)
            return &r->sessions[i];
    }
    return NULL;
}

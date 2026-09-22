/* watchdog.c — 連線看門狗狀態機 C 原型實作（純 C11） */
#include "watchdog.h"
#include <string.h>

int gptbridge_wd_init(gptbridge_wd_t* wd,
                      int64_t min_interval_ms,
                      int64_t max_interval_ms,
                      int32_t dead_threshold,
                      int32_t retry_grace) {
    if (!wd || min_interval_ms <= 0 || max_interval_ms < min_interval_ms ||
        dead_threshold <= 0 || retry_grace < 0) return 0;
    memset(wd, 0, sizeof(*wd));
    wd->state = GPTBRIDGE_WD_UNKNOWN;
    wd->min_interval_ms = min_interval_ms;
    wd->max_interval_ms = max_interval_ms;
    wd->adaptive_interval_ms = min_interval_ms;
    wd->dead_threshold = dead_threshold;
    wd->retry_grace = retry_grace;
    return 1;
}

gptbridge_wd_state_t gptbridge_wd_compute_state(const gptbridge_wd_probe_t* p) {
    if (!p) return GPTBRIDGE_WD_UNKNOWN;
    if (p->backend_process_alive && p->backend_http_healthy && p->frontend_connected)
        return GPTBRIDGE_WD_CONNECTED;
    if (p->backend_process_alive && p->backend_http_healthy && !p->frontend_connected)
        return GPTBRIDGE_WD_DEGRADED;
    if (!p->backend_process_alive)
        return GPTBRIDGE_WD_DISCONNECTED;
    return GPTBRIDGE_WD_STARTING;
}

static int32_t _dead_count(gptbridge_wd_state_t new_state,
                           gptbridge_wd_state_t old_state,
                           int32_t old_dead,
                           int32_t grace) {
    if (new_state == GPTBRIDGE_WD_CONNECTED || new_state == GPTBRIDGE_WD_DEGRADED)
        return 0;
    if (old_state == GPTBRIDGE_WD_CONNECTED && new_state == GPTBRIDGE_WD_DISCONNECTED) {
        int32_t v = old_dead - grace + 1;
        return v < 0 ? 0 : v;
    }
    return old_dead + 1;
}

static void _record_event(gptbridge_wd_t* wd,
                          gptbridge_wd_state_t from,
                          gptbridge_wd_state_t to,
                          int32_t trigger,
                          int64_t now_ms,
                          gptbridge_wd_event_t* out_event) {
    gptbridge_wd_event_t e;
    e.from_state = from;
    e.to_state = to;
    e.trigger_repair = trigger;
    e.at_ms = now_ms;
    wd->events[wd->event_head] = e;
    wd->event_head = (wd->event_head + 1) % GPTBRIDGE_WD_EVENT_MAX;
    if (wd->event_count < GPTBRIDGE_WD_EVENT_MAX) wd->event_count++;
    if (out_event) *out_event = e;
}

int gptbridge_wd_probe(gptbridge_wd_t* wd,
                       const gptbridge_wd_probe_t* probe,
                       int64_t now_ms,
                       gptbridge_wd_event_t* out_event) {
    if (!wd || !probe) return 0;
    gptbridge_wd_state_t old_state = wd->state;
    gptbridge_wd_state_t new_state = gptbridge_wd_compute_state(probe);
    int32_t new_dead = _dead_count(new_state, old_state,
                                   wd->consecutive_dead, wd->retry_grace);
    wd->state = new_state;
    wd->consecutive_dead = new_dead;
    wd->probe_count++;
    /* repair 觸發為一次性旗標：connected 復位，未 connected 且達閾才觸發 */
    int32_t trigger = (new_state != GPTBRIDGE_WD_CONNECTED &&
                       new_dead >= wd->dead_threshold &&
                       !wd->repair_triggered);
    if (new_state == GPTBRIDGE_WD_CONNECTED)
        wd->repair_triggered = 0;
    else if (trigger)
        wd->repair_triggered = 1;
    /* 自適應間隔 */
    if (new_state == GPTBRIDGE_WD_CONNECTED) {
        wd->consecutive_stable++;
        if (wd->consecutive_stable >= 3) {
            int64_t grown = wd->adaptive_interval_ms + wd->adaptive_interval_ms / 2;
            wd->adaptive_interval_ms =
                grown > wd->max_interval_ms ? wd->max_interval_ms : grown;
        }
    } else {
        wd->consecutive_stable = 0;
        wd->adaptive_interval_ms = wd->min_interval_ms;
    }
    if (new_state != old_state)
        _record_event(wd, old_state, new_state, trigger, now_ms, out_event);
    else if (trigger)
        _record_event(wd, old_state, new_state, 1, now_ms, out_event);
    return trigger;
}

int64_t gptbridge_wd_next_interval_ms(gptbridge_wd_t* wd) {
    if (!wd) return 0;
    return wd->adaptive_interval_ms;
}

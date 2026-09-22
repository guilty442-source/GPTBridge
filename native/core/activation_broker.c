/* activation_broker.c — §10.65 E3：按需啟動 broker 決策機

實作對齊 Python model_service_activation.py 的 _ensure_inner／
_maybe_release_owner／note_explicit_owner_stop／_write_state 節流；
所有外部觀測（pending SQL 探針、liveness、governor 狀態）由呼叫方注入。
*/
#include "activation_broker.h"

static double _maxd(double a, double b) { return a > b ? a : b; }
static double _mind(double a, double b) { return a < b ? a : b; }

int gptbridge_act_init(gptbridge_act_broker_t* broker,
                       double cooldown_s,
                       double min_backoff_s,
                       double max_backoff_s) {
    if (broker == NULL) {
        return 0;
    }
    /* Python __init__ 夾取：cooldown>=0、min>=1.0、max>=min */
    broker->cooldown_s = _maxd(0.0, cooldown_s);
    broker->min_backoff_s = _maxd(1.0, min_backoff_s);
    broker->max_backoff_s = _maxd(broker->min_backoff_s, max_backoff_s);
    broker->next_attempt_at = 0.0;
    broker->next_release_at = 0.0;
    broker->backoff_s = broker->min_backoff_s;
    broker->attempts = 0;
    broker->broker_started_owner = 0;
    broker->explicit_stop_at = 0.0;
    return 1;
}

gptbridge_act_decision_t gptbridge_act_ensure(
    gptbridge_act_broker_t* broker,
    const gptbridge_act_inputs_t* in) {
    if (broker == NULL || in == NULL) {
        return GPTBRIDGE_ACT_IDLE;
    }

    if (!in->pending) {
        /* _maybe_release_owner：僅釋放本 broker 啟動的 owner，
           且 governor 管制中；liveness 例外 → idle（Python except 分支） */
        broker->backoff_s = broker->min_backoff_s;
        if (!broker->broker_started_owner || !in->regulation_active) {
            return GPTBRIDGE_ACT_IDLE;
        }
        if (!in->liveness_known) {
            return GPTBRIDGE_ACT_IDLE;
        }
        if (!in->owner_active) {
            broker->broker_started_owner = 0;
            return GPTBRIDGE_ACT_IDLE;
        }
        if (in->now_monotonic < broker->next_release_at) {
            return GPTBRIDGE_ACT_RELEASE_COOLDOWN;
        }
        return GPTBRIDGE_ACT_SHOULD_RELEASE;
    }

    if (!in->maintenance_ready) {
        return GPTBRIDGE_ACT_MAINTENANCE_PENDING;
    }
    if (in->shutting_down) {
        return GPTBRIDGE_ACT_SHUTTING_DOWN;
    }
    if (in->admission_hold) {
        return GPTBRIDGE_ACT_RESOURCE_HOLD;
    }
    if (!in->liveness_known) {
        return GPTBRIDGE_ACT_LIVENESS_UNKNOWN;
    }
    if (in->owner_active) {
        broker->backoff_s = broker->min_backoff_s;
        return GPTBRIDGE_ACT_OWNER_RUNNING;
    }
    if (in->now_monotonic < broker->next_attempt_at) {
        return GPTBRIDGE_ACT_THROTTLED;
    }
    /* Python：payload 構造後 self._attempts += 1 → 委託 start_tool */
    broker->attempts += 1;
    return GPTBRIDGE_ACT_SHOULD_START;
}

gptbridge_act_decision_t gptbridge_act_on_start_result(
    gptbridge_act_broker_t* broker, int32_t ok, double now_monotonic) {
    double delay;
    if (broker == NULL) {
        return GPTBRIDGE_ACT_START_FAILED;
    }
    if (ok) {
        broker->next_attempt_at = now_monotonic + broker->cooldown_s;
        broker->backoff_s = broker->min_backoff_s;
        broker->broker_started_owner = 1;
        return GPTBRIDGE_ACT_STARTED;
    }
    delay = broker->backoff_s;
    broker->backoff_s = _mind(broker->max_backoff_s,
                              broker->backoff_s * 2.0);
    broker->next_attempt_at = now_monotonic + delay;
    return GPTBRIDGE_ACT_START_FAILED;
}

gptbridge_act_decision_t gptbridge_act_on_release_result(
    gptbridge_act_broker_t* broker, int32_t ok, double now_monotonic) {
    if (broker == NULL) {
        return GPTBRIDGE_ACT_RELEASE_FAILED;
    }
    broker->next_release_at = now_monotonic + broker->cooldown_s;
    if (ok) {
        broker->broker_started_owner = 0;
        return GPTBRIDGE_ACT_RELEASED;
    }
    return GPTBRIDGE_ACT_RELEASE_FAILED;
}

void gptbridge_act_note_explicit_stop(gptbridge_act_broker_t* broker,
                                      double now_monotonic,
                                      double wall_time) {
    if (broker == NULL) {
        return;
    }
    broker->explicit_stop_at = wall_time;
    broker->broker_started_owner = 0;
    broker->next_attempt_at = now_monotonic + broker->cooldown_s;
}

double gptbridge_act_poll_interval(int32_t pending,
                                   double idle_interval_s,
                                   double pending_interval_s) {
    return pending ? pending_interval_s : idle_interval_s;
}

int gptbridge_act_state_write_due(int32_t fingerprint_changed,
                                  double now_monotonic,
                                  double last_write_at,
                                  double heartbeat_s) {
    if (fingerprint_changed) {
        return 1;
    }
    return (now_monotonic - last_write_at) >= heartbeat_s;
}

const char* gptbridge_act_decision_name(gptbridge_act_decision_t d) {
    switch (d) {
    case GPTBRIDGE_ACT_IDLE: return "idle";
    case GPTBRIDGE_ACT_MAINTENANCE_PENDING: return "maintenance-pending";
    case GPTBRIDGE_ACT_SHUTTING_DOWN: return "shutting-down";
    case GPTBRIDGE_ACT_RESOURCE_HOLD: return "resource-hold";
    case GPTBRIDGE_ACT_OWNER_RUNNING: return "owner-running";
    case GPTBRIDGE_ACT_LIVENESS_UNKNOWN: return "liveness-unknown";
    case GPTBRIDGE_ACT_THROTTLED: return "throttled";
    case GPTBRIDGE_ACT_SHOULD_START: return "should-start";
    case GPTBRIDGE_ACT_STARTED: return "started";
    case GPTBRIDGE_ACT_START_FAILED: return "start-failed";
    case GPTBRIDGE_ACT_RELEASE_COOLDOWN: return "release-cooldown";
    case GPTBRIDGE_ACT_SHOULD_RELEASE: return "should-release";
    case GPTBRIDGE_ACT_RELEASED: return "released";
    case GPTBRIDGE_ACT_RELEASE_FAILED: return "release-failed";
    default: return "unknown";
    }
}

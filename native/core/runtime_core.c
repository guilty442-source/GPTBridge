/* runtime_core.c — 運行核心 C 原型實作（純 C11） */
#include "runtime_core.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#ifdef _WIN32
#include <windows.h>
#endif

/* ---------- Queue ---------- */
int gptbridge_rc_queue_init(gptbridge_rc_queue_t* q, int32_t capacity) {
    if (!q || capacity <= 0 || capacity > GPTBRIDGE_RC_MAX_TASKS) return 0;
    q->count = 0;
    q->capacity = capacity;
    memset(q->tasks, 0, sizeof(q->tasks));
    return 1;
}

static int _priority_gt(const gptbridge_rc_task_t* a, const gptbridge_rc_task_t* b) {
    if (a->priority != b->priority) return a->priority > b->priority;
    return a->id < b->id; /* 同優先序先入先出（id 小者先） */
}

int gptbridge_rc_queue_push(gptbridge_rc_queue_t* q, const gptbridge_rc_task_t* t) {
    if (!q || !t) return 0;
    if (q->count >= q->capacity) return 0; /* 背壓 */
    /* 插入並按優先序排序（簡單插入排序，N≤64） */
    int i = q->count;
    q->tasks[i] = *t;
    q->count++;
    for (int j = q->count - 1; j > 0; --j) {
        if (_priority_gt(&q->tasks[j], &q->tasks[j-1])) {
            gptbridge_rc_task_t tmp = q->tasks[j];
            q->tasks[j] = q->tasks[j-1];
            q->tasks[j-1] = tmp;
        } else break;
    }
    return 1;
}

int gptbridge_rc_queue_pop(gptbridge_rc_queue_t* q, gptbridge_rc_task_t* out) {
    if (!q || !out || q->count == 0) return 0;
    *out = q->tasks[0];
    memmove(&q->tasks[0], &q->tasks[1], sizeof(gptbridge_rc_task_t) * (size_t)(q->count - 1));
    q->count--;
    return 1;
}

int gptbridge_rc_queue_cancel(gptbridge_rc_queue_t* q, uint64_t id) {
    if (!q) return 0;
    for (int i = 0; i < q->count; ++i) {
        if (q->tasks[i].id == id) {
            q->tasks[i].cancelled = 1;
            q->tasks[i].state = GPTBRIDGE_RC_TASK_CANCELLED;
            return 1;
        }
    }
    return 0;
}

int gptbridge_rc_queue_depth(const gptbridge_rc_queue_t* q) { return q ? q->count : 0; }

/* ---------- Lifecycle ---------- */
static int _lifecycle_allowed(gptbridge_rc_lifecycle_t from, gptbridge_rc_lifecycle_t to) {
    switch (from) {
        case GPTBRIDGE_RC_CREATED: return to == GPTBRIDGE_RC_READY || to == GPTBRIDGE_RC_FAILED;
        case GPTBRIDGE_RC_READY: return to == GPTBRIDGE_RC_RUNNING || to == GPTBRIDGE_RC_TERMINATED || to == GPTBRIDGE_RC_FAILED;
        case GPTBRIDGE_RC_RUNNING: return to == GPTBRIDGE_RC_PAUSED || to == GPTBRIDGE_RC_TERMINATED || to == GPTBRIDGE_RC_FAILED;
        case GPTBRIDGE_RC_PAUSED: return to == GPTBRIDGE_RC_RUNNING || to == GPTBRIDGE_RC_TERMINATED || to == GPTBRIDGE_RC_FAILED;
        default: return 0;
    }
}

int gptbridge_rc_lifecycle_init(gptbridge_rc_lifecycle_sm_t* sm) {
    if (!sm) return 0;
    sm->state = GPTBRIDGE_RC_CREATED;
    return 1;
}
int gptbridge_rc_lifecycle_transition(gptbridge_rc_lifecycle_sm_t* sm, gptbridge_rc_lifecycle_t next) {
    if (!sm) return 0;
    if (!_lifecycle_allowed(sm->state, next)) return 0;
    sm->state = next;
    return 1;
}
gptbridge_rc_lifecycle_t gptbridge_rc_lifecycle_state(const gptbridge_rc_lifecycle_sm_t* sm) {
    return sm ? sm->state : GPTBRIDGE_RC_FAILED;
}

/* ---------- Resource Monitoring ---------- */
int gptbridge_rc_resource_sample(gptbridge_rc_resource_sample_t* out, int32_t queue_depth) {
    if (!out) return 0;
    /* 存根：實際取樣由呼叫方注入（避免依賴系統 API），此處僅填充 queue_depth 與時間戳 */
    out->cpu_pct = 0.0;
    out->mem_mb = 0;
    out->queue_depth = queue_depth;
#ifdef _WIN32
    out->timestamp_ms = (int64_t)GetTickCount64();
#else
    out->timestamp_ms = 0;
#endif
    return 1;
}

/* ---------- Deadline / Cancellation ---------- */
int gptbridge_rc_task_is_expired(const gptbridge_rc_task_t* t, int64_t now_ms) {
    if (!t) return 0;
    if (t->deadline_ms == 0) return 0;
    return now_ms >= t->deadline_ms;
}
int gptbridge_rc_task_cancel(gptbridge_rc_task_t* t) {
    if (!t) return 0;
    t->cancelled = 1;
    t->state = GPTBRIDGE_RC_TASK_CANCELLED;
    return 1;
}

/* ---------- Event Loop ---------- */
struct gptbridge_rc_event_loop {
    gptbridge_rc_queue_t queue;
    gptbridge_rc_lifecycle_sm_t sm;
    int32_t woken;
};

gptbridge_rc_event_loop_t* gptbridge_rc_loop_create(int32_t queue_capacity) {
    gptbridge_rc_event_loop_t* loop = (gptbridge_rc_event_loop_t*)calloc(1, sizeof(*loop));
    if (!loop) return NULL;
    if (!gptbridge_rc_queue_init(&loop->queue, queue_capacity)) { free(loop); return NULL; }
    gptbridge_rc_lifecycle_init(&loop->sm);
    gptbridge_rc_lifecycle_transition(&loop->sm, GPTBRIDGE_RC_READY);
    loop->woken = 0;
    return loop;
}
void gptbridge_rc_loop_destroy(gptbridge_rc_event_loop_t* loop) {
    if (!loop) return;
    free(loop);
}
int gptbridge_rc_loop_submit(gptbridge_rc_event_loop_t* loop, const gptbridge_rc_task_t* t) {
    if (!loop || !t) return 0;
    return gptbridge_rc_queue_push(&loop->queue, t);
}
int gptbridge_rc_loop_tick(gptbridge_rc_event_loop_t* loop, gptbridge_rc_task_fn fn, void* ctx, int64_t now_ms) {
    if (!loop || !fn) return 0;
    if (loop->sm.state != GPTBRIDGE_RC_READY && loop->sm.state != GPTBRIDGE_RC_RUNNING) return 0;
    /* 將當前狀態切為 RUNNING（首次 tick） */
    if (loop->sm.state == GPTBRIDGE_RC_READY) gptbridge_rc_lifecycle_transition(&loop->sm, GPTBRIDGE_RC_RUNNING);
    gptbridge_rc_task_t t;
    if (!gptbridge_rc_queue_pop(&loop->queue, &t)) return 0; /* idle */
    if (t.cancelled || gptbridge_rc_task_is_expired(&t, now_ms)) {
        t.state = t.cancelled ? GPTBRIDGE_RC_TASK_CANCELLED : GPTBRIDGE_RC_TASK_TIMED_OUT;
        /* fail-closed：不執行，直接丟棄並回報（呼叫方 via 審計） */
        return 1;
    }
    t.state = GPTBRIDGE_RC_TASK_RUNNING;
    fn(&t, ctx);
    t.state = GPTBRIDGE_RC_TASK_COMPLETED;
    (void)ctx;
    return 1;
}
int gptbridge_rc_loop_wakeup(gptbridge_rc_event_loop_t* loop) {
    if (!loop) return 0;
    loop->woken = 1;
    return 1;
}
int gptbridge_rc_loop_queue_depth(const gptbridge_rc_event_loop_t* loop) { return loop ? gptbridge_rc_queue_depth(&loop->queue) : 0; }
gptbridge_rc_lifecycle_t gptbridge_rc_loop_lifecycle(const gptbridge_rc_event_loop_t* loop) { return loop ? gptbridge_rc_lifecycle_state(&loop->sm) : GPTBRIDGE_RC_FAILED; }

/* ---------- IPC stub ---------- */
static gptbridge_rc_ipc_msg_t _ipc_slot;
static int _ipc_has = 0;
int gptbridge_rc_ipc_send(const gptbridge_rc_ipc_msg_t* msg) {
    if (!msg) return 0;
    _ipc_slot = *msg;
    _ipc_has = 1;
    return 1;
}
int gptbridge_rc_ipc_recv(gptbridge_rc_ipc_msg_t* out) {
    if (!out || !_ipc_has) return 0;
    *out = _ipc_slot;
    _ipc_has = 0;
    return 1;
}

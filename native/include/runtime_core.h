/* runtime_core.h — 運行核心 C 原型（ §1.1 高速執行核心 ）

純 C11，無 C++，與現有 native/core 並存（memory.h / vector.c / transformer.c 等）。
範圍（2026-09-21 核定，全部可實作）：
  - 主宰工作（Sovereign Work）最小可執行單元
  - Event Loop（單線程事件迴圈+喚醒）
  - Task Queue（有界、優先序、背壓）
  - Lifecycle State Machine（建立/就緒/執行/暫停/終止/失敗）
  - Resource Monitoring（CPU/記憶體/佇列深度取樣）
  - Deadline / Cancellation（每任務截止與取消傳播，fail-closed）
  - IPC 傳輸（受治理本機傳輸，與 Python 治理主宰介接）

Fail-closed 政策仍在 Python；本文件只提供執行能力，不做權限/稽核判定。
*/
#ifndef GPTBRIDGE_RUNTIME_CORE_H
#define GPTBRIDGE_RUNTIME_CORE_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GPTBRIDGE_RC_MAX_TASKS 64
#define GPTBRIDGE_RC_MAX_WORKERS 4

/* ---------- Lifecycle ---------- */
typedef enum {
    GPTBRIDGE_RC_CREATED = 0,
    GPTBRIDGE_RC_READY = 1,
    GPTBRIDGE_RC_RUNNING = 2,
    GPTBRIDGE_RC_PAUSED = 3,
    GPTBRIDGE_RC_TERMINATED = 4,
    GPTBRIDGE_RC_FAILED = 5
} gptbridge_rc_lifecycle_t;

/* ---------- Task ---------- */
typedef enum {
    GPTBRIDGE_RC_TASK_PENDING = 0,
    GPTBRIDGE_RC_TASK_RUNNING = 1,
    GPTBRIDGE_RC_TASK_COMPLETED = 2,
    GPTBRIDGE_RC_TASK_CANCELLED = 3,
    GPTBRIDGE_RC_TASK_TIMED_OUT = 4
} gptbridge_rc_task_state_t;

typedef struct {
    uint64_t id;
    int32_t priority;          /* 高值先行 */
    int64_t deadline_ms;       /* 絕對時間 ms，0=無截止 */
    int32_t cancelled;         /* 0/1 */
    gptbridge_rc_task_state_t state;
    void* payload;             /* 呼叫方持有，C 不擁有 */
} gptbridge_rc_task_t;

/* ---------- Queue (bounded, priority, backpressure) ---------- */
typedef struct {
    gptbridge_rc_task_t tasks[GPTBRIDGE_RC_MAX_TASKS];
    int32_t count;
    int32_t capacity;
} gptbridge_rc_queue_t;

int gptbridge_rc_queue_init(gptbridge_rc_queue_t* q, int32_t capacity);
int gptbridge_rc_queue_push(gptbridge_rc_queue_t* q, const gptbridge_rc_task_t* t); /* 1=ok, 0=backpressure/full */
int gptbridge_rc_queue_pop(gptbridge_rc_queue_t* q, gptbridge_rc_task_t* out); /* 1=ok, 0=empty */
int gptbridge_rc_queue_cancel(gptbridge_rc_queue_t* q, uint64_t id); /* 1=found */
int gptbridge_rc_queue_depth(const gptbridge_rc_queue_t* q);

/* ---------- Lifecycle ---------- */
typedef struct {
    gptbridge_rc_lifecycle_t state;
} gptbridge_rc_lifecycle_sm_t;

int gptbridge_rc_lifecycle_init(gptbridge_rc_lifecycle_sm_t* sm);
int gptbridge_rc_lifecycle_transition(gptbridge_rc_lifecycle_sm_t* sm, gptbridge_rc_lifecycle_t next); /* 1=ok, 0=illegal */
gptbridge_rc_lifecycle_t gptbridge_rc_lifecycle_state(const gptbridge_rc_lifecycle_sm_t* sm);

/* ---------- Resource Monitoring ---------- */
typedef struct {
    double cpu_pct;        /* 0-100 */
    int64_t mem_mb;
    int32_t queue_depth;
    int64_t timestamp_ms;
} gptbridge_rc_resource_sample_t;

int gptbridge_rc_resource_sample(gptbridge_rc_resource_sample_t* out, int32_t queue_depth);

/* ---------- Deadline / Cancellation ---------- */
int gptbridge_rc_task_is_expired(const gptbridge_rc_task_t* t, int64_t now_ms);
int gptbridge_rc_task_cancel(gptbridge_rc_task_t* t);

/* ---------- Event Loop (single-thread, wakeup) ---------- */
typedef struct gptbridge_rc_event_loop gptbridge_rc_event_loop_t;
typedef void (*gptbridge_rc_task_fn)(gptbridge_rc_task_t* task, void* ctx);

gptbridge_rc_event_loop_t* gptbridge_rc_loop_create(int32_t queue_capacity);
void gptbridge_rc_loop_destroy(gptbridge_rc_event_loop_t* loop);
int gptbridge_rc_loop_submit(gptbridge_rc_event_loop_t* loop, const gptbridge_rc_task_t* t);
int gptbridge_rc_loop_tick(gptbridge_rc_event_loop_t* loop, gptbridge_rc_task_fn fn, void* ctx, int64_t now_ms); /* 執行一個就緒任務，1=executed, 0=idle */
int gptbridge_rc_loop_wakeup(gptbridge_rc_event_loop_t* loop);
int gptbridge_rc_loop_queue_depth(const gptbridge_rc_event_loop_t* loop);
gptbridge_rc_lifecycle_t gptbridge_rc_loop_lifecycle(const gptbridge_rc_event_loop_t* loop);

/* ---------- IPC (受治理本機傳輸 stub) ---------- */
typedef struct {
    uint64_t correlation_id;
    char payload[256];
} gptbridge_rc_ipc_msg_t;

int gptbridge_rc_ipc_send(const gptbridge_rc_ipc_msg_t* msg); /* 1=ok, 0=fail-closed */
int gptbridge_rc_ipc_recv(gptbridge_rc_ipc_msg_t* out); /* 1=ok, 0=empty */

#ifdef __cplusplus
}
#endif

#endif /* GPTBRIDGE_RUNTIME_CORE_H */

/* ipc_registry.c — E2 傳輸/註冊面 C 原型實作 */
#include "ipc_registry.h"
#include <string.h>
#include <stdio.h>

int gptbridge_ipc_registry_init(gptbridge_ipc_registry_t* r) {
    if (!r) return 0;
    memset(r, 0, sizeof(*r));
    return 1;
}

int gptbridge_ipc_registry_create(gptbridge_ipc_registry_t* r, const char* request_id, int32_t generation, int64_t now_ms) {
    if (!r || !request_id || r->count >= GPTBRIDGE_IPC_MAX_REQUESTS) return 0;
    if (gptbridge_ipc_registry_find(r, request_id)) return 0; /* 已存在 */
    gptbridge_ipc_request_t* req = &r->reqs[r->count];
    strncpy(req->request_id, request_id, GPTBRIDGE_IPC_ID_MAX - 1);
    req->request_id[GPTBRIDGE_IPC_ID_MAX - 1] = '\0';
    req->backend_generation = generation;
    req->status = GPTBRIDGE_REQ_CREATED;
    req->created_at_ms = now_ms;
    req->started_at_ms = 0;
    req->completed_at_ms = 0;
    req->timeout_ms = 0;
    req->cancelled = 0;
    r->count++;
    return 1;
}

int gptbridge_ipc_registry_set_status(gptbridge_ipc_registry_t* r, const char* request_id, gptbridge_req_status_t s, int64_t now_ms) {
    gptbridge_ipc_request_t* req = (gptbridge_ipc_request_t*)gptbridge_ipc_registry_find(r, request_id);
    if (!req) return 0;
    /* 終止態不可再轉出（COMPLETED/FAILED/CANCELLED/TIMED_OUT 為終止） */
    if (req->status == GPTBRIDGE_REQ_COMPLETED || req->status == GPTBRIDGE_REQ_FAILED ||
        req->status == GPTBRIDGE_REQ_CANCELLED || req->status == GPTBRIDGE_REQ_TIMED_OUT) {
        if (s != req->status) return 0;
    }
    req->status = s;
    if (s == GPTBRIDGE_REQ_RUNNING && req->started_at_ms == 0) req->started_at_ms = now_ms;
    if (s == GPTBRIDGE_REQ_COMPLETED || s == GPTBRIDGE_REQ_FAILED ||
        s == GPTBRIDGE_REQ_CANCELLED || s == GPTBRIDGE_REQ_TIMED_OUT) {
        if (req->completed_at_ms == 0) req->completed_at_ms = now_ms;
    }
    return 1;
}

int gptbridge_ipc_registry_cancel(gptbridge_ipc_registry_t* r, const char* request_id, int64_t now_ms) {
    const gptbridge_ipc_request_t* found = gptbridge_ipc_registry_find(r, request_id);
    if (!found) return 0;
    gptbridge_ipc_request_t* req = (gptbridge_ipc_request_t*)found;
    if (req->status == GPTBRIDGE_REQ_COMPLETED || req->status == GPTBRIDGE_REQ_FAILED ||
        req->status == GPTBRIDGE_REQ_CANCELLED || req->status == GPTBRIDGE_REQ_TIMED_OUT) {
        return 1; /* 已終止，冪等 */
    }
    req->cancelled = 1;
    req->status = GPTBRIDGE_REQ_CANCELLED;
    if (req->completed_at_ms == 0) req->completed_at_ms = now_ms;
    return 1;
}

int gptbridge_ipc_registry_set_timeout(gptbridge_ipc_registry_t* r, const char* request_id, int64_t timeout_ms) {
    gptbridge_ipc_request_t* req = (gptbridge_ipc_request_t*)gptbridge_ipc_registry_find(r, request_id);
    if (!req || timeout_ms < 0) return 0;
    req->timeout_ms = timeout_ms;
    return 1;
}

int64_t gptbridge_ipc_registry_deadline_ms(const gptbridge_ipc_registry_t* r, const char* request_id) {
    const gptbridge_ipc_request_t* req = gptbridge_ipc_registry_find(r, request_id);
    if (!req || req->timeout_ms <= 0) return 0;
    return req->created_at_ms + req->timeout_ms;
}

const gptbridge_ipc_request_t* gptbridge_ipc_registry_find(const gptbridge_ipc_registry_t* r, const char* request_id) {
    if (!r || !request_id) return NULL;
    for (int i = 0; i < r->count; ++i) {
        if (strncmp(r->reqs[i].request_id, request_id, GPTBRIDGE_IPC_ID_MAX) == 0) return &r->reqs[i];
    }
    return NULL;
}

int gptbridge_ipc_registry_count(const gptbridge_ipc_registry_t* r) { return r ? r->count : 0; }

/* Transport stub */
static gptbridge_ipc_transport_msg_t _slot;
static int _has = 0;
int gptbridge_ipc_transport_send(const gptbridge_ipc_transport_msg_t* msg) {
    if (!msg) return 0;
    _slot = *msg;
    _has = 1;
    return 1;
}
int gptbridge_ipc_transport_recv(gptbridge_ipc_transport_msg_t* out) {
    if (!out || !_has) return 0;
    *out = _slot;
    _has = 0;
    return 1;
}

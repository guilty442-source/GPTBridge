/* activation.c — E3 啟動面 C23 原型實作 (C23 constexpr/auto) */
#include "activation.h"
#include <string.h>

int gptbridge_activation_init(gptbridge_activation_registry_t* r) {
    if (!r) return 0;
    memset(r, 0, sizeof(*r));
    return 1;
}

int gptbridge_activation_register(gptbridge_activation_registry_t* r, const char* tool_id) {
    if (!r || !tool_id || r->count >= GPTBRIDGE_ACTIVATION_MAX_TOOLS) return 0;
    if (gptbridge_activation_find(r, tool_id)) return 0;
    gptbridge_activation_tool_t* t = &r->tools[r->count];
    strncpy(t->tool_id, tool_id, GPTBRIDGE_ACTIVATION_ID_MAX - 1);
    t->tool_id[GPTBRIDGE_ACTIVATION_ID_MAX - 1] = '\0';
    t->state = GPTBRIDGE_ACTIVATION_IDLE;
    t->last_activate_ms = 0;
    t->activated_count = 0;
    r->count++;
    return 1;
}

int gptbridge_activation_activate(gptbridge_activation_registry_t* r, const char* tool_id, int64_t now_ms) {
    gptbridge_activation_tool_t* t = (gptbridge_activation_tool_t*)gptbridge_activation_find(r, tool_id);
    if (!t) return 0;
    if (t->state == GPTBRIDGE_ACTIVATION_ACTIVE) return 0;
    t->state = GPTBRIDGE_ACTIVATION_ACTIVE;
    t->last_activate_ms = now_ms;
    t->activated_count++;
    return 1;
}

int gptbridge_activation_deactivate(gptbridge_activation_registry_t* r, const char* tool_id) {
    gptbridge_activation_tool_t* t = (gptbridge_activation_tool_t*)gptbridge_activation_find(r, tool_id);
    if (!t) return 0;
    t->state = GPTBRIDGE_ACTIVATION_IDLE;
    return 1;
}

const gptbridge_activation_tool_t* gptbridge_activation_find(const gptbridge_activation_registry_t* r, const char* tool_id) {
    if (!r || !tool_id) return NULL;
    for (int i = 0; i < r->count; ++i) {
        if (strncmp(r->tools[i].tool_id, tool_id, GPTBRIDGE_ACTIVATION_ID_MAX) == 0) return &r->tools[i];
    }
    return NULL;
}

int gptbridge_activation_is_active(const gptbridge_activation_registry_t* r, const char* tool_id) {
    const gptbridge_activation_tool_t* t = gptbridge_activation_find(r, tool_id);
    return t && t->state == GPTBRIDGE_ACTIVATION_ACTIVE;
}

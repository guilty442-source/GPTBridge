/* runtime_state.c — §10.65 E3：模組雙軸狀態登錄簿（star-runtime-state/v1）

實作對齊 Python runtime_state_registry.py；I/O（JSON atomic persist）
由呼叫方負責，本檔純狀態語義。
*/
#include "runtime_state.h"

#include <string.h>

static void _copy(char* dst, size_t cap, const char* src) {
    size_t n;
    if (src == NULL) {
        src = "";
    }
    n = strlen(src);
    if (n >= cap) {
        n = cap - 1;
    }
    memcpy(dst, src, n);
    dst[n] = '\0';
}

static void _copy_trunc(char* dst, size_t cap, const char* src, size_t limit) {
    size_t n;
    if (src == NULL) {
        src = "";
    }
    n = strlen(src);
    if (n > limit) {
        n = limit;
    }
    if (n >= cap) {
        n = cap - 1;
    }
    memcpy(dst, src, n);
    dst[n] = '\0';
}

uint8_t gptbridge_rs_runtime_from_name(const char* name) {
    static const char* names[] = {
        "", "STARTING", "READY", "DEGRADED",
        "RECOVERING", "FAILED", "STOPPING", "STOPPED"};
    int i;
    if (name == NULL) {
        return GPTBRIDGE_RT_UNKNOWN;
    }
    for (i = 1; i <= 7; ++i) {
        if (strcmp(name, names[i]) == 0) {
            return (uint8_t)i;
        }
    }
    return GPTBRIDGE_RT_UNKNOWN;
}

uint8_t gptbridge_rs_capability_from_name(const char* name) {
    static const char* names[] = {
        "", "AVAILABLE", "UNAVAILABLE", "DISABLED", "NOT_INSTALLED"};
    int i;
    if (name == NULL) {
        return GPTBRIDGE_CAP_UNKNOWN;
    }
    for (i = 1; i <= 4; ++i) {
        if (strcmp(name, names[i]) == 0) {
            return (uint8_t)i;
        }
    }
    return GPTBRIDGE_CAP_UNKNOWN;
}

const char* gptbridge_rs_runtime_name(uint8_t state) {
    static const char* names[] = {
        "UNKNOWN", "STARTING", "READY", "DEGRADED",
        "RECOVERING", "FAILED", "STOPPING", "STOPPED"};
    if (state > 7) {
        state = 0;
    }
    return names[state];
}

const char* gptbridge_rs_capability_name(uint8_t state) {
    static const char* names[] = {
        "UNKNOWN", "AVAILABLE", "UNAVAILABLE", "DISABLED", "NOT_INSTALLED"};
    if (state > 4) {
        state = 0;
    }
    return names[state];
}

void gptbridge_rs_init(gptbridge_rs_registry_t* reg) {
    memset(reg, 0, sizeof(*reg));
}

static gptbridge_rs_record_t* _record_for(gptbridge_rs_registry_t* reg,
                                          const char* module_id) {
    int i;
    gptbridge_rs_record_t* free_slot = NULL;
    if (module_id == NULL || module_id[0] == '\0') {
        return NULL;
    }
    for (i = 0; i < GPTBRIDGE_RS_MAX_MODULES; ++i) {
        gptbridge_rs_record_t* rec = &reg->records[i];
        if (rec->in_use) {
            if (strcmp(rec->module_id, module_id) == 0) {
                return rec;
            }
        } else if (free_slot == NULL) {
            free_slot = rec;
        }
    }
    if (free_slot == NULL) {
        return NULL; /* 表滿 → fail-closed（不覆蓋既有記錄） */
    }
    memset(free_slot, 0, sizeof(*free_slot));
    _copy(free_slot->module_id, sizeof(free_slot->module_id), module_id);
    /* Python ModuleRuntimeRecord 預設：STOPPED／AVAILABLE／health=unknown */
    free_slot->runtime_state = GPTBRIDGE_RT_STOPPED;
    free_slot->capability_state = GPTBRIDGE_CAP_AVAILABLE;
    _copy(free_slot->health, sizeof(free_slot->health), "unknown");
    free_slot->in_use = 1;
    reg->count += 1;
    return free_slot;
}

int gptbridge_rs_set_runtime(gptbridge_rs_registry_t* reg,
                             const char* module_id,
                             uint8_t runtime_state,
                             const char* health,
                             const char* release_id,
                             const char* error,
                             const char* now_str) {
    gptbridge_rs_record_t* rec;
    if (reg == NULL || runtime_state == GPTBRIDGE_RT_UNKNOWN
        || runtime_state > GPTBRIDGE_RT_STOPPED) {
        return 0;
    }
    rec = _record_for(reg, module_id);
    if (rec == NULL) {
        return 0;
    }
    rec->runtime_state = runtime_state;
    if (runtime_state == GPTBRIDGE_RT_RECOVERING) {
        rec->recovery_attempts += 1;
    }
    if (health != NULL) {
        _copy(rec->health, sizeof(rec->health), health);
    }
    if (release_id != NULL) {
        _copy(rec->release_id, sizeof(rec->release_id), release_id);
    }
    if (error != NULL) {
        _copy(rec->last_error, sizeof(rec->last_error), error);
    } else if (runtime_state == GPTBRIDGE_RT_READY
               || runtime_state == GPTBRIDGE_RT_STARTING) {
        rec->last_error[0] = '\0';
    }
    _copy(rec->updated_at, sizeof(rec->updated_at), now_str);
    return 1;
}

int gptbridge_rs_set_capability(gptbridge_rs_registry_t* reg,
                                const char* module_id,
                                uint8_t capability_state,
                                const char* now_str) {
    gptbridge_rs_record_t* rec;
    if (reg == NULL || capability_state == GPTBRIDGE_CAP_UNKNOWN
        || capability_state > GPTBRIDGE_CAP_NOT_INSTALLED) {
        return 0;
    }
    rec = _record_for(reg, module_id);
    if (rec == NULL) {
        return 0;
    }
    rec->capability_state = capability_state;
    _copy(rec->updated_at, sizeof(rec->updated_at), now_str);
    return 1;
}

int gptbridge_rs_heartbeat(gptbridge_rs_registry_t* reg,
                           const char* module_id,
                           const char* now_str) {
    gptbridge_rs_record_t* rec = _record_for(reg, module_id);
    if (rec == NULL) {
        return 0;
    }
    _copy(rec->last_heartbeat, sizeof(rec->last_heartbeat), now_str);
    _copy(rec->updated_at, sizeof(rec->updated_at), now_str);
    return 1;
}

int gptbridge_rs_record_error(gptbridge_rs_registry_t* reg,
                              const char* module_id,
                              const char* error,
                              const char* now_str) {
    gptbridge_rs_record_t* rec = _record_for(reg, module_id);
    if (rec == NULL) {
        return 0;
    }
    /* Python: record.last_error = error[:500] */
    _copy_trunc(rec->last_error, sizeof(rec->last_error), error, 500);
    _copy(rec->updated_at, sizeof(rec->updated_at), now_str);
    return 1;
}

const gptbridge_rs_record_t* gptbridge_rs_find(
    const gptbridge_rs_registry_t* reg, const char* module_id) {
    int i;
    if (reg == NULL || module_id == NULL) {
        return NULL;
    }
    for (i = 0; i < GPTBRIDGE_RS_MAX_MODULES; ++i) {
        const gptbridge_rs_record_t* rec = &reg->records[i];
        if (rec->in_use && strcmp(rec->module_id, module_id) == 0) {
            return rec;
        }
    }
    return NULL;
}

int32_t gptbridge_rs_aggregate(const gptbridge_rs_registry_t* reg,
                               int32_t by_runtime[8],
                               int32_t by_capability[5],
                               char failed_ids[][GPTBRIDGE_RS_ID_MAX],
                               int32_t failed_cap,
                               int32_t* failed_count) {
    int i;
    int32_t failed = 0;
    if (reg == NULL) {
        return 0;
    }
    if (by_runtime != NULL) {
        memset(by_runtime, 0, sizeof(int32_t) * 8);
    }
    if (by_capability != NULL) {
        memset(by_capability, 0, sizeof(int32_t) * 5);
    }
    for (i = 0; i < GPTBRIDGE_RS_MAX_MODULES; ++i) {
        const gptbridge_rs_record_t* rec = &reg->records[i];
        if (!rec->in_use) {
            continue;
        }
        if (by_runtime != NULL) {
            by_runtime[rec->runtime_state <= 7
                           ? rec->runtime_state : 0] += 1;
        }
        if (by_capability != NULL) {
            by_capability[rec->capability_state <= 4
                              ? rec->capability_state : 0] += 1;
        }
        if (rec->runtime_state == GPTBRIDGE_RT_FAILED) {
            if (failed_ids != NULL && failed < failed_cap) {
                _copy(failed_ids[failed], GPTBRIDGE_RS_ID_MAX,
                      rec->module_id);
            }
            failed += 1;
        }
    }
    /* failed 清單排序（Python sorted(failed)）——簡易插入排序，界小。 */
    if (failed_ids != NULL) {
        int32_t n = failed < failed_cap ? failed : failed_cap;
        int32_t a, b;
        for (a = 1; a < n; ++a) {
            char key[GPTBRIDGE_RS_ID_MAX];
            memcpy(key, failed_ids[a], sizeof(key));
            b = a - 1;
            while (b >= 0 && strcmp(failed_ids[b], key) > 0) {
                memcpy(failed_ids[b + 1], failed_ids[b],
                       GPTBRIDGE_RS_ID_MAX);
                b -= 1;
            }
            memcpy(failed_ids[b + 1], key, GPTBRIDGE_RS_ID_MAX);
        }
    }
    if (failed_count != NULL) {
        *failed_count = failed;
    }
    return reg->count;
}

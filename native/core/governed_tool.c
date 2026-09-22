/* governed_tool.c — M1 受管工具執行面 ABI 決策自由語義
 * （star-governed-tool-runtime-abi/v1；Python governed_runtime 為權威）
 */
#include "governed_tool.h"
#include "system_rescue.h" /* gptbridge_sr_sha256_hex */

#include <stdio.h>
#include <string.h>
#include <ctype.h>

int gptbridge_gt_tool_id_valid(const char* tool_id) {
    size_t len;
    size_t i;
    if (tool_id == NULL) {
        return 0;
    }
    len = strlen(tool_id);
    if (len < 2 || len > 63) {
        return 0;
    }
    if (!(tool_id[0] >= 'a' && tool_id[0] <= 'z')
        && !(tool_id[0] >= '0' && tool_id[0] <= '9')) {
        return 0;
    }
    for (i = 1; i < len; ++i) {
        const char c = tool_id[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
              || c == '_' || c == '-')) {
            return 0;
        }
    }
    return 1;
}

int gptbridge_gt_session_token_valid(const char* token) {
    /* Python: str(env).strip().lower() 後 fullmatch ^[a-f0-9]{64}$ */
    const char* start = token;
    const char* end;
    size_t len, i;
    if (token == NULL) {
        return 0;
    }
    while (*start != '\0' && isspace((unsigned char)*start)) {
        ++start;
    }
    end = start + strlen(start);
    while (end > start && isspace((unsigned char)end[-1])) {
        --end;
    }
    len = (size_t)(end - start);
    if (len != 64) {
        return 0;
    }
    for (i = 0; i < len; ++i) {
        const char c = (char)tolower((unsigned char)start[i]);
        if (!((c >= 'a' && c <= 'f') || (c >= '0' && c <= '9'))) {
            return 0;
        }
    }
    return 1;
}

int gptbridge_gt_port_valid(int64_t port) {
    return port >= 1024 && port <= 65535;
}

int gptbridge_gt_env_gate(int32_t project_root_ok,
                          int32_t tool_dir_ok,
                          const char* session_token,
                          int64_t port,
                          int32_t bootstrap_ok) {
    if (!project_root_ok || !tool_dir_ok || !bootstrap_ok) {
        return 0;
    }
    if (!gptbridge_gt_session_token_valid(session_token)) {
        return 0;
    }
    if (!gptbridge_gt_port_valid(port)) {
        return 0;
    }
    return 1;
}

int gptbridge_gt_workspace_instance_id(const char* tool_id,
                                       int64_t port,
                                       char* out) {
    char input[96];
    char hex[65];
    if (out == NULL || tool_id == NULL) {
        return 0;
    }
    snprintf(input, sizeof(input), "%s:%lld", tool_id,
             (long long)port);
    if (!gptbridge_sr_sha256_hex((const uint8_t*)input, strlen(input),
                                 hex)) {
        return 0;
    }
    memcpy(out, hex, GPTBRIDGE_GT_INSTANCE_ID_LEN);
    out[GPTBRIDGE_GT_INSTANCE_ID_LEN] = '\0';
    return 1;
}

/* hmac.compare_digest 語義：長度不同即 0（Python 同；長度洩漏屬規格）；
   等長時常數時間 XOR 累積。 */
static int _ct_equal(const char* a, const char* b) {
    size_t la, lb, i;
    unsigned char acc = 0;
    if (a == NULL || b == NULL) {
        return 0;
    }
    la = strlen(a);
    lb = strlen(b);
    if (la != lb) {
        return 0;
    }
    for (i = 0; i < la; ++i) {
        acc |= (unsigned char)(a[i] ^ b[i]);
    }
    return acc == 0;
}

int gptbridge_gt_shutdown_gate(const char* env_token,
                               const char* provided_token) {
    /* env token 空字串 → /shutdown 恆 403（規格 §2） */
    if (env_token == NULL || env_token[0] == '\0') {
        return 0;
    }
    if (provided_token == NULL) {
        return 0;
    }
    return _ct_equal(env_token, provided_token);
}

int gptbridge_gt_ws_gate(const char* session_token,
                         const char* provided_token,
                         const char* expected_instance,
                         const char* provided_instance) {
    char lowered[65];
    size_t len, i;
    if (session_token == NULL || provided_token == NULL
        || expected_instance == NULL || provided_instance == NULL) {
        return 0;
    }
    len = strlen(provided_token);
    if (len >= sizeof(lowered)) {
        return 0;
    }
    for (i = 0; i < len; ++i) {
        lowered[i] = (char)tolower((unsigned char)provided_token[i]);
    }
    lowered[len] = '\0';
    if (!_ct_equal(session_token, lowered)) {
        return 0;
    }
    return _ct_equal(expected_instance, provided_instance);
}

int gptbridge_gt_request_valid(const char* command,
                               int32_t payload_is_dict,
                               const char* request_id,
                               const char* payload_tool_id,
                               const char* self_tool_id) {
    if (command == NULL || command[0] == '\0') {
        return 0;
    }
    if (!payload_is_dict) {
        return 0;
    }
    if (request_id == NULL || request_id[0] == '\0'
        || strlen(request_id) > GPTBRIDGE_GT_REQUEST_ID_MAX) {
        return 0;
    }
    /* payload.tool_id 缺省視為自身；明確指定則必須相符 */
    if (payload_tool_id != NULL && payload_tool_id[0] != '\0'
        && self_tool_id != NULL
        && strcmp(payload_tool_id, self_tool_id) != 0) {
        return 0;
    }
    if (self_tool_id == NULL) {
        return 0;
    }
    return 1;
}

int64_t gptbridge_gt_idle_next_ms(int64_t current_ms, int32_t notified) {
    /* Python: notify 命中或拿到 request → idle_poll 重置 0.25s；
       逾時 → min(idle_poll*1.5, 0.5s)。 */
    if (notified) {
        return GPTBRIDGE_GT_IDLE_INITIAL_MS;
    }
    if (current_ms <= 0) {
        current_ms = GPTBRIDGE_GT_IDLE_INITIAL_MS;
    }
    current_ms = current_ms + current_ms / 2; /* ×1.5 */
    if (current_ms > GPTBRIDGE_GT_IDLE_MAX_MS) {
        current_ms = GPTBRIDGE_GT_IDLE_MAX_MS;
    }
    return current_ms;
}

int64_t gptbridge_gt_wait_timeout_ms(int64_t idle_poll_ms,
                                     int32_t notify_pending) {
    /* Python: 0.05 if notify_queue not empty else
       max(idle_poll, 0.05)。 */
    if (notify_pending) {
        return GPTBRIDGE_GT_IDLE_NOTIFY_MS;
    }
    if (idle_poll_ms < GPTBRIDGE_GT_IDLE_NOTIFY_MS) {
        return GPTBRIDGE_GT_IDLE_NOTIFY_MS;
    }
    return idle_poll_ms;
}

int gptbridge_gt_health_degraded(int32_t consecutive_failures) {
    return consecutive_failures >= GPTBRIDGE_GT_HEALTH_DEGRADE_AT;
}

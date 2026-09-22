/* activation.h — E3 啟動面 C 原型（ §10.65 E3 ）

對應 Python `runtime_state` + `model_service_activation.ModelServiceActivationBroker`：
  - 行程啟停與 activation broker 為執行行為
  - broker 的 admission 判定維持回 Python 治理路徑（C 側不判定權限，僅 fail-closed 轉發）

純 C11，shadow 模式。
*/
#ifndef GPTBRIDGE_ACTIVATION_H
#define GPTBRIDGE_ACTIVATION_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GPTBRIDGE_ACTIVATION_MAX_TOOLS 16
#define GPTBRIDGE_ACTIVATION_ID_MAX 64

typedef enum {
    GPTBRIDGE_ACTIVATION_IDLE = 0,
    GPTBRIDGE_ACTIVATION_ACTIVATING = 1,
    GPTBRIDGE_ACTIVATION_ACTIVE = 2,
    GPTBRIDGE_ACTIVATION_FAILED = 3
} gptbridge_activation_state_t;

typedef struct {
    char tool_id[GPTBRIDGE_ACTIVATION_ID_MAX];
    gptbridge_activation_state_t state;
    int64_t last_activate_ms;
    int32_t activated_count;
} gptbridge_activation_tool_t;

typedef struct {
    gptbridge_activation_tool_t tools[GPTBRIDGE_ACTIVATION_MAX_TOOLS];
    int32_t count;
} gptbridge_activation_registry_t;

int gptbridge_activation_init(gptbridge_activation_registry_t* r);
int gptbridge_activation_register(gptbridge_activation_registry_t* r, const char* tool_id);
int gptbridge_activation_activate(gptbridge_activation_registry_t* r, const char* tool_id, int64_t now_ms); /* 1=ok, 0=already active or not found */
int gptbridge_activation_deactivate(gptbridge_activation_registry_t* r, const char* tool_id);
const gptbridge_activation_tool_t* gptbridge_activation_find(const gptbridge_activation_registry_t* r, const char* tool_id);
int gptbridge_activation_is_active(const gptbridge_activation_registry_t* r, const char* tool_id);

#ifdef __cplusplus
}
#endif

#endif /* GPTBRIDGE_ACTIVATION_H */

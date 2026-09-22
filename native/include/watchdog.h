/* watchdog.h — E1 執行面：連線看門狗狀態機 C 原型（ §10.65 E1 ）

對應 Python `tasks/connection_watchdog.py` 的決策自由執行語義：
  - 狀態機：connected / degraded / disconnected / starting（探針結果注入，C 不開連線）
  - consecutive_dead 含 retry-grace；dead_threshold 觸發 repair 旗標（一次性）
  - 自適應間隔：穩定 >=3 次指數退避至上限；失敗即重置
  - 有界事件環（transition 紀錄，供呼叫方寫審計）

純 C11，無 C++，無 I/O；probe 結果由呼叫方注入（A177：C 層不開連線）。
shadow 模式：與 Python 並行比對，Python 為權威。
*/
#ifndef GPTBRIDGE_WATCHDOG_H
#define GPTBRIDGE_WATCHDOG_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GPTBRIDGE_WD_EVENT_MAX 32

typedef enum {
    GPTBRIDGE_WD_UNKNOWN = 0,
    GPTBRIDGE_WD_CONNECTED = 1,
    GPTBRIDGE_WD_DEGRADED = 2,
    GPTBRIDGE_WD_DISCONNECTED = 3,
    GPTBRIDGE_WD_STARTING = 4
} gptbridge_wd_state_t;

typedef struct {
    gptbridge_wd_state_t from_state;
    gptbridge_wd_state_t to_state;
    int32_t trigger_repair;   /* 0/1 */
    int64_t at_ms;
} gptbridge_wd_event_t;

typedef struct {
    gptbridge_wd_state_t state;
    int32_t consecutive_dead;
    int64_t probe_count;
    int32_t repair_triggered; /* 0/1，connected 時復位 */
    int32_t consecutive_stable;
    int64_t adaptive_interval_ms;
    int64_t min_interval_ms;
    int64_t max_interval_ms;
    int32_t dead_threshold;
    int32_t retry_grace;
    gptbridge_wd_event_t events[GPTBRIDGE_WD_EVENT_MAX];
    int32_t event_head;  /* ring 寫入位置 */
    int32_t event_count;
} gptbridge_wd_t;

/* 一次探針輸入（由呼叫方注入實測結果） */
typedef struct {
    int32_t backend_process_alive; /* 0/1 */
    int32_t backend_http_healthy;  /* 0/1 */
    int32_t frontend_connected;    /* 0/1 */
} gptbridge_wd_probe_t;

int gptbridge_wd_init(gptbridge_wd_t* wd,
                      int64_t min_interval_ms,
                      int64_t max_interval_ms,
                      int32_t dead_threshold,
                      int32_t retry_grace);

/* 狀態計算（對齊 Python _compute_state） */
gptbridge_wd_state_t gptbridge_wd_compute_state(const gptbridge_wd_probe_t* p);

/* 一次探針：更新狀態機；回傳 1=觸發 repair（一次性，connected 前不重複），0=未觸發。
   out_event 非 NULL 且發生轉移時回填事件。 */
int gptbridge_wd_probe(gptbridge_wd_t* wd,
                       const gptbridge_wd_probe_t* probe,
                       int64_t now_ms,
                       gptbridge_wd_event_t* out_event);

/* 下一探針間隔（自適應）：穩定 >=3 次 → ×1.5 至上限；非 connected → 重置下限 */
int64_t gptbridge_wd_next_interval_ms(gptbridge_wd_t* wd);

#ifdef __cplusplus
}
#endif

#endif /* GPTBRIDGE_WATCHDOG_H */

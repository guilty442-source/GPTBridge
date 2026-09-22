/* activation_broker.h — E3 啟動面：按需啟動 broker 決策機 C 原型（ §10.65 E3 ）

對應 Python `tasks/model_service_activation.py`
（ModelServiceActivationBroker）的決策自由執行語義：

  - pending 檢測／maintenance_ready／shutting_down／admission_hold／
    owner liveness／regulation_active 皆由呼叫方注入（SQL 探針與治理
    判定留在 Python——A297 決策層／執行層分離；藍圖明定 broker 的
    admission 判定維持回 Python 治理路徑）
  - 節流：next_attempt_at 冷卻；失敗時 backoff ×2 至上限
  - 自動釋放：regulation_active 且無 pending 時，僅釋放本 broker
    啟動的 owner（broker_started_owner）
  - 顯式停止記憶：note_explicit_owner_stop → explicit_stop_at＋
    broker_started_owner 清零＋下一嘗試冷卻
  - SHOULD_START／SHOULD_RELEASE 表示呼叫方須執行 toolbox 呼叫，
    結果回報 on_start_result／on_release_result（決策→執行邊界）
  - 狀態寫盤節流：fingerprint 變更或 >=60s 心跳（§10.63 R2）

純 C11，無 C++，無 I/O，無 SQL。shadow 模式：Python 為權威。
*/
#ifndef GPTBRIDGE_ACTIVATION_BROKER_H
#define GPTBRIDGE_ACTIVATION_BROKER_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 決策碼——與 Python _ensure_inner 回傳字串一一對應；
   SHOULD_START／SHOULD_RELEASE 為執行委託碼（Python 此處直接 await）。 */
typedef enum {
    GPTBRIDGE_ACT_IDLE = 0,                 /* "idle" */
    GPTBRIDGE_ACT_MAINTENANCE_PENDING = 1,  /* "maintenance-pending" */
    GPTBRIDGE_ACT_SHUTTING_DOWN = 2,        /* "shutting-down" */
    GPTBRIDGE_ACT_RESOURCE_HOLD = 3,        /* "resource-hold" */
    GPTBRIDGE_ACT_OWNER_RUNNING = 4,        /* "owner-running" */
    GPTBRIDGE_ACT_LIVENESS_UNKNOWN = 5,     /* "liveness-unknown" */
    GPTBRIDGE_ACT_THROTTLED = 6,            /* "throttled" */
    GPTBRIDGE_ACT_SHOULD_START = 7,         /* 委託 start_tool */
    GPTBRIDGE_ACT_STARTED = 8,              /* "started" */
    GPTBRIDGE_ACT_START_FAILED = 9,         /* "start-failed" */
    GPTBRIDGE_ACT_RELEASE_COOLDOWN = 10,    /* "release-cooldown" */
    GPTBRIDGE_ACT_SHOULD_RELEASE = 11,      /* 委託 stop_tool */
    GPTBRIDGE_ACT_RELEASED = 12,            /* "released" */
    GPTBRIDGE_ACT_RELEASE_FAILED = 13       /* "release-failed" */
} gptbridge_act_decision_t;

typedef struct {
    /* 設定（Python __init__ 夾取後值） */
    double cooldown_s;
    double min_backoff_s;
    double max_backoff_s;
    /* 運行狀態 */
    double next_attempt_at;
    double next_release_at;
    double backoff_s;
    int32_t attempts;
    int32_t broker_started_owner; /* 0/1 */
    double explicit_stop_at;      /* wall time；0=無 */
} gptbridge_act_broker_t;

/* 一次判定的注入輸入（全部來自 Python 治理／探針面） */
typedef struct {
    int32_t pending;            /* 新鮮 queued ai 請求存在 */
    int32_t maintenance_ready;  /* app.maintenance_ready */
    int32_t shutting_down;      /* app._shutting_down */
    int32_t admission_hold;     /* resource governor worker hold */
    int32_t liveness_known;     /* tool_process_active 呼叫成功 */
    int32_t owner_active;       /* liveness_known=1 時的活性 */
    int32_t regulation_active;  /* resource governor regulating */
    double now_monotonic;
} gptbridge_act_inputs_t;

int gptbridge_act_init(gptbridge_act_broker_t* broker,
                       double cooldown_s,
                       double min_backoff_s,
                       double max_backoff_s);

/* 對齊 _ensure_inner：回傳 SHOULD_START／SHOULD_RELEASE 時呼叫方執行
   對應 toolbox 呼叫，再以 on_start_result／on_release_result 回報。 */
gptbridge_act_decision_t gptbridge_act_ensure(
    gptbridge_act_broker_t* broker,
    const gptbridge_act_inputs_t* inputs);

/* start_tool 結果：ok→冷卻＋backoff 重置＋broker_started_owner=1→STARTED；
   失敗→delay=backoff、backoff×2 夾上限、next_attempt_at=now+delay
   →START_FAILED。回傳值即 Python 決策字串對應碼。 */
gptbridge_act_decision_t gptbridge_act_on_start_result(
    gptbridge_act_broker_t* broker, int32_t ok, double now_monotonic);

/* stop_tool 結果：next_release_at=now+cooldown（不論成敗）；
   ok→broker_started_owner=0→RELEASED，否則 RELEASE_FAILED。 */
gptbridge_act_decision_t gptbridge_act_on_release_result(
    gptbridge_act_broker_t* broker, int32_t ok, double now_monotonic);

/* 顯式停止記憶（note_explicit_owner_stop） */
void gptbridge_act_note_explicit_stop(gptbridge_act_broker_t* broker,
                                      double now_monotonic,
                                      double wall_time);

/* 輪詢間隔：pending ? pending_interval : idle_interval */
double gptbridge_act_poll_interval(int32_t pending,
                                   double idle_interval_s,
                                   double pending_interval_s);

/* §10.63 R2 狀態寫盤節流：fingerprint 變更或 >=heartbeat_s → 1 該寫 */
int gptbridge_act_state_write_due(int32_t fingerprint_changed,
                                  double now_monotonic,
                                  double last_write_at,
                                  double heartbeat_s);

const char* gptbridge_act_decision_name(gptbridge_act_decision_t d);

#ifdef __cplusplus
}
#endif

#endif /* GPTBRIDGE_ACTIVATION_BROKER_H */

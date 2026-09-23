/* channel_runtime.h — M2 A263 channel 非同步執行面（C++ 骨架）
 * （module-language-migration-order-20260922 M2 information-channel-gateway）
 *
 * 對應 Python `shared-layer/src/shared_layer/channel_runtime.py`
 * ＋ `connection_mixin.py` ＋ `heartbeat_mixin.py` ＋
 * `transactional_outbox.py` 的**執行面**語義：
 *
 *  - connect：generation+1 → CONNECTING → hello → OPEN；
 *    heartbeat_dead 清除；send/receive/heartbeat 迴圈啟動
 *  - _send_loop：control 佇列先排空 → outbox fetch_after 視窗依序送
 *    state_event（sent_upto 逐事件推進）→ 一般訊息依 MessagePriority
 *    值穩定排序整批送出 → 阻塞在 wakeup，逾時 send_idle_sleep
 *  - _receive_loop/dispatch：heartbeat_pong 更新 last_pong；
 *    state_event_ack 單調前進 acked_cursor；state_event_resync →
 *    雙游標重置＋outbox 回放（sent_upto 重置後由 send loop 重送）
 *  - heartbeat deadline：monotonic - last_pong > timeout → dead＋
 *    transport.close(1001,"heartbeat_timeout")
 *  - reconnect：snapshot verify（cursor>=0）→ attempts++ > max →
 *    DEAD＋失敗；否則 close(1001) → 指數退避 → generation+1 →
 *    resync → OPEN
 *  - outbox 事件持有＋回放：sequence=latest+1、idempotency_key
 *    "channel:seq"、fetch 視窗 [cursor+1, min(cursor+1+limit, latest+1))
 *
 * 判定一律委派 `a263_channel_core`（Python 等值語義 C 核心）；
 * transport（send/receive/close）、token、WS/SQLite 由呼叫方注入
 * （模式 B 邊界不變——本檔只做線上機械語義）。
 */
#ifndef GPTBRIDGE_CHANNEL_RUNTIME_H
#define GPTBRIDGE_CHANNEL_RUNTIME_H

#include <atomic>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "jsonlite.h"

namespace gptbridge {
namespace chan {

/* 對齊 channel_types.MessagePriority（值即排序鍵）。 */
enum class MessagePriority : int32_t {
    CONTROL = 0,
    STATE = 1,
    COMMAND = 2,
};

struct ChannelRuntimeConfig {
    std::string channel_id;
    uint32_t max_queue_size = 1000;
    double heartbeat_interval_seconds = 10.0;
    double heartbeat_timeout_seconds = 30.0;
    uint32_t reconnect_max_attempts = 3;
    double reconnect_base_delay_seconds = 1.0;
    uint32_t control_channel_capacity = 100;
    bool enable_backpressure = true;
    uint32_t max_outbound_batch = 100;
    double send_idle_sleep_seconds = 0.001;
    /* §10.65 dual-track 標記：此 runtime 在 shadow/parity 觀察期間
       與 Python 正典並行時置 true；primary 切換後應為 false。
       僅觀測語意——不影響行為，稽核可據此拒絕「假 primary」。 */
    bool dual_track = false;
};

struct ChannelHooks {
    /* transport.send（必備）：回 false＝傳輸層失敗 → heartbeat_dead。 */
    std::function<bool(const jsonlite::JsonValue& message)> send;
    /* transport.receive（可空）：阻塞式取下一則訊息；回 false 或
       Null → 串流結束（receive 迴圈退出）。空 → 不啟動 receive 執行緒，
       由呼叫方自行 handle_incoming。 */
    std::function<bool(jsonlite::JsonValue* out)> receive;
    /* transport.close（必備）。 */
    std::function<void(int code, const std::string& reason)> close;
    /* 觀測 callback（可空）：state 字串對（"open" 等 .value）。 */
    std::function<void(const std::string& old_state,
                       const std::string& new_state)> on_state_change;
    std::function<void(const jsonlite::JsonValue& message)> on_message;
    std::function<void(const jsonlite::JsonValue& message)> on_control;
    /* 單調時鐘（可注入；預設 steady_clock 秒）。心跳判定與 reconnect
       退避都走此鐘，測試可注入假時鐘。 */
    std::function<double()> monotonic;
    /* 睡眠（可注入；預設 sleep_for 秒）。reconnect 退避用。 */
    std::function<void(double seconds)> sleep;
};

/* Outbox 持有的事件（TransactionalOutbox._events[seq] 等價物）。 */
struct OutboxEvent {
    uint64_t sequence = 0;
    std::string entity_id;
    std::string entity_type;
    std::string operation;
    jsonlite::JsonValue payload;
    std::string state_hash;
    std::string idempotency_key;
    std::string timestamp; /* ISO8601 UTC（Python 同） */
};

class A263ChannelRuntime {
public:
    A263ChannelRuntime();
    ~A263ChannelRuntime();
    A263ChannelRuntime(const A263ChannelRuntime&) = delete;
    A263ChannelRuntime& operator=(const A263ChannelRuntime&) = delete;

    bool start(const ChannelRuntimeConfig& config, ChannelHooks hooks,
               std::string* err);

    /* ConnectionMixin.connect：generation+1、CONNECTING→OPEN、
       hello 入 control 佇列、啟動 send/receive/heartbeat 迴圈。 */
    bool connect(const std::string& backend_generation = "",
                 const std::string& session_id = "");

    /* ConnectionMixin.disconnect：dead → 收執行緒 → close → CLOSED。 */
    void disconnect(int code = 1000, const std::string& reason = "");

    /* ConnectionMixin.reconnect：snapshot 驗證失敗或 attempts>max →
       false（後者置 DEAD）。 */
    bool reconnect(int64_t snapshot_cursor, const std::string& snapshot_hash,
                   const std::string& backend_generation = "",
                   const std::string& session_id = "");

    /* 佇列入口（send loop 來源）。回 false＝backpressure 拒收。 */
    bool enqueue_control(const jsonlite::JsonValue& message);
    bool send_message(MessagePriority priority,
                      const jsonlite::JsonValue& message);

    /* TransactionalOutbox.append：回新 sequence；channel_id 為空 → 0
       （fail-closed）。 */
    uint64_t append_state_event(const std::string& entity_id,
                                const std::string& entity_type,
                                const std::string& operation,
                                const jsonlite::JsonValue& payload,
                                const std::string& state_hash = "");

    /* _dispatch_message/_handle_control：ack/pong/resync/hello/session。 */
    void handle_incoming(const jsonlite::JsonValue& message);

    /* 心跳 deadline 檢查（heartbeat_loop 每 interval 呼叫一次；測試
       亦可直接呼叫）。逾期 → dead＋close(1001,"heartbeat_timeout")。 */
    void poll_heartbeat();

    /* 觀測（執行緒安全快照）。 */
    std::string state_name() const;
    uint64_t generation() const;
    uint64_t acked_cursor() const;
    uint64_t sent_upto() const;
    uint64_t latest_sequence() const;
    int64_t messages_sent() const;
    int64_t messages_received() const;
    int64_t reconnects() const;
    bool heartbeat_dead() const;
    bool running() const;
    /* §10.65 dual-track 標記（config 直通）。 */
    bool dual_track() const;

private:
    void send_loop();
    void receive_loop();
    void heartbeat_loop();
    void set_state(int32_t new_state);
    void start_threads();
    void join_threads();
    jsonlite::JsonValue generation_dict() const;
    void enqueue_hello();
    void enqueue_resync(uint64_t cursor);
    void enqueue_session_info();

    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace chan
} // namespace gptbridge

#endif /* GPTBRIDGE_CHANNEL_RUNTIME_H */

/* a263_channel_core.h — M2 前置：A263 channel 決定性語義 C 核心
 * （module-language-migration-order-20260922 M2；§10.65 shadow 模式）
 *
 * 對應 Python
 * `shared-layer/src/shared_layer/channel_runtime.py`
 * `shared-layer/src/shared_layer/connection_mixin.py`
 * `shared-layer/src/shared_layer/heartbeat_mixin.py`
 * `shared-layer/src/shared_layer/transactional_outbox.py`
 * 的決定性（無 I/O）語義：
 *
 *  - ChannelState 列舉與名稱（Python .value 字串）
 *  - generation 遞增（connect/reconnect 皆 +1）
 *  - reconnect 嘗試上限 → DEAD 判定＋指數退避 base*2^(attempt-1)
 *  - heartbeat deadline：monotonic - last_pong > timeout → dead
 *  - ack cursor 單調遞增（只在更大時前進）
 *  - resync 收斂：acked_cursor 與 sent_upto 同設為 peer cursor
 *  - outbox fetch_after 視窗 [cursor+1, min(cursor+1+limit, latest+1))
 *  - outbox sequence = latest+1；idempotency_key/full_id = "channel:seq"
 *  - backpressure：queue_len < capacity（control queue 恆強制）
 *  - message batch 依 MessagePriority 值穩定排序（Python sort 穩定）
 *  - snapshot verify：cursor >= 0
 *
 * transport／WebSocket／SQLite store／token 簽發留在呼叫方 Python
 * 治理路徑——本檔純 C11、無 I/O、零副作用。
 * shadow 模式：與 Python 並行比對，Python 為權威。
 */
#ifndef GPTBRIDGE_A263_CHANNEL_CORE_H
#define GPTBRIDGE_A263_CHANNEL_CORE_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ChannelState（對齊 channel_types.ChannelState .value） */
typedef enum {
    GPTBRIDGE_A263_STATE_CLOSED = 0,
    GPTBRIDGE_A263_STATE_CONNECTING = 1,
    GPTBRIDGE_A263_STATE_OPEN = 2,
    GPTBRIDGE_A263_STATE_RECONNECTING = 3,
    GPTBRIDGE_A263_STATE_DEAD = 4
} gptbridge_a263_state_t;

/* MessagePriority（對齊 channel_types.MessagePriority .value） */
typedef enum {
    GPTBRIDGE_A263_PRIORITY_CONTROL = 0,
    GPTBRIDGE_A263_PRIORITY_STATE = 1,
    GPTBRIDGE_A263_PRIORITY_COMMAND = 2
} gptbridge_a263_priority_t;

/* reconnect 判定（Python: attempts += 1; if attempts > max → DEAD + raise） */
typedef enum {
    GPTBRIDGE_A263_RECONNECT_OK = 0,
    GPTBRIDGE_A263_RECONNECT_DEAD = 1
} gptbridge_a263_reconnect_verdict_t;

/* ChannelState → Python .value 字串（"closed"/"open"/...） */
const char* gptbridge_a263_state_name(gptbridge_a263_state_t state);

/* connect()/reconnect() 皆令 generation+1。 */
uint64_t gptbridge_a263_next_generation(uint64_t current_generation);

/* reconnect 上限判定：attempts_after_increment > max_attempts → DEAD。 */
gptbridge_a263_reconnect_verdict_t gptbridge_a263_reconnect_verdict(
    uint32_t attempts_after_increment,
    uint32_t max_attempts);

/* 指數退避：base * 2^(attempt-1)。attempt 依 Python 語義從 1 起算；
   attempt == 0 時 fail-closed 視同 attempt 1（exponent 0）。 */
double gptbridge_a263_reconnect_delay_seconds(
    double base_seconds,
    uint32_t attempt);

/* heartbeat deadline：now - last_pong > timeout → expired（嚴格大於，
   對齊 heartbeat_mixin._heartbeat_loop 判定）。 */
int gptbridge_a263_heartbeat_expired(
    double now_monotonic,
    double last_pong_received,
    double timeout_seconds);

/* state_event_ack：cursor 僅在更大時前進（_handle_control ack 分支）。 */
uint64_t gptbridge_a263_ack_advance(uint64_t current, uint64_t incoming);

/* _handle_resync：acked_cursor 與 sent_upto 同設為 peer cursor。 */
void gptbridge_a263_resync_cursors(
    uint64_t peer_cursor,
    uint64_t* acked_cursor,
    uint64_t* sent_upto);

/* TransactionalOutbox.fetch_after 視窗：Python range(cursor+1,
   min(cursor+1+limit, sequence+1))。回傳 1 表視窗非空並填
   *first_seq/*end_seq_exclusive；空視窗回 0（兩指標仍被填為 cursor+1）。
   present 集合由呼叫方過濾（本檔不持有事件表）。 */
int gptbridge_a263_fetch_window(
    uint64_t cursor,
    uint64_t latest_sequence,
    uint32_t limit,
    uint64_t* first_seq,
    uint64_t* end_seq_exclusive);

/* TransactionalOutbox.append：sequence = latest+1。 */
uint64_t gptbridge_a263_outbox_next_sequence(uint64_t latest_sequence);

/* idempotency_key = f"{channel_id}:{sequence}"；同式亦為
   ChannelGeneration.full_id。channel_id NULL → 回 0（fail-closed）；
   buf 不足 → 回 0。成功回 1。 */
int gptbridge_a263_sequence_key(
    const char* channel_id,
    uint64_t sequence,
    char* buf,
    size_t buflen);

/* backpressure：enable_backpressure 為真時 queue_len >= capacity → 拒；
   停用時恆收。control queue 呼叫方應以 enabled=1 呼叫（Python
   _enqueue_control 無條件檢查容量）。 */
int gptbridge_a263_enqueue_allowed(
    uint32_t queue_len,
    uint32_t capacity,
    int backpressure_enabled);

/* _send_loop 第 3 段：message batch 依 priority 值穩定排序
   （Python list.sort 穩定——同值保持原 FIFO 順序）。
   priorities[] 為各訊息優先值，count 為筆數；order_out[] 填入排序後
   的原索引序列（長度須 >= count）。回 1 成功；NULL/負 count → 0。 */
int gptbridge_a263_drain_order(
    const int32_t* priorities,
    int32_t count,
    int32_t* order_out);

/* _verify_snapshot：cursor >= 0 → 1（Python 現行 stub 語義）。 */
int gptbridge_a263_verify_snapshot(int64_t snapshot_cursor);

#ifdef __cplusplus
}
#endif

#endif /* GPTBRIDGE_A263_CHANNEL_CORE_H */

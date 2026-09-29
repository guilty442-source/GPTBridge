/* channel_runtime.cpp — M2 A263 channel 非同步執行面實作（生命週期/
 * 入隊/outbox 事件段）。見 channel_runtime.h；判定委派
 * a263_channel_core（Python 權威）。迴圈與連線段見
 * channel_runtime_loops.cpp（B94）。
 */
#include "channel_runtime_internal.h"

namespace gptbridge {
namespace chan {

namespace jl = jsonlite;

A263ChannelRuntime::A263ChannelRuntime() : impl_(new Impl) {}
A263ChannelRuntime::~A263ChannelRuntime() {
    disconnect(1000, "dtor");
    join_threads();
}

bool A263ChannelRuntime::start(const ChannelRuntimeConfig& config,
                               ChannelHooks hooks, std::string* err) {
    if (config.channel_id.empty()) {
        if (err) *err = "channel_id required";
        return false;
    }
    if (!hooks.send || !hooks.close) {
        if (err) *err = "send/close hooks required";
        return false;
    }
    impl_->cfg = config;
    impl_->hooks = std::move(hooks);
    impl_->state.store(GPTBRIDGE_A263_STATE_CLOSED);
    return true;
}

void A263ChannelRuntime::set_state(int32_t new_state) {
    const int32_t old = impl_->state.exchange(new_state);
    if (old == new_state) return;
    if (impl_->hooks.on_state_change) {
        try {
            impl_->hooks.on_state_change(
                gptbridge_a263_state_name(
                    static_cast<gptbridge_a263_state_t>(old)),
                gptbridge_a263_state_name(
                    static_cast<gptbridge_a263_state_t>(new_state)));
        } catch (...) {
        }
    }
}

jl::JsonValue A263ChannelRuntime::generation_dict() const {
    return jobj({{"channel_id", jstr(impl_->cfg.channel_id)},
                 {"generation", jnum(
                     static_cast<double>(impl_->generation.load()))},
                 {"created_at", jstr(impl_->gen_created_at)},
                 {"backend_generation", jstr(impl_->backend_generation)},
                 {"session_id", jstr(impl_->session_id)}});
}

void A263ChannelRuntime::enqueue_hello() {
    /* _send_hello：control hello 攜 acked_cursor。 */
    enqueue_control(jobj(
        {{"type", jstr("control")},
         {"command", jstr("state_event_hello")},
         {"payload",
          jobj({{"cursor",
                 jnum(static_cast<double>(impl_->acked_cursor.load()))}})},
         {"generation", generation_dict()}}));
}

void A263ChannelRuntime::enqueue_resync(uint64_t cursor) {
    enqueue_control(jobj(
        {{"type", jstr("control")},
         {"command", jstr("state_event_resync")},
         {"payload",
          jobj({{"cursor", jnum(static_cast<double>(cursor))}})},
         {"generation", generation_dict()}}));
}

void A263ChannelRuntime::enqueue_session_info() {
    /* _send_session_info：hello 回應（session/generation/cursor/latest）。 */
    enqueue_control(jobj(
        {{"type", jstr("control")},
         {"command", jstr("state_event_session")},
         {"payload",
          jobj({{"session_id", jstr(impl_->session_id)},
                {"backend_generation", jstr(impl_->backend_generation)},
                {"cursor",
                 jnum(static_cast<double>(impl_->acked_cursor.load()))},
                {"latest_sequence",
                 jnum(static_cast<double>(impl_->latest_seq.load()))}})},
         {"generation", generation_dict()}}));
}

bool A263ChannelRuntime::enqueue_control(const jl::JsonValue& message) {
    {
        std::lock_guard<std::mutex> lk(impl_->mu);
        /* Python _enqueue_control 無條件檢查容量 → enabled=1。 */
        if (!gptbridge_a263_enqueue_allowed(
                static_cast<uint32_t>(impl_->control_queue.size()),
                impl_->cfg.control_channel_capacity, 1))
            return false;
        impl_->control_queue.push_back(message);
    }
    impl_->wakeup.notify_all();
    return true;
}

bool A263ChannelRuntime::send_message(MessagePriority priority,
                                      const jl::JsonValue& message) {
    {
        std::lock_guard<std::mutex> lk(impl_->mu);
        if (!gptbridge_a263_enqueue_allowed(
                static_cast<uint32_t>(impl_->message_queue.size()),
                impl_->cfg.max_queue_size,
                impl_->cfg.enable_backpressure ? 1 : 0))
            return false;
        impl_->message_queue.emplace_back(
            static_cast<int32_t>(priority), message);
        /* Python deque(maxlen)：backpressure 停用時越界靜默丟棄最舊
           訊息（恆回 true）；鏡像同語義。 */
        while (impl_->message_queue.size() > impl_->cfg.max_queue_size)
            impl_->message_queue.pop_front();
    }
    impl_->wakeup.notify_all();
    return true;
}

uint64_t A263ChannelRuntime::append_state_event(
    const std::string& entity_id, const std::string& entity_type,
    const std::string& operation, const jl::JsonValue& payload,
    const std::string& state_hash) {
    std::lock_guard<std::mutex> lk(impl_->mu);
    const uint64_t seq =
        gptbridge_a263_outbox_next_sequence(impl_->latest_seq.load());
    char key[128];
    if (!gptbridge_a263_sequence_key(impl_->cfg.channel_id.c_str(), seq,
                                     key, sizeof(key)))
        return 0; /* fail-closed：channel_id 缺失/緩衝不足 */
    OutboxEvent ev;
    ev.sequence = seq;
    ev.entity_id = entity_id;
    ev.entity_type = entity_type;
    ev.operation = operation;
    ev.payload = payload;
    ev.state_hash = state_hash;
    ev.idempotency_key = key;
    ev.timestamp = utc_now_iso();
    impl_->events[seq] = ev;
    impl_->latest_seq.store(seq);
    impl_->wakeup.notify_all();
    return seq;
}

} // namespace chan
} // namespace gptbridge

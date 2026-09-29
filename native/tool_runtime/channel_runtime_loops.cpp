/* channel_runtime_loops.cpp — M2 A263 channel 執行面：send/receive/
 * heartbeat 迴圈、訊息處理、執行緒管理與 connect/disconnect/reconnect。
 * 與 channel_runtime.cpp 共用 channel_runtime_internal.h（B94）。
 */
#include "channel_runtime_internal.h"

namespace gptbridge {
namespace chan {

namespace jl = jsonlite;

void A263ChannelRuntime::send_loop() {
    /* channel_runtime._send_loop：control 排空 → outbox 視窗 →
       一般批次依 priority 穩定排序 → park 於 wakeup。 */
    while (!impl_->heartbeat_dead.load()) {
        /* 1) control channel */
        for (;;) {
            jl::JsonValue control;
            {
                std::lock_guard<std::mutex> lk(impl_->mu);
                if (impl_->control_queue.empty()) break;
                control = impl_->control_queue.front();
                impl_->control_queue.pop_front();
            }
            if (!impl_->transport_send(control)) {
                impl_->heartbeat_dead.store(true);
                return;
            }
            impl_->messages_sent.fetch_add(1);
        }

        /* 2) outbox state events（sent_upto 視窗、依序推送）。 */
        for (;;) {
            uint64_t first = 0, end = 0;
            const uint64_t sent = impl_->sent_upto.load();
            const uint64_t latest = impl_->latest_seq.load();
            if (!gptbridge_a263_fetch_window(
                    sent, latest, impl_->cfg.max_outbound_batch,
                    &first, &end))
                break;
            bool progressed = false;
            for (uint64_t seq = first; seq < end; ++seq) {
                OutboxEvent ev;
                {
                    std::lock_guard<std::mutex> lk(impl_->mu);
                    auto it = impl_->events.find(seq);
                    if (it == impl_->events.end()) continue;
                    ev = it->second;
                }
                jl::JsonValue evj = jobj(
                    {{"sequence", jnum(static_cast<double>(ev.sequence))},
                     {"entity_id", jstr(ev.entity_id)},
                     {"entity_type", jstr(ev.entity_type)},
                     {"operation", jstr(ev.operation)},
                     {"payload", ev.payload},
                     {"state_hash", jstr(ev.state_hash)},
                     {"idempotency_key", jstr(ev.idempotency_key)},
                     {"timestamp", jstr(ev.timestamp)}});
                jl::JsonValue msg = jobj(
                    {{"type", jstr("state_event")},
                     {"event", evj},
                     {"generation", generation_dict()}});
                if (!impl_->transport_send(msg)) {
                    impl_->heartbeat_dead.store(true);
                    return;
                }
                impl_->sent_upto.store(seq);
                impl_->messages_sent.fetch_add(1);
                progressed = true;
            }
            if (!progressed) break;
        }

        /* 3) regular message batch（priority 值穩定排序）。 */
        {
            std::vector<std::pair<int32_t, jl::JsonValue>> batch;
            {
                std::lock_guard<std::mutex> lk(impl_->mu);
                batch.assign(impl_->message_queue.begin(),
                             impl_->message_queue.end());
                impl_->message_queue.clear();
            }
            if (!batch.empty()) {
                std::vector<int32_t> pri(batch.size());
                std::vector<int32_t> order(batch.size());
                for (size_t i = 0; i < batch.size(); ++i)
                    pri[i] = batch[i].first;
                if (gptbridge_a263_drain_order(
                        pri.data(), static_cast<int32_t>(pri.size()),
                        order.data())) {
                    for (int32_t idx : order) {
                        if (!impl_->transport_send(batch[idx].second)) {
                            impl_->heartbeat_dead.store(true);
                            return;
                        }
                        impl_->messages_sent.fetch_add(1);
                    }
                }
            }
        }

        /* park：等 enqueue/逾時喚醒（Python wait_for(send_idle_sleep)）。 */
        std::unique_lock<std::mutex> lk(impl_->mu);
        impl_->wakeup.wait_for(
            lk, std::chrono::duration<double>(
                    impl_->cfg.send_idle_sleep_seconds));
    }
}

void A263ChannelRuntime::receive_loop() {
    /* _receive_loop：transport.receive → dispatch；null/失敗 → 結束。 */
    while (!impl_->heartbeat_dead.load()) {
        jl::JsonValue msg;
        bool ok = false;
        try {
            ok = impl_->hooks.receive && impl_->hooks.receive(&msg);
        } catch (...) {
            ok = false;
        }
        if (!ok || msg.type == jl::JsonValue::Type::Null) break;
        handle_incoming(msg);
    }
    /* Python：例外 → heartbeat_dead.set()；串流正常結束不設。 */
}

void A263ChannelRuntime::heartbeat_loop() {
    /* _heartbeat_loop：每 interval 檢查一次 deadline（Python：sleep →
       ping → 檢查）。以 wakeup CV 實現可中斷等待——enqueue notify
       不縮短間隔（提早醒來未屆期會繼續睡），dead/disconnect 的
       notify 才即刻退出（對應 task.cancel()）。 */
    double next_check = impl_->now() + impl_->cfg.heartbeat_interval_seconds;
    while (!impl_->heartbeat_dead.load()) {
        {
            std::unique_lock<std::mutex> lk(impl_->mu);
            while (!impl_->heartbeat_dead.load() &&
                   impl_->now() < next_check) {
                impl_->wakeup.wait_for(
                    lk, std::chrono::duration<double>(
                            (std::max)(0.001,
                                       next_check - impl_->now())));
            }
        }
        if (impl_->heartbeat_dead.load()) break;
        poll_heartbeat();
        next_check = impl_->now() + impl_->cfg.heartbeat_interval_seconds;
    }
}

void A263ChannelRuntime::poll_heartbeat() {
    const double now = impl_->now();
    if (gptbridge_a263_heartbeat_expired(
            now, impl_->last_pong.load(),
            impl_->cfg.heartbeat_timeout_seconds)) {
        impl_->heartbeat_dead.store(true);
        impl_->transport_close(1001, "heartbeat_timeout");
        impl_->wakeup.notify_all();
    }
}

void A263ChannelRuntime::handle_incoming(const jl::JsonValue& message) {
    /* _dispatch_message → _handle_control（ack/pong/resync/hello）；
       其餘交 on_message。 */
    impl_->messages_received.fetch_add(1);
    const jl::JsonValue* type = message.get("type");
    const jl::JsonValue* cmd = message.get("command");
    const std::string type_s =
        (type && type->type == jl::JsonValue::Type::String) ? type->string
                                                          : "";
    const std::string cmd_s =
        (cmd && cmd->type == jl::JsonValue::Type::String) ? cmd->string
                                                         : "";
    const jl::JsonValue* payload = message.get("payload");

    if (type_s == "control" || !cmd_s.empty()) {
        if (cmd_s == "heartbeat_pong") {
            impl_->last_pong.store(impl_->now());
        } else if (cmd_s == "state_event_ack") {
            double cursor = 0;
            const jl::JsonValue* c =
                payload ? payload->get("cursor") : nullptr;
            if (c && c->type == jl::JsonValue::Type::Number)
                cursor = c->number;
            impl_->acked_cursor.store(gptbridge_a263_ack_advance(
                impl_->acked_cursor.load(),
                static_cast<uint64_t>(cursor)));
        } else if (cmd_s == "state_event_hello") {
            enqueue_session_info();
        } else if (cmd_s == "state_event_resync") {
            double cursor = 0;
            const jl::JsonValue* c =
                payload ? payload->get("cursor") : nullptr;
            if (c && c->type == jl::JsonValue::Type::Number)
                cursor = c->number;
            /* _handle_resync：雙游標重置 → outbox 回放由 send loop
               對 sent_upto 重取完成（replay_from 等價物）。 */
            uint64_t acked = impl_->acked_cursor.load();
            uint64_t sent = impl_->sent_upto.load();
            gptbridge_a263_resync_cursors(
                static_cast<uint64_t>(cursor), &acked, &sent);
            impl_->acked_cursor.store(acked);
            impl_->sent_upto.store(sent);
            impl_->wakeup.notify_all();
        } else if (cmd_s == "state_event_session") {
            /* peer session info — no-op（Python pass） */
        }
        if (impl_->hooks.on_control) {
            try {
                impl_->hooks.on_control(message);
            } catch (...) {
            }
        }
        return;
    }
    if (impl_->hooks.on_message) {
        try {
            impl_->hooks.on_message(message);
        } catch (...) {
        }
    }
}

void A263ChannelRuntime::start_threads() {
    impl_->threads_running.store(true);
    impl_->send_thread =
        std::thread([this] { send_loop(); });
    if (impl_->hooks.receive)
        impl_->recv_thread =
            std::thread([this] { receive_loop(); });
    impl_->hb_thread = std::thread([this] { heartbeat_loop(); });
}

void A263ChannelRuntime::join_threads() {
    impl_->heartbeat_dead.store(true);
    impl_->wakeup.notify_all();
    if (impl_->send_thread.joinable()) impl_->send_thread.join();
    if (impl_->recv_thread.joinable()) impl_->recv_thread.join();
    if (impl_->hb_thread.joinable()) impl_->hb_thread.join();
    impl_->threads_running.store(false);
}

bool A263ChannelRuntime::connect(const std::string& backend_generation,
                                 const std::string& session_id) {
    /* ConnectionMixin.connect：generation+1 → CONNECTING → 迴圈 →
       hello → OPEN。 */
    impl_->generation.store(
        gptbridge_a263_next_generation(impl_->generation.load()));
    impl_->backend_generation = backend_generation;
    impl_->session_id = session_id;
    impl_->gen_created_at = utc_now_iso();
    impl_->reconnect_attempts = 0;

    set_state(GPTBRIDGE_A263_STATE_CONNECTING);
    impl_->heartbeat_dead.store(false);
    /* Python 不在 connect 重置 _last_pong_received（初值 0.0，僅
       pong 更新）——對端須於首個 interval 檢查前 pong，否則逾期；
       此處鏡像同語義，不代設。 */

    start_threads();
    enqueue_hello();
    set_state(GPTBRIDGE_A263_STATE_OPEN);
    return true;
}

void A263ChannelRuntime::disconnect(int code, const std::string& reason) {
    if (impl_->state.load() == GPTBRIDGE_A263_STATE_CLOSED &&
        !impl_->threads_running.load())
        return;
    impl_->heartbeat_dead.store(true);
    impl_->wakeup.notify_all();
    join_threads();
    impl_->transport_close(code, reason);
    set_state(GPTBRIDGE_A263_STATE_CLOSED);
}

bool A263ChannelRuntime::reconnect(int64_t snapshot_cursor,
                                   const std::string& snapshot_hash,
                                   const std::string& backend_generation,
                                   const std::string& session_id) {
    /* _verify_snapshot：cursor >= 0（Python 現行 stub 語義）。 */
    if (!gptbridge_a263_verify_snapshot(snapshot_cursor))
        return false;
    (void)snapshot_hash; /* Python 同：暫存快照欄位，hash 驗證留 stub */

    set_state(GPTBRIDGE_A263_STATE_RECONNECTING);
    impl_->heartbeat_dead.store(false);
    impl_->reconnect_attempts += 1;

    if (gptbridge_a263_reconnect_verdict(
            impl_->reconnect_attempts,
            impl_->cfg.reconnect_max_attempts) ==
        GPTBRIDGE_A263_RECONNECT_DEAD) {
        set_state(GPTBRIDGE_A263_STATE_DEAD);
        return false;
    }

    /* 先收舊迴圈再退避＋新代。 */
    join_threads();
    impl_->transport_close(1001, "reconnect");
    impl_->heartbeat_dead.store(false);

    impl_->sleep_s(gptbridge_a263_reconnect_delay_seconds(
        impl_->cfg.reconnect_base_delay_seconds,
        impl_->reconnect_attempts));

    impl_->generation.store(
        gptbridge_a263_next_generation(impl_->generation.load()));
    impl_->backend_generation = backend_generation;
    impl_->session_id = session_id;
    impl_->gen_created_at = utc_now_iso();
    /* _last_pong_received 跨 reconnect 不重置（Python 同）。 */

    start_threads();
    enqueue_resync(static_cast<uint64_t>(snapshot_cursor));
    set_state(GPTBRIDGE_A263_STATE_OPEN);
    impl_->n_reconnects.fetch_add(1);
    return true;
}

std::string A263ChannelRuntime::state_name() const {
    return gptbridge_a263_state_name(
        static_cast<gptbridge_a263_state_t>(impl_->state.load()));
}
uint64_t A263ChannelRuntime::generation() const {
    return impl_->generation.load();
}
uint64_t A263ChannelRuntime::acked_cursor() const {
    return impl_->acked_cursor.load();
}
uint64_t A263ChannelRuntime::sent_upto() const {
    return impl_->sent_upto.load();
}
uint64_t A263ChannelRuntime::latest_sequence() const {
    return impl_->latest_seq.load();
}
int64_t A263ChannelRuntime::messages_sent() const {
    return impl_->messages_sent.load();
}
int64_t A263ChannelRuntime::messages_received() const {
    return impl_->messages_received.load();
}
int64_t A263ChannelRuntime::reconnects() const {
    return impl_->n_reconnects.load();
}
bool A263ChannelRuntime::heartbeat_dead() const {
    return impl_->heartbeat_dead.load();
}
bool A263ChannelRuntime::running() const {
    return impl_->threads_running.load();
}
bool A263ChannelRuntime::dual_track() const {
    return impl_->cfg.dual_track;
}

} // namespace chan
} // namespace gptbridge

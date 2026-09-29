/* channel_runtime_internal.h — A263ChannelRuntime 內部實作細節。
 * B94 拆分：供 channel_runtime.cpp 與 channel_runtime_loops.cpp 共用
 * （JSON 小工具、utc_now_iso、Impl 定義）。僅限 channel_runtime TUs
 * 包含；判定委派仍屬 a263_channel_core。
 */
#pragma once

#include "channel_runtime.h"

#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <ctime>
#include <thread>

extern "C" {
#include "a263_channel_core.h"
}

namespace gptbridge {
namespace chan {

namespace jl = jsonlite;

inline jl::JsonValue jstr(const std::string& s) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::String; v.string = s;
    return v;
}
inline jl::JsonValue jnum(double n) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Number; v.number = n;
    return v;
}
inline jl::JsonValue jobj(
    std::initializer_list<std::pair<std::string, jl::JsonValue>> items) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Object;
    v.object.assign(items.begin(), items.end());
    return v;
}

inline std::string utc_now_iso() {
    std::time_t t = std::time(nullptr);
    std::tm tm{};
    gmtime_s(&tm, &t);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02d+00:00",
                  tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
                  tm.tm_hour, tm.tm_min, tm.tm_sec);
    return buf;
}

struct A263ChannelRuntime::Impl {
    ChannelRuntimeConfig cfg;
    ChannelHooks hooks;

    std::mutex mu;
    std::condition_variable wakeup;
    std::deque<jl::JsonValue> control_queue;
    std::deque<std::pair<int32_t, jl::JsonValue>> message_queue;
    std::map<uint64_t, OutboxEvent> events;   /* TransactionalOutbox._events */

    std::atomic<int32_t> state{GPTBRIDGE_A263_STATE_CLOSED};
    std::atomic<uint64_t> generation{0};
    std::string backend_generation;
    std::string session_id;
    std::string gen_created_at;

    std::atomic<uint64_t> acked_cursor{0};
    std::atomic<uint64_t> sent_upto{0};
    std::atomic<uint64_t> latest_seq{0};

    std::atomic<bool> heartbeat_dead{false};
    std::atomic<double> last_pong{0.0};

    std::atomic<int64_t> messages_sent{0};
    std::atomic<int64_t> messages_received{0};
    std::atomic<int64_t> n_reconnects{0};
    uint32_t reconnect_attempts = 0; /* reconnect 內部持有（非並行熱點） */

    std::atomic<bool> threads_running{false};
    std::thread send_thread;
    std::thread recv_thread;
    std::thread hb_thread;

    double now() const {
        if (hooks.monotonic) return hooks.monotonic();
        return std::chrono::duration<double>(
                   std::chrono::steady_clock::now().time_since_epoch())
            .count();
    }
    void sleep_s(double seconds) const {
        if (hooks.sleep) { hooks.sleep(seconds); return; }
        if (seconds > 0)
            std::this_thread::sleep_for(
                std::chrono::duration<double>(seconds));
    }
    bool transport_send(const jl::JsonValue& msg) {
        try {
            return hooks.send ? hooks.send(msg) : false;
        } catch (...) {
            return false;
        }
    }
    void transport_close(int code, const std::string& reason) {
        try {
            if (hooks.close) hooks.close(code, reason);
        } catch (...) {
        }
    }
};


} // namespace chan
} // namespace gptbridge

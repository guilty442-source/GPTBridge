// Suite: M2 A263 channel 非同步執行面（channel_runtime.cpp）。
// Fake transport 注入＋注入時鐘/睡眠，驗證 send-loop 排序、outbox
// 持有＋resync 回放、ack/resync 游標、heartbeat deadline、reconnect
// 退避/上限、backpressure——判定全走 a263_channel_core（Python 權威）。
#include "harness.hpp"

#include <atomic>
#include <chrono>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "channel_runtime.h"

namespace jl = gptbridge::jsonlite;
namespace chan = gptbridge::chan;

namespace {
const char* SUITE = "CHANNEL_RUNTIME_SUITE";

jl::JsonValue jstr(const std::string& s) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::String; v.string = s;
    return v;
}
jl::JsonValue jnum(double n) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Number; v.number = n;
    return v;
}
jl::JsonValue jobj(
    std::initializer_list<std::pair<std::string, jl::JsonValue>> items) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Object;
    v.object.assign(items.begin(), items.end());
    return v;
}
std::string get_str(const jl::JsonValue& v, const char* key) {
    const jl::JsonValue* p = v.get(key);
    return (p && p->type == jl::JsonValue::Type::String) ? p->string : "";
}
double get_num(const jl::JsonValue& v, const char* key) {
    const jl::JsonValue* p = v.get(key);
    return (p && p->type == jl::JsonValue::Type::Number) ? p->number : 0.0;
}

struct FakeTransport {
    std::mutex mu;
    std::vector<jl::JsonValue> sent;
    std::vector<std::pair<int, std::string>> closed;
    std::atomic<int> sends{0};

    bool on_send(const jl::JsonValue& msg) {
        std::lock_guard<std::mutex> lk(mu);
        sent.push_back(msg);
        sends.fetch_add(1);
        return true;
    }
    void on_close(int code, const std::string& reason) {
        std::lock_guard<std::mutex> lk(mu);
        closed.emplace_back(code, reason);
    }
    bool wait_sent(size_t n, int ms) {
        const auto t0 = std::chrono::steady_clock::now();
        while (std::chrono::steady_clock::now() - t0 <
               std::chrono::milliseconds(ms)) {
            {
                std::lock_guard<std::mutex> lk(mu);
                if (sent.size() >= n) return true;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        return false;
    }
    /* 第 i 則訊息的 type/command 摘要（無 → ""/""）。 */
    std::pair<std::string, std::string> kind_at(size_t i) {
        std::lock_guard<std::mutex> lk(mu);
        if (i >= sent.size()) return {"", ""};
        return {get_str(sent[i], "type"), get_str(sent[i], "command")};
    }
    double seq_at(size_t i) {
        std::lock_guard<std::mutex> lk(mu);
        const jl::JsonValue* ev = sent[i].get("event");
        return ev ? get_num(*ev, "sequence") : -1;
    }
    size_t size() {
        std::lock_guard<std::mutex> lk(mu);
        return sent.size();
    }
};

chan::ChannelRuntimeConfig base_cfg() {
    chan::ChannelRuntimeConfig cfg;
    cfg.channel_id = "ai";
    cfg.send_idle_sleep_seconds = 0.01;
    cfg.heartbeat_interval_seconds = 3600.0; /* 測試期間不自行觸發 */
    cfg.heartbeat_timeout_seconds = 30.0;
    return cfg;
}

chan::ChannelHooks base_hooks(FakeTransport& t) {
    chan::ChannelHooks h;
    h.send = [&](const jl::JsonValue& m) { return t.on_send(m); };
    h.close = [&](int c, const std::string& r) { t.on_close(c, r); };
    return h;
}
} // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "connect_hello_open") {
        FakeTransport t;
        chan::A263ChannelRuntime ch;
        std::string err;
        NT_CHECK(ch.start(base_cfg(), base_hooks(t), &err), "start");
        NT_CHECK(ch.state_name() == "closed", "initially closed");
        NT_CHECK(ch.connect("bg1", "s1"), "connect");
        NT_CHECK(ch.state_name() == "open", "open after connect");
        NT_CHECK(ch.generation() == 1, "generation 1");
        NT_CHECK(t.wait_sent(1, 2000), "hello sent");
        auto k = t.kind_at(0);
        NT_CHECK(k.first == "control" && k.second == "state_event_hello",
                 "hello is control/state_event_hello");
        ch.disconnect();
        NT_CHECK(ch.state_name() == "closed", "closed after disconnect");
        NT_CHECK(!t.closed.empty() && t.closed.back().first == 1000,
                 "transport closed 1000");
    }
    NT_END_TEST(SUITE, "connect_hello_open");

    NT_TEST(SUITE, "send_ordering_control_outbox_batch") {
        /* _send_loop：control → outbox 依序 → message 批次依 priority
           穩定排序。佇列先備料再 connect，首輪一次排空。 */
        FakeTransport t;
        chan::A263ChannelRuntime ch;
        std::string err;
        NT_CHECK(ch.start(base_cfg(), base_hooks(t), &err), "start");
        NT_CHECK(ch.append_state_event("e1", "T", "op", jobj({})) == 1,
                 "event seq 1");
        NT_CHECK(ch.append_state_event("e2", "T", "op", jobj({})) == 2,
                 "event seq 2");
        NT_CHECK(ch.send_message(chan::MessagePriority::COMMAND,
                                 jobj({{"id", jstr("m1")}})),
                 "m1 queued");
        NT_CHECK(ch.send_message(chan::MessagePriority::CONTROL,
                                 jobj({{"id", jstr("m2")}})),
                 "m2 queued");
        NT_CHECK(ch.send_message(chan::MessagePriority::COMMAND,
                                 jobj({{"id", jstr("m3")}})),
                 "m3 queued");
        NT_CHECK(ch.connect(), "connect");
        NT_CHECK(t.wait_sent(6, 3000), "all 6 sent"); /* hello+2ev+3msg */

        /* 位置斷言（hello 可能落在第二輪——只驗各類內部序）。 */
        size_t n = t.size();
        int pos_ev1 = -1, pos_ev2 = -1, pos_m1 = -1, pos_m2 = -1,
            pos_m3 = -1;
        bool hello_seen = false;
        for (size_t i = 0; i < n; ++i) {
            auto k = t.kind_at(i);
            if (k.second == "state_event_hello") hello_seen = true;
            else if (k.first == "state_event") {
                if (t.seq_at(i) == 1) pos_ev1 = (int)i;
                if (t.seq_at(i) == 2) pos_ev2 = (int)i;
            } else {
                const jl::JsonValue* idp;
                {
                    std::lock_guard<std::mutex> lk(t.mu);
                    idp = nullptr;
                    const jl::JsonValue* idv = t.sent[i].get("id");
                    if (idv) {
                        const std::string id = idv->string;
                        if (id == "m1") pos_m1 = (int)i;
                        if (id == "m2") pos_m2 = (int)i;
                        if (id == "m3") pos_m3 = (int)i;
                    }
                }
                (void)idp;
            }
        }
        NT_CHECK(hello_seen, "hello sent");
        NT_CHECK(pos_ev1 >= 0 && pos_ev2 > pos_ev1,
                 "outbox in sequence order");
        NT_CHECK(pos_m2 >= 0 && pos_m1 > pos_m2 && pos_m3 > pos_m1,
                 "batch sorted by priority (CONTROL first, FIFO ties)");
        NT_CHECK(ch.sent_upto() == 2, "sent_upto advanced");
        ch.disconnect();
    }
    NT_END_TEST(SUITE, "send_ordering_control_outbox_batch");

    NT_TEST(SUITE, "ack_monotonic_and_resync_replay") {
        /* state_event_ack 單調；state_event_resync → 雙游標重置 →
           send loop 對 sent_upto 重取回放（不丟未確認事件）。 */
        FakeTransport t;
        chan::A263ChannelRuntime ch;
        std::string err;
        NT_CHECK(ch.start(base_cfg(), base_hooks(t), &err), "start");
        ch.append_state_event("e1", "T", "op", jobj({}));
        ch.append_state_event("e2", "T", "op", jobj({}));
        ch.append_state_event("e3", "T", "op", jobj({}));
        NT_CHECK(ch.connect(), "connect");
        NT_CHECK(t.wait_sent(4, 3000), "hello + 3 events sent");
        NT_CHECK(ch.sent_upto() == 3, "sent_upto 3");

        ch.handle_incoming(jobj(
            {{"type", jstr("control")},
             {"command", jstr("state_event_ack")},
             {"payload", jobj({{"cursor", jnum(3)}})}}));
        NT_CHECK(ch.acked_cursor() == 3, "ack advances");
        ch.handle_incoming(jobj(
            {{"type", jstr("control")},
             {"command", jstr("state_event_ack")},
             {"payload", jobj({{"cursor", jnum(1)}})}}));
        NT_CHECK(ch.acked_cursor() == 3, "ack does not regress");

        /* resync 至 cursor=1 → 回放 seq 2,3（send loop 重送）。 */
        const size_t before = t.size();
        ch.handle_incoming(jobj(
            {{"type", jstr("control")},
             {"command", jstr("state_event_resync")},
             {"payload", jobj({{"cursor", jnum(1)}})}}));
        /* sent_upto 由 send loop 即刻重取推進——只能斷 acked==1
           （handle_incoming 唯一寫入者）與最終回放結果，中間值
           屬競態視窗。 */
        NT_CHECK(ch.acked_cursor() == 1, "resync converges acked");
        NT_CHECK(t.wait_sent(before + 2, 3000), "replayed 2 events");
        NT_CHECK(t.seq_at(before) == 2 && t.seq_at(before + 1) == 3,
                 "replay covers unconfirmed seq 2,3");
        NT_CHECK(ch.sent_upto() == 3, "sent_upto re-advanced");
        ch.disconnect();
    }
    NT_END_TEST(SUITE, "ack_monotonic_and_resync_replay");

    NT_TEST(SUITE, "heartbeat_deadline_pong_resets") {
        /* heartbeat deadline：注入時鐘。pong 更新 last_pong →
           deadline 自該點起算；逾期 → dead＋close(1001)。 */
        FakeTransport t;
        std::atomic<double> fake_now{0.0};
        chan::ChannelHooks h = base_hooks(t);
        h.monotonic = [&]() { return fake_now.load(); };
        chan::A263ChannelRuntime ch;
        std::string err;
        chan::ChannelRuntimeConfig cfg = base_cfg();
        cfg.heartbeat_timeout_seconds = 30.0;
        NT_CHECK(ch.start(cfg, std::move(h), &err), "start");
        NT_CHECK(ch.connect(), "connect");

        fake_now.store(10.0);
        ch.poll_heartbeat();
        NT_CHECK(!ch.heartbeat_dead(), "under deadline alive");

        /* pong at t=10 → deadline 重算自此。 */
        ch.handle_incoming(jobj(
            {{"type", jstr("control")},
             {"command", jstr("heartbeat_pong")},
             {"payload", jobj({})}}));
        fake_now.store(39.9);
        ch.poll_heartbeat();
        NT_CHECK(!ch.heartbeat_dead(), "pong refreshed deadline");
        fake_now.store(41.0); /* 41-10=31 > 30 */
        ch.poll_heartbeat();
        NT_CHECK(ch.heartbeat_dead(), "expired -> dead");
        NT_CHECK(!t.closed.empty() && t.closed.back().first == 1001 &&
                     t.closed.back().second == "heartbeat_timeout",
                 "close(1001, heartbeat_timeout)");
        ch.disconnect();
    }
    NT_END_TEST(SUITE, "heartbeat_deadline_pong_resets");

    NT_TEST(SUITE, "reconnect_backoff_and_dead") {
        /* reconnect：snapshot verify、attempts++ > max → DEAD；
           退避 base*2^(n-1)；成功 → gen+1＋resync＋OPEN。 */
        FakeTransport t;
        std::vector<double> sleeps;
        chan::ChannelHooks h = base_hooks(t);
        h.sleep = [&](double s) { sleeps.push_back(s); };
        chan::ChannelRuntimeConfig cfg = base_cfg();
        cfg.reconnect_max_attempts = 2;
        cfg.reconnect_base_delay_seconds = 1.0;
        chan::A263ChannelRuntime ch;
        std::string err;
        NT_CHECK(ch.start(cfg, std::move(h), &err), "start");
        NT_CHECK(ch.connect(), "connect");
        t.wait_sent(1, 2000);

        NT_CHECK(!ch.reconnect(-1, "h"), "negative cursor rejected");

        NT_CHECK(ch.reconnect(0, "h", "bg2", "s2"), "attempt 1 ok");
        NT_CHECK(ch.generation() == 2, "gen incremented");
        NT_CHECK(ch.state_name() == "open", "open again");
        NT_CHECK(ch.reconnects() == 1, "reconnects=1");

        NT_CHECK(ch.reconnect(0, "h"), "attempt 2 ok");
        NT_CHECK(ch.generation() == 3, "gen 3");
        NT_CHECK(sleeps.size() == 2 && sleeps[0] == 1.0 &&
                     sleeps[1] == 2.0,
                 "backoff 1s,2s");

        NT_CHECK(!ch.reconnect(0, "h"), "attempt 3 exceeds max");
        NT_CHECK(ch.state_name() == "dead", "DEAD after max attempts");
        NT_CHECK(t.closed.back().first == 1001, "closed 1001 reconnect");
        ch.disconnect();
    }
    NT_END_TEST(SUITE, "reconnect_backoff_and_dead");

    NT_TEST(SUITE, "parity_pipeline_matrix") {
        /* M2→M3 parity 放行判據之端到端情境：把固定劇本（3 事件＋
           3 訊息＋hello → ack 亂序 → resync 回放）跑過執行面，輸出
           a263_runtime_matrix.json；Python 端
           test_native_m2_a263_parity.py 以真實 A263Channel 重播同
           劇本逐鍵比對（Python 為權威）。 */
        FakeTransport t;
        chan::A263ChannelRuntime ch;
        std::string err;
        NT_CHECK(ch.start(base_cfg(), base_hooks(t), &err), "start");
        ch.append_state_event("e1", "T", "op", jobj({{"i", jnum(1)}}));
        ch.append_state_event("e2", "T", "op", jobj({{"i", jnum(2)}}));
        ch.append_state_event("e3", "T", "op", jobj({{"i", jnum(3)}}));
        ch.send_message(chan::MessagePriority::COMMAND,
                        jobj({{"id", jstr("m1")}}));
        ch.send_message(chan::MessagePriority::CONTROL,
                        jobj({{"id", jstr("m2")}}));
        ch.send_message(chan::MessagePriority::COMMAND,
                        jobj({{"id", jstr("m3")}}));
        NT_CHECK(ch.connect("bg", "s"), "connect");
        NT_CHECK(t.wait_sent(7, 3000), "hello+3ev+3msg");

        /* ack 亂序：2 → 5 → 1 → cursor 2,5,5。 */
        std::vector<uint64_t> ack_traj;
        const int acks[] = {2, 5, 1};
        for (int a : acks) {
            ch.handle_incoming(jobj(
                {{"type", jstr("control")},
                 {"command", jstr("state_event_ack")},
                 {"payload", jobj({{"cursor", jnum(a)}})}}));
            ack_traj.push_back(ch.acked_cursor());
        }
        NT_CHECK(ack_traj[0] == 2 && ack_traj[1] == 5 &&
                     ack_traj[2] == 5, "ack trajectory");

        /* resync cursor=1 → 回放 seq 2,3。 */
        const size_t before = t.size();
        ch.handle_incoming(jobj(
            {{"type", jstr("control")},
             {"command", jstr("state_event_resync")},
             {"payload", jobj({{"cursor", jnum(1)}})}}));
        NT_CHECK(t.wait_sent(before + 2, 3000), "replay sent");
        NT_CHECK(t.seq_at(before) == 2 && t.seq_at(before + 1) == 3,
                 "replayed unconfirmed");

        /* 匯出 matrix：分流為 control/event/message 序列——同層序
           是契約，跨層位置受 pass 邊界競態影響（Python 同）。 */
        std::string controls = "[", events = "[", msgs = "[";
        bool fc = true, fe = true, fm = true;
        for (size_t i = 0; i < t.size(); ++i) {
            auto k = t.kind_at(i);
            if (k.second == "state_event_hello" ||
                k.first == "control") {
                if (!fc) controls += ",";
                controls += "{\"command\":\"" + k.second + "\"}";
                fc = false;
            } else if (k.first == "state_event") {
                if (!fe) events += ",";
                char buf[96];
                std::snprintf(buf, sizeof(buf),
                              "{\"sequence\":%.0f}", t.seq_at(i));
                events += buf;
                fe = false;
            } else {
                std::string id;
                {
                    std::lock_guard<std::mutex> lk(t.mu);
                    const jl::JsonValue* idv = t.sent[i].get("id");
                    if (idv) id = idv->string;
                }
                if (!fm) msgs += ",";
                msgs += "{\"id\":\"" + id + "\"}";
                fm = false;
            }
        }
        controls += "]"; events += "]"; msgs += "]";
        FILE* m = std::fopen("a263_runtime_matrix.json", "w");
        NT_CHECK(m != nullptr, "matrix file");
        std::fprintf(m,
            "{\"schema\":\"a263-runtime-matrix/v1\",\"scenario\":"
            "\"channel_pipeline\",\"controls\":%s,\"state_events\":%s,"
            "\"messages\":%s,\"ack_input\":[2,5,1],\"ack_trajectory\":"
            "[%llu,%llu,%llu],\"resync_cursor\":1,\"final_acked\":%llu,"
            "\"final_sent\":%llu}",
            controls.c_str(), events.c_str(), msgs.c_str(),
            (unsigned long long)ack_traj[0],
            (unsigned long long)ack_traj[1],
            (unsigned long long)ack_traj[2],
            (unsigned long long)ch.acked_cursor(),
            (unsigned long long)ch.sent_upto());
        std::fclose(m);
        ch.disconnect();
    }
    NT_END_TEST(SUITE, "parity_pipeline_matrix");

    NT_TEST(SUITE, "backpressure_and_drop_oldest") {
        /* backpressure enabled：queue_len>=cap → 拒收；停用時
           deque(maxlen) 語義：恆收但丟最舊。control 恆容量檢查。 */
        FakeTransport t;
        chan::ChannelRuntimeConfig cfg = base_cfg();
        cfg.max_queue_size = 2;
        cfg.control_channel_capacity = 1;
        chan::A263ChannelRuntime ch;
        std::string err;
        NT_CHECK(ch.start(cfg, base_hooks(t), &err), "start");
        /* 未 connect → send loop 未啟，佇列直接堆積。 */
        NT_CHECK(ch.send_message(chan::MessagePriority::COMMAND,
                                 jobj({{"id", jstr("a")}})),
                 "q1");
        NT_CHECK(ch.send_message(chan::MessagePriority::COMMAND,
                                 jobj({{"id", jstr("b")}})),
                 "q2");
        NT_CHECK(!ch.send_message(chan::MessagePriority::COMMAND,
                                  jobj({{"id", jstr("c")}})),
                 "at capacity rejected");
        NT_CHECK(ch.enqueue_control(jobj({{"command", jstr("x")}})),
                 "control 1 ok");
        NT_CHECK(!ch.enqueue_control(jobj({{"command", jstr("y")}})),
                 "control cap enforced");

        /* 停用 backpressure → 恆收、丟最舊（deque maxlen 語義）。 */
        cfg.enable_backpressure = false;
        chan::A263ChannelRuntime ch2;
        NT_CHECK(ch2.start(cfg, base_hooks(t), &err), "start2");
        NT_CHECK(ch2.send_message(chan::MessagePriority::COMMAND,
                                  jobj({{"id", jstr("a")}})),
                 "d1");
        ch2.send_message(chan::MessagePriority::COMMAND,
                         jobj({{"id", jstr("b")}}));
        NT_CHECK(ch2.send_message(chan::MessagePriority::COMMAND,
                                  jobj({{"id", jstr("c")}})),
                 "overflow still accepted");
        NT_CHECK(ch2.connect(), "connect2");
        NT_CHECK(t.wait_sent(3, 3000), "hello + 2 surviving msgs");
        /* 最舊 a 被丟 → 送出 b,c。 */
        std::vector<std::string> ids;
        for (size_t i = 0; i < t.size(); ++i) {
            std::lock_guard<std::mutex> lk(t.mu);
            const jl::JsonValue* idv = t.sent[i].get("id");
            if (idv) ids.push_back(idv->string);
        }
        NT_CHECK(ids.size() == 2 && ids[0] == "b" && ids[1] == "c",
                 "oldest dropped (deque maxlen)");
        ch.disconnect();
        ch2.disconnect();
    }
    NT_END_TEST(SUITE, "backpressure_and_drop_oldest");

    return native_tests::report("channel_runtime_suite.json");
}

// Suite: M2 A263 channel core (channel_runtime/mixins deterministic semantics).
// Covers state names, generation increment, reconnect verdict/backoff,
// heartbeat deadline, ack/resync cursors, outbox fetch window + sequence
// keys, backpressure, stable priority drain order, snapshot verify.
// Tail tests run the M2→M3 gate parity scenarios (heartbeat deadline /
// outbox no-loss replay / cursor convergence) and emit
// a263_parity_matrix.json for the Python-side replay comparison
// (main-system/tests/test_native_m2_a263_parity.py).
#include "harness.hpp"

#include <cstring>
#include <string>
#include <vector>

extern "C" {
#include "a263_channel_core.h"
}

namespace {
const char* SUITE = "A263_CHANNEL_CORE_SUITE";

// Parity matrix accumulator: each scenario appends one JSON object; main()
// writes them to a263_parity_matrix.json next to the suite report so the
// Python replay test can diff C-observed verdicts key-by-key.
std::string g_matrix;

void matrix_add(const std::string& entry) {
    if (!g_matrix.empty()) g_matrix += ",";
    g_matrix += entry;
}

std::string seq_list_json(const std::vector<uint64_t>& seqs) {
    std::string out = "[";
    for (std::size_t i = 0; i < seqs.size(); ++i) {
        if (i) out += ",";
        out += std::to_string(seqs[i]);
    }
    return out + "]";
}
}

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "state_names") {
        NT_CHECK(std::strcmp(
                     gptbridge_a263_state_name(GPTBRIDGE_A263_STATE_CLOSED),
                     "closed") == 0, "closed");
        NT_CHECK(std::strcmp(
                     gptbridge_a263_state_name(GPTBRIDGE_A263_STATE_OPEN),
                     "open") == 0, "open");
        NT_CHECK(std::strcmp(gptbridge_a263_state_name(
                                 GPTBRIDGE_A263_STATE_RECONNECTING),
                             "reconnecting") == 0, "reconnecting");
        NT_CHECK(std::strcmp(
                     gptbridge_a263_state_name(GPTBRIDGE_A263_STATE_DEAD),
                     "dead") == 0, "dead");
        NT_CHECK(std::strcmp(
                     gptbridge_a263_state_name(
                         static_cast<gptbridge_a263_state_t>(99)),
                     "unknown") == 0, "unknown state");
    }
    NT_END_TEST(SUITE, "state_names");

    NT_TEST(SUITE, "generation_increment") {
        NT_CHECK(gptbridge_a263_next_generation(0) == 1, "first connect");
        NT_CHECK(gptbridge_a263_next_generation(41) == 42,
                 "reconnect increments");
    }
    NT_END_TEST(SUITE, "generation_increment");

    NT_TEST(SUITE, "reconnect_verdict_and_backoff") {
        NT_CHECK(gptbridge_a263_reconnect_verdict(1, 3)
                     == GPTBRIDGE_A263_RECONNECT_OK, "attempt 1 of 3 ok");
        NT_CHECK(gptbridge_a263_reconnect_verdict(3, 3)
                     == GPTBRIDGE_A263_RECONNECT_OK, "attempt 3 of 3 ok");
        NT_CHECK(gptbridge_a263_reconnect_verdict(4, 3)
                     == GPTBRIDGE_A263_RECONNECT_DEAD,
                 "attempt 4 of 3 -> DEAD");
        NT_CHECK(gptbridge_a263_reconnect_delay_seconds(1.0, 1) == 1.0,
                 "attempt 1 = base");
        NT_CHECK(gptbridge_a263_reconnect_delay_seconds(1.0, 2) == 2.0,
                 "attempt 2 = 2x");
        NT_CHECK(gptbridge_a263_reconnect_delay_seconds(1.0, 3) == 4.0,
                 "attempt 3 = 4x");
        NT_CHECK(gptbridge_a263_reconnect_delay_seconds(0.5, 4) == 4.0,
                 "base 0.5 attempt 4 = 4.0");
        NT_CHECK(gptbridge_a263_reconnect_delay_seconds(1.0, 0) == 1.0,
                 "attempt 0 fail-closed = base");
    }
    NT_END_TEST(SUITE, "reconnect_verdict_and_backoff");

    NT_TEST(SUITE, "heartbeat_deadline") {
        NT_CHECK(gptbridge_a263_heartbeat_expired(40.0, 5.0, 30.0) == 1,
                 "35s > 30s timeout -> dead");
        NT_CHECK(gptbridge_a263_heartbeat_expired(35.0, 5.0, 30.0) == 0,
                 "exactly at timeout stays alive (strict >)");
        NT_CHECK(gptbridge_a263_heartbeat_expired(20.0, 5.0, 30.0) == 0,
                 "within deadline alive");
        NT_CHECK(gptbridge_a263_heartbeat_expired(0.5, 0.0, 30.0) == 0,
                 "fresh connect no pong yet, under deadline");
    }
    NT_END_TEST(SUITE, "heartbeat_deadline");

    NT_TEST(SUITE, "ack_cursor_monotonic") {
        NT_CHECK(gptbridge_a263_ack_advance(5, 9) == 9, "larger advances");
        NT_CHECK(gptbridge_a263_ack_advance(9, 5) == 9,
                 "smaller does not regress");
        NT_CHECK(gptbridge_a263_ack_advance(9, 9) == 9, "equal holds");
    }
    NT_END_TEST(SUITE, "ack_cursor_monotonic");

    NT_TEST(SUITE, "resync_cursors") {
        uint64_t acked = 12;
        uint64_t sent = 30;
        gptbridge_a263_resync_cursors(7, &acked, &sent);
        NT_CHECK(acked == 7, "acked set to peer cursor");
        NT_CHECK(sent == 7, "sent_upto set to peer cursor");
        gptbridge_a263_resync_cursors(7, nullptr, nullptr);
    }
    NT_END_TEST(SUITE, "resync_cursors");

    NT_TEST(SUITE, "fetch_window") {
        uint64_t first = 0;
        uint64_t end = 0;
        NT_CHECK(gptbridge_a263_fetch_window(0, 10, 100, &first, &end) == 1,
                 "window non-empty");
        NT_CHECK(first == 1 && end == 11, "full range [1,11)");
        NT_CHECK(gptbridge_a263_fetch_window(5, 10, 3, &first, &end) == 1,
                 "limited window");
        NT_CHECK(first == 6 && end == 9, "limit caps at cursor+1+limit");
        NT_CHECK(gptbridge_a263_fetch_window(10, 10, 100, &first, &end) == 0,
                 "past latest -> empty");
        NT_CHECK(first == 11 && end == 11, "empty window boundaries");
        NT_CHECK(gptbridge_a263_fetch_window(0, 0, 10, &first, &end) == 0,
                 "empty outbox");
    }
    NT_END_TEST(SUITE, "fetch_window");

    NT_TEST(SUITE, "outbox_sequence_and_key") {
        char buf[64];
        NT_CHECK(gptbridge_a263_outbox_next_sequence(0) == 1,
                 "first event seq 1");
        NT_CHECK(gptbridge_a263_outbox_next_sequence(41) == 42, "seq+1");
        NT_CHECK(gptbridge_a263_sequence_key("ai", 7, buf, sizeof(buf)) == 1,
                 "key written");
        NT_CHECK(std::strcmp(buf, "ai:7") == 0,
                 "idempotency_key = channel:seq");
        NT_CHECK(gptbridge_a263_sequence_key(nullptr, 7, buf, sizeof(buf))
                     == 0, "null channel fail-closed");
        NT_CHECK(gptbridge_a263_sequence_key("ai", 7, buf, 4) == 0,
                 "short buffer fail-closed");
    }
    NT_END_TEST(SUITE, "outbox_sequence_and_key");

    NT_TEST(SUITE, "backpressure") {
        NT_CHECK(gptbridge_a263_enqueue_allowed(999, 1000, 1) == 1,
                 "under capacity allowed");
        NT_CHECK(gptbridge_a263_enqueue_allowed(1000, 1000, 1) == 0,
                 "at capacity rejected");
        NT_CHECK(gptbridge_a263_enqueue_allowed(2000, 1000, 0) == 1,
                 "backpressure disabled -> always allowed");
    }
    NT_END_TEST(SUITE, "backpressure");

    NT_TEST(SUITE, "drain_order_stable") {
        /* CONTROL=0 < STATE=1 < COMMAND=2；同值保持 FIFO。 */
        int32_t pri[5] = {2, 0, 1, 2, 0};
        int32_t order[5] = {0, 0, 0, 0, 0};
        NT_CHECK(gptbridge_a263_drain_order(pri, 5, order) == 1, "sorted");
        NT_CHECK(order[0] == 1 && order[1] == 4, "control first (FIFO)");
        NT_CHECK(order[2] == 2, "state next");
        NT_CHECK(order[3] == 0 && order[4] == 3,
                 "command last (FIFO preserved)");
        NT_CHECK(gptbridge_a263_drain_order(pri, 0, order) == 1,
                 "empty batch ok");
        NT_CHECK(gptbridge_a263_drain_order(nullptr, 1, order) == 0,
                 "null priorities fail-closed");
        NT_CHECK(gptbridge_a263_drain_order(pri, 5, nullptr) == 0,
                 "null order fail-closed");
    }
    NT_END_TEST(SUITE, "drain_order_stable");

    NT_TEST(SUITE, "snapshot_verify") {
        NT_CHECK(gptbridge_a263_verify_snapshot(0) == 1, "cursor 0 ok");
        NT_CHECK(gptbridge_a263_verify_snapshot(123) == 1, "positive ok");
        NT_CHECK(gptbridge_a263_verify_snapshot(-1) == 0,
                 "negative rejected");
    }
    NT_END_TEST(SUITE, "snapshot_verify");

    // ==== M2→M3 parity gate scenarios (matrix emitted for Python replay) ====

    NT_TEST(SUITE, "parity_heartbeat_deadline") {
        /* heartbeat_mixin._heartbeat_loop 判定：
           monotonic - last_pong > timeout（嚴格大於）。 */
        struct Row { double now; double last; double timeout; int expired; };
        const Row rows[] = {
            {40.0, 5.0, 30.0, 1},   /* 35 > 30 -> dead */
            {35.0, 5.0, 30.0, 0},   /* 恰等於逾時值仍存活 */
            {20.0, 5.0, 30.0, 0},
            {0.5, 0.0, 30.0, 0},    /* fresh connect, under deadline */
            {61.0, 30.0, 30.0, 1},
            {60.0, 30.0, 30.0, 0},
            {100.0, 0.0, 15.0, 1},
            {15.0, 0.0, 15.0, 0},
        };
        std::string entries;
        for (const Row& r : rows) {
            const int got =
                gptbridge_a263_heartbeat_expired(r.now, r.last, r.timeout);
            NT_CHECK(got == r.expired, "heartbeat verdict mismatch");
            char buf[160];
            std::snprintf(buf, sizeof(buf),
                          "{\"now\":%.1f,\"last_pong\":%.1f,\"timeout\":%.1f,"
                          "\"expired\":%d}",
                          r.now, r.last, r.timeout, got);
            if (!entries.empty()) entries += ",";
            entries += buf;
        }
        matrix_add(std::string("{\"scenario\":\"heartbeat_deadline\","
                               "\"rows\":[") + entries + "]}");
    }
    NT_END_TEST(SUITE, "parity_heartbeat_deadline");

    NT_TEST(SUITE, "parity_outbox_no_loss_replay") {
        /* TransactionalOutbox：append 12 事件 → limit=4 消費全取 →
           resync cursor=3 → replay 覆蓋全部未確認事件（seq>3）。 */
        const char* channel = "ai";
        const int total = 12;
        const uint32_t limit = 4;
        uint64_t latest = 0;
        std::vector<uint64_t> appended;
        std::vector<std::string> keys;
        for (int i = 0; i < total; ++i) {
            latest = gptbridge_a263_outbox_next_sequence(latest);
            appended.push_back(latest);
            char key[64];
            NT_CHECK(gptbridge_a263_sequence_key(channel, latest, key,
                                                 sizeof(key)) == 1,
                     "key write");
            keys.push_back(key);
        }
        NT_CHECK(latest == static_cast<uint64_t>(total), "12 appended");

        /* 消費迴圈：fetch 視窗 [cursor+1, min(cursor+1+limit, latest+1))，
           逐 seq ack_advance，直到視窗空。 */
        uint64_t cursor = 0;
        std::vector<uint64_t> delivered;
        for (;;) {
            uint64_t first = 0, end = 0;
            if (!gptbridge_a263_fetch_window(cursor, latest, limit,
                                           &first, &end))
                break;
            for (uint64_t seq = first; seq < end; ++seq) {
                delivered.push_back(seq);
                cursor = gptbridge_a263_ack_advance(cursor, seq);
            }
        }
        NT_CHECK(delivered.size() == static_cast<size_t>(total),
                 "all events delivered");
        for (uint64_t i = 0; i < static_cast<uint64_t>(total); ++i)
            NT_CHECK(delivered[i] == i + 1, "ordered delivery");

        /* resync peer=3 → 雙游標重置 → replay [4..12]（不丟未確認）。 */
        uint64_t acked = cursor, sent = cursor;
        gptbridge_a263_resync_cursors(3, &acked, &sent);
        NT_CHECK(acked == 3 && sent == 3, "resync resets both cursors");
        uint64_t first = 0, end = 0;
        NT_CHECK(gptbridge_a263_fetch_window(acked, latest, 1000,
                                             &first, &end) == 1,
                 "replay window non-empty");
        std::vector<uint64_t> replayed;
        for (uint64_t seq = first; seq < end; ++seq) replayed.push_back(seq);
        NT_CHECK(replayed.size() == static_cast<size_t>(total - 3),
                 "replay covers unconfirmed");
        for (uint64_t i = 0; i < replayed.size(); ++i)
            NT_CHECK(replayed[i] == 4 + i, "replay ordered from cursor");

        std::string keys_json = "[";
        for (std::size_t i = 0; i < keys.size(); ++i) {
            if (i) keys_json += ",";
            keys_json += "\"" + keys[i] + "\"";
        }
        keys_json += "]";
        matrix_add(std::string("{\"scenario\":\"outbox_no_loss_replay\","
                               "\"channel\":\"ai\",\"appended\":") +
                   seq_list_json(appended) +
                   ",\"keys\":" + keys_json +
                   ",\"delivered\":" + seq_list_json(delivered) +
                   ",\"resync_cursor\":3,\"replayed\":" +
                   seq_list_json(replayed) +
                   ",\"final_acked\":3}");
    }
    NT_END_TEST(SUITE, "parity_outbox_no_loss_replay");

    NT_TEST(SUITE, "parity_cursor_convergence") {
        /* _handle_control state_event_ack：cursor 僅在更大時前進；
           _handle_resync：雙游標同設為 peer cursor。 */
        const uint64_t acks[] = {5, 3, 9, 7, 9, 12};
        const uint64_t expect[] = {5, 5, 9, 9, 9, 12};
        uint64_t cursor = 0;
        std::vector<uint64_t> trajectory;
        for (size_t i = 0; i < sizeof(acks) / sizeof(acks[0]); ++i) {
            cursor = gptbridge_a263_ack_advance(cursor, acks[i]);
            NT_CHECK(cursor == expect[i], "ack trajectory mismatch");
            trajectory.push_back(cursor);
        }
        uint64_t acked = cursor, sent = 30;
        gptbridge_a263_resync_cursors(4, &acked, &sent);
        NT_CHECK(acked == 4 && sent == 4, "resync converges cursors");

        const uint64_t post_acks[] = {4, 6, 2};
        const uint64_t post_expect[] = {4, 6, 6};
        std::vector<uint64_t> post_trajectory;
        for (size_t i = 0; i < 3; ++i) {
            acked = gptbridge_a263_ack_advance(acked, post_acks[i]);
            NT_CHECK(acked == post_expect[i], "post-resync ack mismatch");
            post_trajectory.push_back(acked);
        }

        std::string acks_json = "[5,3,9,7,9,12]";
        matrix_add(std::string("{\"scenario\":\"cursor_convergence\","
                               "\"acks\":") + acks_json +
                   ",\"trajectory\":" + seq_list_json(trajectory) +
                   ",\"resync_peer\":4,\"post_resync\":{\"acked\":4,"
                   "\"sent\":4},\"post_acks\":[4,6,2],"
                   "\"post_trajectory\":" + seq_list_json(post_trajectory) +
                   "}");
    }
    NT_END_TEST(SUITE, "parity_cursor_convergence");

    /* 寫出 parity matrix 供 Python 端逐鍵重播比對（Python 為權威）。 */
    {
        FILE* m = std::fopen("a263_parity_matrix.json", "w");
        if (m) {
            std::fprintf(m, "{\"schema\":\"a263-parity-matrix/v1\","
                            "\"scenarios\":[%s]}", g_matrix.c_str());
            std::fclose(m);
        }
    }

    return native_tests::report("a263_channel_core_suite.json");
}

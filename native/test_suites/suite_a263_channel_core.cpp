// Suite: M2 A263 channel core (channel_runtime/mixins deterministic semantics).
// Covers state names, generation increment, reconnect verdict/backoff,
// heartbeat deadline, ack/resync cursors, outbox fetch window + sequence
// keys, backpressure, stable priority drain order, snapshot verify.
#include "harness.hpp"

#include <cstring>
#include <string>

extern "C" {
#include "a263_channel_core.h"
}

namespace {
const char* SUITE = "A263_CHANNEL_CORE_SUITE";
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

    return native_tests::report("a263_channel_core_suite.json");
}

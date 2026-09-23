// Suite: E3 activation-broker C prototype (model_service_activation
// decision-machine semantics): admission ladder, throttle/backoff,
// governed release, explicit-stop memory, state-write heartbeat.
#include "harness.hpp"

#include <cmath>

extern "C" {
#include "activation_broker.h"
}

namespace {
const char* SUITE = "ACTIVATION_BROKER_SUITE";

gptbridge_act_inputs_t inputs_pending() {
    gptbridge_act_inputs_t in{};
    in.pending = 1;
    in.maintenance_ready = 1;
    in.shutting_down = 0;
    in.admission_hold = 0;
    in.liveness_known = 1;
    in.owner_active = 0;
    in.regulation_active = 0;
    in.now_monotonic = 100.0;
    return in;
}
}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "init_clamps_like_python") {
        gptbridge_act_broker_t b;
        NT_CHECK(gptbridge_act_init(&b, -5.0, 0.0, 0.5) == 1, "init");
        NT_CHECK(b.cooldown_s == 0.0, "cooldown clamped >=0");
        NT_CHECK(b.min_backoff_s == 1.0, "min backoff clamped >=1");
        NT_CHECK(b.max_backoff_s == 1.0, "max >= min");
        NT_CHECK(b.backoff_s == 1.0, "backoff starts at min");
        NT_CHECK(gptbridge_act_init(nullptr, 0, 0, 0) == 0, "null rejected");
    }
    NT_END_TEST(SUITE, "init_clamps_like_python");

    NT_TEST(SUITE, "admission_ladder_order") {
        gptbridge_act_broker_t b;
        gptbridge_act_init(&b, 20.0, 15.0, 180.0);
        gptbridge_act_inputs_t in = inputs_pending();

        in.maintenance_ready = 0;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_MAINTENANCE_PENDING,
                 "maintenance first");
        in = inputs_pending();
        in.shutting_down = 1;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_SHUTTING_DOWN,
                 "shutdown second");
        in = inputs_pending();
        in.admission_hold = 1;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_RESOURCE_HOLD,
                 "governor hold third");
        in = inputs_pending();
        in.liveness_known = 0;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_LIVENESS_UNKNOWN,
                 "liveness unknown fourth");
        in = inputs_pending();
        in.owner_active = 1;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_OWNER_RUNNING,
                 "owner already running");
        NT_CHECK(b.backoff_s == b.min_backoff_s, "owner-running resets backoff");
        in = inputs_pending();
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_SHOULD_START,
                 "fresh pending + owner down -> delegate start");
        NT_CHECK(b.attempts == 1, "attempt counted at delegation");
    }
    NT_END_TEST(SUITE, "admission_ladder_order");

    NT_TEST(SUITE, "throttle_and_backoff_escalation") {
        gptbridge_act_broker_t b;
        gptbridge_act_init(&b, 20.0, 15.0, 60.0);
        gptbridge_act_inputs_t in = inputs_pending();

        // start succeeds -> cooldown throttle
        gptbridge_act_ensure(&b, &in);
        NT_CHECK(gptbridge_act_on_start_result(&b, 1, 100.0) == GPTBRIDGE_ACT_STARTED,
                 "started");
        NT_CHECK(b.broker_started_owner == 1, "broker owns owner");
        in.now_monotonic = 105.0;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_THROTTLED,
                 "inside cooldown -> throttled");
        in.now_monotonic = 121.0;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_SHOULD_START,
                 "past cooldown -> start");

        // failures -> exponential backoff capped at max
        NT_CHECK(gptbridge_act_on_start_result(&b, 0, 121.0) == GPTBRIDGE_ACT_START_FAILED,
                 "fail 1");
        NT_CHECK(std::fabs(b.next_attempt_at - (121.0 + 15.0)) < 1e-9,
                 "delay=min backoff");
        in.now_monotonic = 200.0;
        gptbridge_act_ensure(&b, &in);
        gptbridge_act_on_start_result(&b, 0, 200.0);
        NT_CHECK(std::fabs(b.backoff_s - 60.0) < 1e-9, "backoff doubled->max");
        in.now_monotonic = 300.0;
        gptbridge_act_ensure(&b, &in);
        gptbridge_act_on_start_result(&b, 0, 300.0);
        NT_CHECK(std::fabs(b.backoff_s - 60.0) < 1e-9, "backoff capped at max");
    }
    NT_END_TEST(SUITE, "throttle_and_backoff_escalation");

    NT_TEST(SUITE, "release_only_broker_owned_under_regulation") {
        gptbridge_act_broker_t b;
        gptbridge_act_init(&b, 20.0, 15.0, 180.0);
        gptbridge_act_inputs_t in{};
        in.pending = 0;
        in.liveness_known = 1;
        in.owner_active = 1;
        in.regulation_active = 0;
        in.now_monotonic = 50.0;

        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_IDLE,
                 "not broker-started -> idle");
        b.broker_started_owner = 1;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_IDLE,
                 "no regulation -> idle");
        in.regulation_active = 1;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_SHOULD_RELEASE,
                 "regulation + broker-owned -> delegate stop");
        NT_CHECK(gptbridge_act_on_release_result(&b, 1, 50.0) == GPTBRIDGE_ACT_RELEASED,
                 "released");
        NT_CHECK(b.broker_started_owner == 0, "ownership cleared");
        NT_CHECK(std::fabs(b.next_release_at - 70.0) < 1e-9, "release cooldown");

        // release failure keeps ownership + cooldown gate
        b.broker_started_owner = 1;
        in.now_monotonic = 60.0;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_RELEASE_COOLDOWN,
                 "within release cooldown");
        in.now_monotonic = 71.0;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_SHOULD_RELEASE,
                 "past release cooldown");
        NT_CHECK(gptbridge_act_on_release_result(&b, 0, 71.0) == GPTBRIDGE_ACT_RELEASE_FAILED,
                 "release failed");
        NT_CHECK(b.broker_started_owner == 1, "failed release keeps ownership");

        // owner already down clears ownership without a stop call
        in.owner_active = 0;
        in.now_monotonic = 200.0;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_IDLE,
                 "owner down -> idle");
        NT_CHECK(b.broker_started_owner == 0, "ownership cleared on dead owner");
        // liveness exception -> idle (Python except branch)
        b.broker_started_owner = 1;
        in.owner_active = 1;
        in.liveness_known = 0;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_IDLE,
                 "liveness exception -> idle");
    }
    NT_END_TEST(SUITE, "release_only_broker_owned_under_regulation");

    NT_TEST(SUITE, "explicit_stop_memory_and_cooldown") {
        gptbridge_act_broker_t b;
        gptbridge_act_init(&b, 20.0, 15.0, 180.0);
        b.broker_started_owner = 1;
        gptbridge_act_note_explicit_stop(&b, 500.0, 9999.0);
        NT_CHECK(std::fabs(b.explicit_stop_at - 9999.0) < 1e-9, "wall stamp");
        NT_CHECK(b.broker_started_owner == 0, "ownership cleared");
        NT_CHECK(std::fabs(b.next_attempt_at - 520.0) < 1e-9,
                 "next attempt cooled");

        gptbridge_act_inputs_t in = inputs_pending();
        in.now_monotonic = 505.0;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_THROTTLED,
                 "post-stop requests wait cooldown");
        in.now_monotonic = 521.0;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_SHOULD_START,
                 "new request after cooldown still activates");
    }
    NT_END_TEST(SUITE, "explicit_stop_memory_and_cooldown");

    NT_TEST(SUITE, "poll_interval_and_write_due") {
        NT_CHECK(std::fabs(gptbridge_act_poll_interval(1, 5.0, 1.0) - 1.0) < 1e-9,
                 "pending -> fast poll");
        NT_CHECK(std::fabs(gptbridge_act_poll_interval(0, 5.0, 1.0) - 5.0) < 1e-9,
                 "idle -> slow poll");
        NT_CHECK(gptbridge_act_state_write_due(1, 10.0, 0.0, 60.0) == 1,
                 "changed fingerprint writes");
        NT_CHECK(gptbridge_act_state_write_due(0, 30.0, 0.0, 60.0) == 0,
                 "unchanged within heartbeat skipped");
        NT_CHECK(gptbridge_act_state_write_due(0, 61.0, 0.0, 60.0) == 1,
                 "heartbeat boundary writes");
    }
    NT_END_TEST(SUITE, "poll_interval_and_write_due");

    NT_TEST(SUITE, "restore_replays_authoritative_state") {
        gptbridge_act_broker_t b;
        gptbridge_act_init(&b, 20.0, 15.0, 180.0);
        /* fail-closed: null + invalid backoff rejected */
        NT_CHECK(gptbridge_act_restore(nullptr, 0, 0, 15.0, 0, 0, 0) == 0,
                 "null broker rejected");
        NT_CHECK(gptbridge_act_restore(&b, 0, 0, 0.0, 0, 0, 0) == 0,
                 "non-positive backoff rejected");
        /* replay: fields written verbatim, backoff clamped to bounds */
        NT_CHECK(gptbridge_act_restore(&b, 500.0, 700.0, 45.0, 3, 1, 9999.0)
                     == 1,
                 "restore ok");
        NT_CHECK(std::fabs(b.next_attempt_at - 500.0) < 1e-9, "attempt_at");
        NT_CHECK(std::fabs(b.next_release_at - 700.0) < 1e-9, "release_at");
        NT_CHECK(std::fabs(b.backoff_s - 45.0) < 1e-9, "backoff");
        NT_CHECK(b.attempts == 3, "attempts");
        NT_CHECK(b.broker_started_owner == 1, "owner flag replayed");
        NT_CHECK(std::fabs(b.explicit_stop_at - 9999.0) < 1e-9, "stop_at");
        /* clamp: below min -> min, above max -> max */
        gptbridge_act_restore(&b, 0, 0, 1.0, 0, 0, 0);
        NT_CHECK(std::fabs(b.backoff_s - 15.0) < 1e-9, "backoff clamps min");
        gptbridge_act_restore(&b, 0, 0, 9999.0, 0, 0, 0);
        NT_CHECK(std::fabs(b.backoff_s - 180.0) < 1e-9, "backoff clamps max");
        /* replayed flag drives the release ladder like a live start */
        gptbridge_act_restore(&b, 0, 0, 15.0, 0, 1, 0);
        gptbridge_act_inputs_t in{};
        in.liveness_known = 1;
        in.owner_active = 1;
        in.regulation_active = 1;
        in.now_monotonic = 1000.0;
        NT_CHECK(gptbridge_act_ensure(&b, &in) == GPTBRIDGE_ACT_SHOULD_RELEASE,
                 "restored owner flag -> should-release");
    }
    NT_END_TEST(SUITE, "restore_replays_authoritative_state");

    return native_tests::report("activation_broker_suite.json");
}

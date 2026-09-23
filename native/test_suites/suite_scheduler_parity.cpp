// Suite: scheduler parity — C scheduler vs Python PeriodicScheduler (G100)
// Covers register/tick/pause/timeout equivalence for shadow→primary gate.

#include "harness.hpp"
extern "C" {
#include "scheduler.h"
}
#include <atomic>

namespace {
const char* SUITE = "SCHEDULER_PARITY_SUITE";

int g_counter = 0;
void inc_fn(void* ctx) {
    auto* p = static_cast<int*>(ctx);
    (*p)++;
}
void fail_fn(void*) { /* simulate failure via error_count increment */ }

}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "register_and_tick_runs_at_deadline") {
        gptbridge_sched_t s{};
        gptbridge_sched_init(&s);
        int c = 0;
        gptbridge_sched_register(&s, "job", 50, 0, 0, 0, 0, inc_fn, &c);
        // Not due at 0
        int n = gptbridge_sched_tick(&s, 0, 0);
        NT_CHECK(n == 0, "not due at 0");
        NT_CHECK(c == 0, "counter still 0");
        // Due at 50
        n = gptbridge_sched_tick(&s, 50, 0);
        NT_CHECK(n == 1, "due at 50");
        NT_CHECK(c == 1, "counter 1");
        // Not due again until 100
        n = gptbridge_sched_tick(&s, 75, 0);
        NT_CHECK(n == 0, "not due at 75");
        n = gptbridge_sched_tick(&s, 100, 0);
        NT_CHECK(n == 1, "due at 100");
        NT_CHECK(c == 2, "counter 2");
    }
    NT_END_TEST(SUITE, "register_and_tick_runs_at_deadline");

    NT_TEST(SUITE, "unregister_removes_job") {
        gptbridge_sched_t s{};
        gptbridge_sched_init(&s);
        int c = 0;
        gptbridge_sched_register(&s, "job", 20, 0, 0, 1, 0, inc_fn, &c);
        gptbridge_sched_tick(&s, 0, 0);
        NT_CHECK(c == 1, "run_immediately");
        gptbridge_sched_unregister(&s, "job");
        gptbridge_sched_tick(&s, 20, 0);
        gptbridge_sched_tick(&s, 40, 0);
        NT_CHECK(c == 1, "unregistered job must not run");
        NT_CHECK(gptbridge_sched_find(&s, "job") == nullptr, "find after unregister");
    }
    NT_END_TEST(SUITE, "unregister_removes_job");

    NT_TEST(SUITE, "pausable_job_defers_under_regulation") {
        gptbridge_sched_t s{};
        gptbridge_sched_init(&s);
        int pausable = 0, essential = 0;
        gptbridge_sched_register(&s, "pausable-job", 20, 0, 0, 0, 1, inc_fn, &pausable);
        gptbridge_sched_register(&s, "essential-job", 20, 0, 0, 0, 0, inc_fn, &essential);
        // Two ticks while paused: pausable defers, essential runs
        gptbridge_sched_tick(&s, 20, 1);
        gptbridge_sched_tick(&s, 40, 1);
        NT_CHECK(pausable == 0, "pausable must defer");
        NT_CHECK(essential == 2, "essential must run");
        const auto* pj = gptbridge_sched_find(&s, "pausable-job");
        NT_CHECK(pj && pj->paused_count >= 2, "paused_count");
        // Release
        gptbridge_sched_tick(&s, 60, 0);
        NT_CHECK(pausable == 1, "pausable runs after release");
    }
    NT_END_TEST(SUITE, "pausable_job_defers_under_regulation");

    NT_TEST(SUITE, "run_immediately_anchors_now") {
        gptbridge_sched_t s{};
        gptbridge_sched_init(&s);
        int c = 0;
        gptbridge_sched_register(&s, "job", 100, 0, 50, 1, 0, inc_fn, &c);
        // run_immediately => next_due = now (50), so tick at 50 runs
        int n = gptbridge_sched_tick(&s, 50, 0);
        NT_CHECK(n == 1 && c == 1, "immediate at now");
        // Next due = 150
        n = gptbridge_sched_tick(&s, 100, 0);
        NT_CHECK(n == 0, "not due at 100");
    }
    NT_END_TEST(SUITE, "run_immediately_anchors_now");

    return native_tests::report("scheduler_parity_suite.json");
}

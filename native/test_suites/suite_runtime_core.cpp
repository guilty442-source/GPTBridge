// Suite: E1/E2 runtime-core C prototypes (pure C11 sources, linked for real).
// Covers runtime_core (queue/lifecycle/deadline/loop), scheduler (E1 periodic
// scheduler shadow), ipc_registry (E2 request registry shadow), watchdog
// (E1 connection watchdog), outbox (E1 A195 cursors), maintenance (E1 tick).
#include "harness.hpp"

#include <cstring>

extern "C" {
#include "runtime_core.h"
#include "scheduler.h"
#include "ipc_registry.h"
#include "watchdog.h"
#include "outbox.h"
#include "maintenance.h"
}

namespace {

const char* SUITE = "RUNTIME_CORE_SUITE";

int g_ran = 0;
void counting_task(gptbridge_rc_task_t*, void*) { ++g_ran; }
void counting_job(void*) { ++g_ran; }

}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "queue_priority_order_and_backpressure") {
        gptbridge_rc_queue_t q;
        NT_CHECK(gptbridge_rc_queue_init(&q, 4) == 1, "init");
        gptbridge_rc_task_t t{};
        t.id = 1; t.priority = 5; NT_CHECK(gptbridge_rc_queue_push(&q, &t) == 1, "push1");
        t.id = 2; t.priority = 9; NT_CHECK(gptbridge_rc_queue_push(&q, &t) == 1, "push2");
        t.id = 3; t.priority = 1; NT_CHECK(gptbridge_rc_queue_push(&q, &t) == 1, "push3");
        t.id = 4; t.priority = 7; NT_CHECK(gptbridge_rc_queue_push(&q, &t) == 1, "push4");
        t.id = 5; NT_CHECK(gptbridge_rc_queue_push(&q, &t) == 0, "backpressure on full");
        gptbridge_rc_task_t out;
        NT_CHECK(gptbridge_rc_queue_pop(&q, &out) == 1 && out.id == 2, "highest priority first");
        NT_CHECK(gptbridge_rc_queue_pop(&q, &out) == 1 && out.id == 4, "second");
        NT_CHECK(gptbridge_rc_queue_pop(&q, &out) == 1 && out.id == 1, "third");
        NT_CHECK(gptbridge_rc_queue_depth(&q) == 1, "depth 1");
    }
    NT_END_TEST(SUITE, "queue_priority_order_and_backpressure");

    NT_TEST(SUITE, "lifecycle_legal_and_illegal_transitions") {
        gptbridge_rc_lifecycle_sm_t sm;
        NT_CHECK(gptbridge_rc_lifecycle_init(&sm) == 1, "init");
        NT_CHECK(gptbridge_rc_lifecycle_state(&sm) == GPTBRIDGE_RC_CREATED, "created");
        NT_CHECK(gptbridge_rc_lifecycle_transition(&sm, GPTBRIDGE_RC_RUNNING) == 0,
                 "created->running illegal");
        NT_CHECK(gptbridge_rc_lifecycle_transition(&sm, GPTBRIDGE_RC_READY) == 1, "->ready");
        NT_CHECK(gptbridge_rc_lifecycle_transition(&sm, GPTBRIDGE_RC_RUNNING) == 1, "->running");
        NT_CHECK(gptbridge_rc_lifecycle_transition(&sm, GPTBRIDGE_RC_TERMINATED) == 1, "->terminated");
        NT_CHECK(gptbridge_rc_lifecycle_transition(&sm, GPTBRIDGE_RC_READY) == 0,
                 "terminated is terminal");
    }
    NT_END_TEST(SUITE, "lifecycle_legal_and_illegal_transitions");

    NT_TEST(SUITE, "deadline_and_cancellation_fail_closed") {
        gptbridge_rc_task_t t{};
        t.id = 7; t.deadline_ms = 1000;
        NT_CHECK(gptbridge_rc_task_is_expired(&t, 999) == 0, "not expired");
        NT_CHECK(gptbridge_rc_task_is_expired(&t, 1000) == 1, "expired at deadline");
        NT_CHECK(gptbridge_rc_task_cancel(&t) == 1 &&
                 t.state == GPTBRIDGE_RC_TASK_CANCELLED, "cancel");
    }
    NT_END_TEST(SUITE, "deadline_and_cancellation_fail_closed");

    NT_TEST(SUITE, "event_loop_executes_ready_and_skips_expired") {
        g_ran = 0;
        gptbridge_rc_event_loop_t* loop = gptbridge_rc_loop_create(8);
        NT_CHECK(loop != nullptr, "loop create");
        gptbridge_rc_task_t a{}; a.id = 1; a.priority = 1;
        gptbridge_rc_task_t b{}; b.id = 2; b.priority = 5; b.deadline_ms = 100;
        NT_CHECK(gptbridge_rc_loop_submit(loop, &a) == 1, "submit a");
        NT_CHECK(gptbridge_rc_loop_submit(loop, &b) == 1, "submit b");
        NT_CHECK(gptbridge_rc_loop_tick(loop, counting_task, nullptr, 500) == 1,
                 "tick executes");
        NT_CHECK(g_ran == 0, "expired task not executed");
        NT_CHECK(gptbridge_rc_loop_tick(loop, counting_task, nullptr, 500) == 1 &&
                 g_ran == 1, "live task executed");
        NT_CHECK(gptbridge_rc_loop_tick(loop, counting_task, nullptr, 500) == 0,
                 "idle when empty");
        gptbridge_rc_loop_destroy(loop);
    }
    NT_END_TEST(SUITE, "event_loop_executes_ready_and_skips_expired");

    NT_TEST(SUITE, "scheduler_due_jobs_and_isolation") {
        g_ran = 0;
        gptbridge_sched_t s;
        NT_CHECK(gptbridge_sched_init(&s) == 1, "init");
        NT_CHECK(gptbridge_sched_register(&s, "a", 100, 50, counting_job, nullptr) == 1,
                 "register a");
        NT_CHECK(gptbridge_sched_register(&s, "b", 200, 50, counting_job, nullptr) == 1,
                 "register b");
        NT_CHECK(gptbridge_sched_tick(&s, 50) == 0, "nothing due at 50");
        NT_CHECK(gptbridge_sched_tick(&s, 150) == 1 && g_ran == 1, "only a due at 150");
        NT_CHECK(gptbridge_sched_tick(&s, 250) >= 1 && g_ran >= 2, "b due by 250");
        NT_CHECK(gptbridge_sched_find(&s, "a") != nullptr, "find a");
        NT_CHECK(gptbridge_sched_find(&s, "nope") == nullptr, "find missing");
    }
    NT_END_TEST(SUITE, "scheduler_due_jobs_and_isolation");

    NT_TEST(SUITE, "ipc_registry_request_lifecycle") {
        gptbridge_ipc_registry_t r;
        NT_CHECK(gptbridge_ipc_registry_init(&r) == 1, "init");
        NT_CHECK(gptbridge_ipc_registry_create(&r, "req-1", 41) == 1, "create");
        NT_CHECK(gptbridge_ipc_registry_set_status(&r, "req-1", GPTBRIDGE_REQ_QUEUED) == 1,
                 "queued");
        NT_CHECK(gptbridge_ipc_registry_set_status(&r, "req-1", GPTBRIDGE_REQ_RUNNING) == 1,
                 "running");
        NT_CHECK(gptbridge_ipc_registry_set_status(&r, "req-1", GPTBRIDGE_REQ_COMPLETED) == 1,
                 "completed");
        const gptbridge_ipc_request_t* found =
            gptbridge_ipc_registry_find(&r, "req-1");
        NT_CHECK(found != nullptr && found->status == GPTBRIDGE_REQ_COMPLETED &&
                 found->backend_generation == 41, "recorded");
        NT_CHECK(gptbridge_ipc_registry_cancel(&r, "req-2") == 0, "cancel unknown");
    }
    NT_END_TEST(SUITE, "ipc_registry_request_lifecycle");

    NT_TEST(SUITE, "watchdog_state_machine_and_repair_trigger") {
        gptbridge_wd_t wd;
        NT_CHECK(gptbridge_wd_init(&wd, 15000, 60000, 2, 1) == 1, "init");
        gptbridge_wd_probe_t ok{1, 1, 1};
        NT_CHECK(gptbridge_wd_compute_state(&ok) == GPTBRIDGE_WD_CONNECTED,
                 "all up = connected");
        gptbridge_wd_probe(&wd, &ok, 0, nullptr);
        NT_CHECK(wd.state == GPTBRIDGE_WD_CONNECTED, "probe connected");
        gptbridge_wd_probe_t dead{0, 0, 0};
        NT_CHECK(gptbridge_wd_compute_state(&dead) == GPTBRIDGE_WD_DISCONNECTED,
                 "process dead = disconnected");
        NT_CHECK(gptbridge_wd_probe(&wd, &dead, 15000, nullptr) == 0,
                 "first dead probe absorbed by grace");
        NT_CHECK(gptbridge_wd_probe(&wd, &dead, 30000, nullptr) == 0,
                 "dead=1 below threshold");
        NT_CHECK(gptbridge_wd_probe(&wd, &dead, 45000, nullptr) == 1,
                 "dead reaches threshold triggers repair once");
        NT_CHECK(gptbridge_wd_probe(&wd, &dead, 60000, nullptr) == 0,
                 "no repeat trigger");
        gptbridge_wd_probe(&wd, &ok, 75000, nullptr);
        NT_CHECK(wd.repair_triggered == 0, "repair flag reset on connected");
    }
    NT_END_TEST(SUITE, "watchdog_state_machine_and_repair_trigger");

    NT_TEST(SUITE, "watchdog_adaptive_interval_backoff") {
        gptbridge_wd_t wd;
        gptbridge_wd_init(&wd, 15000, 60000, 2, 1);
        gptbridge_wd_probe_t ok{1, 1, 1};
        for (int i = 0; i < 4; ++i) gptbridge_wd_probe(&wd, &ok, i * 15000, nullptr);
        NT_CHECK(gptbridge_wd_next_interval_ms(&wd) > 15000,
                 "stable probes grow interval");
        gptbridge_wd_probe_t deg{1, 1, 0};
        gptbridge_wd_probe(&wd, &deg, 90000, nullptr);
        NT_CHECK(gptbridge_wd_next_interval_ms(&wd) == 15000,
                 "non-connected resets interval");
    }
    NT_END_TEST(SUITE, "watchdog_adaptive_interval_backoff");

    NT_TEST(SUITE, "outbox_cursors_window_and_retry") {
        gptbridge_ob_registry_t r;
        NT_CHECK(gptbridge_ob_init(&r) == 1, "init");
        NT_CHECK(gptbridge_ob_register(&r, "s1") == 1, "register");
        int32_t reset = 0;
        NT_CHECK(gptbridge_ob_hello(&r, "s1", 10, 1, 100, &reset) == 10 && reset == 0,
                 "hello same generation keeps cursor");
        NT_CHECK(gptbridge_ob_hello(&r, "s1", 10, 0, 100, &reset) == 100 && reset == 1,
                 "generation mismatch resets to latest");
        NT_CHECK(gptbridge_ob_ack(&r, "s1", 90) == 0, "ack never moves backwards");
        NT_CHECK(gptbridge_ob_ack(&r, "s1", 105) == 1, "ack forward");
        int64_t start_after = 0, limit = 0;
        NT_CHECK(gptbridge_ob_drain_plan(&r, "s1", 0, GPTBRIDGE_OB_RETRY_MS,
                                         &start_after, &limit) == 1 &&
                 start_after == 105, "drain starts after sent_upto");
        NT_CHECK(gptbridge_ob_mark_sent(&r, "s1", 200, 1000) == 1, "mark sent");
        int64_t deadline = 0;
        NT_CHECK(gptbridge_ob_next_retry_deadline(&r, GPTBRIDGE_OB_RETRY_MS,
                                                &deadline) == 1 &&
                 deadline == 1000 + GPTBRIDGE_OB_RETRY_MS,
                 "retry deadline = last_attempt + retry");
        NT_CHECK(gptbridge_ob_prune_floor(&r, 500) == 105, "prune floor = min acked");
    }
    NT_END_TEST(SUITE, "outbox_cursors_window_and_retry");

    NT_TEST(SUITE, "maintenance_admission_retry_and_ttl_cache") {
        gptbridge_mt_t mt;
        NT_CHECK(gptbridge_mt_init(&mt, 30000, 3600000, 3, 60000, 7) == 1, "init");
        gptbridge_mt_job_t j{};
        std::strncpy(j.job_id, "job-1", sizeof(j.job_id) - 1);
        std::strncpy(j.action_id, "pg-vacuum", sizeof(j.action_id) - 1);
        j.risk_class = GPTBRIDGE_MT_M1;
        j.generation = 7;
        NT_CHECK(gptbridge_mt_admit(&mt, &j, 0, 0, 0) == 0,
                 "M1 rejected when not idle");
        NT_CHECK(gptbridge_mt_admit(&mt, &j, 1, 0, 0) == 1,
                 "M1 admitted when idle");
        gptbridge_mt_job_t j2 = j;
        std::strncpy(j2.job_id, "job-2", sizeof(j2.job_id) - 1);
        j2.risk_class = GPTBRIDGE_MT_M3;
        NT_CHECK(gptbridge_mt_admit(&mt, &j2, 1, 1, 0) == 0,
                 "M3 candidate never auto-runs");
        gptbridge_mt_job_t j3 = j;
        std::strncpy(j3.job_id, "job-3", sizeof(j3.job_id) - 1);
        j3.generation = 8;
        NT_CHECK(gptbridge_mt_admit(&mt, &j3, 1, 1, 0) == 0,
                 "generation mismatch rejected");
        gptbridge_mt_job_t* due = gptbridge_mt_next_due(&mt, 100);
        NT_CHECK(due != nullptr && due->status == GPTBRIDGE_MT_RUNNING,
                 "due job runs");
        NT_CHECK(gptbridge_mt_fail(&mt, "job-1", 200) == 1 &&
                 due->status == GPTBRIDGE_MT_DEFERRED,
                 "fail defers with backoff");
        NT_CHECK(due->next_attempt_ms == 200 + 60000, "backoff applied");
        gptbridge_mt_cache_t c{};
        int payload = 42;
        gptbridge_mt_cache_set(&c, 1000, 1, &payload);
        void* got = nullptr; int32_t ok = 0;
        NT_CHECK(gptbridge_mt_cache_get(&c, 1500, 20000, &got, &ok) == 1 &&
                 got == &payload && ok == 1, "TTL hit shares probe");
        NT_CHECK(gptbridge_mt_cache_get(&c, 30000, 20000, &got, &ok) == 0,
                 "TTL expiry misses");
    }
    NT_END_TEST(SUITE, "maintenance_admission_retry_and_ttl_cache");

    return native_tests::report("runtime_core_suite.json");
}

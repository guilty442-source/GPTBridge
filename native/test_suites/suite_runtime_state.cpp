// Suite: E3 runtime_state C prototype (star-runtime-state/v1 semantics).
// Covers dual-axis validation, RECOVERING counter, error-clear on
// READY/STARTING, error truncation, local-failure-isolation aggregate.
#include "harness.hpp"

#include <cstring>
#include <string>

extern "C" {
#include "runtime_state.h"
}

namespace {
const char* SUITE = "RUNTIME_STATE_SUITE";
}

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "state_name_validation_fail_closed") {
        NT_CHECK(gptbridge_rs_runtime_from_name("READY") == GPTBRIDGE_RT_READY,
                 "known state");
        NT_CHECK(gptbridge_rs_runtime_from_name("bogus") == GPTBRIDGE_RT_UNKNOWN,
                 "unknown -> 0");
        NT_CHECK(gptbridge_rs_runtime_from_name(nullptr) == GPTBRIDGE_RT_UNKNOWN,
                 "null -> 0");
        NT_CHECK(gptbridge_rs_capability_from_name("DISABLED") == GPTBRIDGE_CAP_DISABLED,
                 "known capability");
        NT_CHECK(gptbridge_rs_capability_from_name("ready") == GPTBRIDGE_CAP_UNKNOWN,
                 "case-sensitive unknown");

        gptbridge_rs_registry_t reg;
        gptbridge_rs_init(&reg);
        NT_CHECK(gptbridge_rs_set_runtime(&reg, "mod-a", GPTBRIDGE_RT_UNKNOWN,
                                          nullptr, nullptr, nullptr, "t0") == 0,
                 "UNKNOWN state rejected (ValueError parity)");
        NT_CHECK(reg.count == 0, "rejected set must not create record");
        NT_CHECK(gptbridge_rs_set_capability(&reg, "mod-a", 9, "t0") == 0,
                 "out-of-range capability rejected");
        NT_CHECK(reg.count == 0, "still no record");
    }
    NT_END_TEST(SUITE, "state_name_validation_fail_closed");

    NT_TEST(SUITE, "defaults_and_side_effects") {
        gptbridge_rs_registry_t reg;
        gptbridge_rs_init(&reg);
        // Python default record: STOPPED / AVAILABLE / health=unknown.
        NT_CHECK(gptbridge_rs_heartbeat(&reg, "mod-b", "t1", 1000) == 1,
                 "heartbeat creates");
        const gptbridge_rs_record_t* r = gptbridge_rs_find(&reg, "mod-b");
        NT_CHECK(r != nullptr, "find");
        NT_CHECK(r->runtime_state == GPTBRIDGE_RT_STOPPED, "default STOPPED");
        NT_CHECK(r->capability_state == GPTBRIDGE_CAP_AVAILABLE, "default AVAILABLE");
        NT_CHECK(std::strcmp(r->health, "unknown") == 0, "default health");
        NT_CHECK(std::strcmp(r->last_heartbeat, "t1") == 0, "heartbeat stamp");
        NT_CHECK(std::strcmp(r->updated_at, "t1") == 0, "updated_at==heartbeat");
        /* staleness dimension: beat at 1000ms is fresh inside 60s horizon,
           stale past it; never-beaten and unknown modules are stale /
           missing respectively. */
        NT_CHECK(r->last_heartbeat_ms == 1000, "heartbeat ms stamp");
        NT_CHECK(gptbridge_rs_is_stale(&reg, "mod-b", 1500, 60000) == 0,
                 "fresh inside horizon");
        NT_CHECK(gptbridge_rs_is_stale(&reg, "mod-b", 999999, 60000) == 1,
                 "stale past horizon");
        NT_CHECK(gptbridge_rs_is_stale(&reg, "mod-b", 61001, 60000) == 1,
                 "boundary > horizon is stale");
        NT_CHECK(gptbridge_rs_is_stale(&reg, "mod-b", 61000, 60000) == 0,
                 "boundary == horizon is fresh");
        NT_CHECK(gptbridge_rs_set_runtime(&reg, "mod-c", GPTBRIDGE_RT_STOPPED,
                                          nullptr, nullptr, nullptr, "t0") == 1,
                 "mod-c without heartbeat");
        NT_CHECK(gptbridge_rs_is_stale(&reg, "mod-c", 2000, 60000) == 1,
                 "never-beat is stale");
        NT_CHECK(gptbridge_rs_is_stale(&reg, "ghost", 2000, 60000) == -1,
                 "unknown module");
    }
    NT_END_TEST(SUITE, "defaults_and_side_effects");

    NT_TEST(SUITE, "recovering_counts_and_ready_clears_error") {
        gptbridge_rs_registry_t reg;
        gptbridge_rs_init(&reg);
        NT_CHECK(gptbridge_rs_set_runtime(&reg, "m", GPTBRIDGE_RT_FAILED,
                                          nullptr, nullptr, "boom", "t0") == 1,
                 "failed with error");
        NT_CHECK(gptbridge_rs_set_runtime(&reg, "m", GPTBRIDGE_RT_RECOVERING,
                                          nullptr, nullptr, nullptr, "t1") == 1,
                 "recovering 1");
        NT_CHECK(gptbridge_rs_set_runtime(&reg, "m", GPTBRIDGE_RT_RECOVERING,
                                          nullptr, nullptr, nullptr, "t2") == 1,
                 "recovering 2");
        const gptbridge_rs_record_t* r = gptbridge_rs_find(&reg, "m");
        NT_CHECK(r->recovery_attempts == 2, "RECOVERING increments");
        NT_CHECK(std::strlen(r->last_error) > 0, "error retained in RECOVERING");
        NT_CHECK(gptbridge_rs_set_runtime(&reg, "m", GPTBRIDGE_RT_READY,
                                          "ok", "rel-1", nullptr, "t3") == 1,
                 "ready");
        r = gptbridge_rs_find(&reg, "m");
        NT_CHECK(r->last_error[0] == '\0', "READY clears last_error");
        NT_CHECK(std::strcmp(r->health, "ok") == 0, "health updated");
        NT_CHECK(std::strcmp(r->release_id, "rel-1") == 0, "release updated");
        // error!=NULL overrides clear even on READY
        NT_CHECK(gptbridge_rs_set_runtime(&reg, "m", GPTBRIDGE_RT_READY,
                                          nullptr, nullptr, "warn", "t4") == 1,
                 "ready+error");
        r = gptbridge_rs_find(&reg, "m");
        NT_CHECK(std::strcmp(r->last_error, "warn") == 0, "explicit error kept");
    }
    NT_END_TEST(SUITE, "recovering_counts_and_ready_clears_error");

    NT_TEST(SUITE, "record_error_truncates_500") {
        gptbridge_rs_registry_t reg;
        gptbridge_rs_init(&reg);
        std::string big(600, 'x');
        NT_CHECK(gptbridge_rs_record_error(&reg, "m", big.c_str(), "t0") == 1,
                 "record_error");
        const gptbridge_rs_record_t* r = gptbridge_rs_find(&reg, "m");
        NT_CHECK(std::strlen(r->last_error) == 500, "truncated to 500");
    }
    NT_END_TEST(SUITE, "record_error_truncates_500");

    NT_TEST(SUITE, "aggregate_local_failure_isolation") {
        gptbridge_rs_registry_t reg;
        gptbridge_rs_init(&reg);
        gptbridge_rs_set_runtime(&reg, "zeta", GPTBRIDGE_RT_FAILED,
                                 nullptr, nullptr, nullptr, "t");
        gptbridge_rs_set_runtime(&reg, "alpha", GPTBRIDGE_RT_FAILED,
                                 nullptr, nullptr, nullptr, "t");
        gptbridge_rs_set_runtime(&reg, "beta", GPTBRIDGE_RT_READY,
                                 nullptr, nullptr, nullptr, "t");
        gptbridge_rs_set_capability(&reg, "beta", GPTBRIDGE_CAP_DISABLED, "t");
        int32_t by_rt[8], by_cap[5], failed_n = 0;
        char failed[4][GPTBRIDGE_RS_ID_MAX];
        int32_t total = gptbridge_rs_aggregate(&reg, by_rt, by_cap,
                                               failed, 4, &failed_n);
        NT_CHECK(total == 3, "module_count");
        NT_CHECK(by_rt[GPTBRIDGE_RT_FAILED] == 2, "2 failed");
        NT_CHECK(by_rt[GPTBRIDGE_RT_READY] == 1, "1 ready");
        NT_CHECK(by_cap[GPTBRIDGE_CAP_DISABLED] == 1, "1 disabled cap");
        NT_CHECK(failed_n == 2, "failed count");
        // Python sorted(failed_modules)
        NT_CHECK(std::strcmp(failed[0], "alpha") == 0 &&
                 std::strcmp(failed[1], "zeta") == 0, "failed sorted");
    }
    NT_END_TEST(SUITE, "aggregate_local_failure_isolation");

    NT_TEST(SUITE, "capacity_fail_closed") {
        gptbridge_rs_registry_t reg;
        gptbridge_rs_init(&reg);
        char id[16];
        for (int i = 0; i < GPTBRIDGE_RS_MAX_MODULES; ++i) {
            std::snprintf(id, sizeof(id), "m-%03d", i);
            NT_CHECK(gptbridge_rs_heartbeat(&reg, id, "t", 1000) == 1, "fill");
        }
        NT_CHECK(gptbridge_rs_heartbeat(&reg, "overflow", "t", 1000) == 0,
                 "full table rejects new module");
        NT_CHECK(reg.count == GPTBRIDGE_RS_MAX_MODULES, "count bounded");
    }
    NT_END_TEST(SUITE, "capacity_fail_closed");

    return native_tests::report("runtime_state_suite.json");
}

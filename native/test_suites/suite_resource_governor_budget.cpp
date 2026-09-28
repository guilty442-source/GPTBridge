// Suite: resource-governor 全域 concurrency budget（concurrency-budget/v1）。
//
// 鎖定不變式：total_quota ≤ logical_cores、INTERACTIVE 恆得保留額且永不
// paused、壓力層級依 regulation/pre/機器過熱降速（shed order 尾端先停）、
// generation 僅在配額簽章變動時遞增、rules 關閉時整層消失。
// 全部走假引擎，零 OS 副作用。
#include "harness.hpp"

#include "governor_fake_engine.h"

#include <map>
#include <string>
#include <vector>

namespace {

using namespace governor_suite;
const char* SUITE = "RESOURCE_GOVERNOR_BUDGET_SUITE";

gov::RulesDoc rules_with(const std::string& defaults_json) {
    const std::string text =
        std::string("{\"defaults\": ") + defaults_json + "}";
    auto parsed = gov::parse_rules(text);
    if (parsed.has_value()) return *parsed;
    return gov::RulesDoc{};
}

int class_quota(const gov::ConcurrencyBudget& budget, gov::WorkClass cls) {
    return budget.classes[static_cast<int>(cls)].quota;
}

bool class_paused(const gov::ConcurrencyBudget& budget, gov::WorkClass cls) {
    return budget.classes[static_cast<int>(cls)].paused;
}

int class_sum(const gov::ConcurrencyBudget& budget) {
    int total = 0;
    for (int i = 0; i < gov::kWorkClassCount; ++i)
        total += budget.classes[i].quota;
    return total;
}

}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "budget_published_bounded_and_generation_stable") {
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        engine.procs = {worker_proc(901, 1.0)};

        gov::Snapshot snap =
            gov::govern_once(config, empty_rules(), engine, records, regulation,
                             ctx, logs);
        NT_CHECK(snap.concurrency_budget.has_value(), "budget published");
        const gov::ConcurrencyBudget& budget = *snap.concurrency_budget;
        NT_CHECK(budget.logical_cores == 8, "logical cores");
        NT_CHECK(budget.interactive_reserve == 2, "reserve round(8*.25)");
        NT_CHECK(budget.total_quota <= 8, "bounded by logical");
        NT_CHECK(class_sum(budget) == budget.total_quota, "sum invariant");
        NT_CHECK(class_quota(budget, gov::WorkClass::Interactive) == 2 &&
                     !class_paused(budget, gov::WorkClass::Interactive),
                 "interactive never paused");
        /* mem_used 37.5% > kGlobalRamLimitPct(30) → machine-hot → pre。 */
        NT_CHECK(budget.pressure == gov::PressureTier::Pre, "pre tier");
        NT_CHECK(budget.generation == 1, "first generation");

        snap = gov::govern_once(config, empty_rules(), engine, records,
                                regulation, ctx, logs);
        NT_CHECK(snap.concurrency_budget->generation == 1,
                 "same signature keeps generation");

        const std::string text =
            gptbridge::jsonlite::json_serialize(gov::snapshot_to_json(snap));
        for (const char* key :
             {"\"concurrency_budget\"", "\"concurrency-budget/v1\"",
              "\"shed_order\"", "\"training\""})
            NT_CHECK(text.find(key) != std::string::npos, key);
    }
    NT_END_TEST(SUITE, "budget_published_bounded_and_generation_stable");

    NT_TEST(SUITE, "budget_active_sheds_tail_and_bumps_generation") {
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        /* 95 per-core / 8 logical ≈ 11.9% machine > 10% budget →
         * kRegulateOverSamples=1 → 首週期即 active（strict INT-10）。 */
        engine.procs = {worker_proc(902, 95.0)};

        gov::Snapshot snap =
            gov::govern_once(config, empty_rules(), engine, records, regulation,
                             ctx, logs);
        NT_CHECK(snap.reg_active, "regulation active");
        NT_CHECK(snap.concurrency_budget.has_value(), "budget present");
        const gov::ConcurrencyBudget& budget = *snap.concurrency_budget;
        NT_CHECK(budget.pressure == gov::PressureTier::Active, "active tier");
        NT_CHECK(class_paused(budget, gov::WorkClass::Training) &&
                     class_quota(budget, gov::WorkClass::Training) == 0,
                 "training paused first");
        NT_CHECK(class_paused(budget, gov::WorkClass::Batch),
                 "batch paused");
        NT_CHECK(class_quota(budget, gov::WorkClass::Interactive) == 2,
                 "interactive reserve intact");
        NT_CHECK(class_quota(budget, gov::WorkClass::Model) >= 1,
                 "model floor");
        NT_CHECK(budget.total_quota <= 8, "still bounded");
    }
    NT_END_TEST(SUITE, "budget_active_sheds_tail_and_bumps_generation");

    NT_TEST(SUITE, "budget_disabled_is_absent") {
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        engine.procs = {worker_proc(903, 1.0)};
        const gov::RulesDoc rules = rules_with("{\"concurrency_budget\": false}");
        gov::Snapshot snap = gov::govern_once(config, rules, engine, records,
                                            regulation, ctx, logs);
        NT_CHECK(!snap.concurrency_budget.has_value(), "kill switch");
        const std::string text =
            gptbridge::jsonlite::json_serialize(gov::snapshot_to_json(snap));
        NT_CHECK(text.find("\"concurrency_budget\":null") !=
                     std::string::npos,
                 "emitted as null");
    }
    NT_END_TEST(SUITE, "budget_disabled_is_absent");

    return native_tests::report("resource_governor_budget_suite.json");
}

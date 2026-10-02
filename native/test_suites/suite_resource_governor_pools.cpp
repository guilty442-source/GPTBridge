// Suite: resource-governor pool layer — Pool 歸因、pools 規則解析
// （fail-closed）、per-pool 共享 Job envelope、快照池帳本。
// 與 suite_resource_governor.cpp 同假引擎（governor_fake_engine.h）；
// 依 A185 拆分維持 ≤500 effective 行。
#include "harness.hpp"

#include "governor_fake_engine.h"

#include <string>
#include <vector>

namespace {

using namespace governor_suite;
const char* SUITE = "RESOURCE_GOVERNOR_POOLS_SUITE";

}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "pool_rules_parse_and_fail_closed") {
        const std::string text =
            R"({"pools": {"enabled": true,)"
            R"( "compute": {"cpu_limit_percent": 30.0, "memory_mb": 2048,)"
            R"( "process_limit": 8, "members": ["train", "inference"]},)"
            R"( "background": {"cpu_limit_percent": 5.0, "background": true,)"
            R"( "ecoqos": true, "members": ["dotnet"]}},)"
            R"( "programs": {"testhost.exe": {"pool": "background"}}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "pools parse");
        NT_CHECK(parsed->pools_enabled, "pools enabled");
        NT_CHECK(parsed->pools.count(gov::Pool::Compute) == 1, "compute pool");
        const gov::PoolPolicy& compute = parsed->pools.at(gov::Pool::Compute);
        NT_CHECK(compute.cpu_limit_percent == 30.0, "compute cpu cap");
        NT_CHECK(compute.memory_mb == 2048, "compute memory cap");
        NT_CHECK(compute.process_limit == 8, "compute proc limit");
        NT_CHECK(compute.members.size() == 2, "compute members");
        const gov::PoolPolicy& bg = parsed->pools.at(gov::Pool::Background);
        NT_CHECK(bg.background && bg.ecoqos, "background demotion flags");
        NT_CHECK(parsed->programs.at("testhost.exe").pool ==
                     gov::Pool::Background,
                 "program pool override");

        auto bad_name = gov::parse_rules(
            R"({"programs": {"x.exe": {"pool": "bogus"}}})");
        NT_CHECK(bad_name.has_value() && !bad_name->error.empty(),
                 "unknown pool name fails closed");

        auto bad_pool = gov::parse_rules(
            R"({"pools": {"mystery": {"cpu_limit_percent": 5}}})");
        NT_CHECK(bad_pool.has_value() && !bad_pool->error.empty(),
                 "unknown pool key fails closed");

        auto disabled = gov::parse_rules(
            R"({"pools": {"enabled": false, "compute": {"members": ["x"]}}})");
        NT_CHECK(disabled.has_value() && !disabled->pools_enabled,
                 "enabled:false closes the layer");
    }
    NT_END_TEST(SUITE, "pool_rules_parse_and_fail_closed");

    NT_TEST(SUITE, "pool_classification_and_envelope") {
        const std::string text =
            R"({"pools": {"compute": {"cpu_limit_percent": 30.0,)"
            R"( "members": ["train"]},)"
            R"( "io": {"cpu_limit_percent": 15.0, "members": ["searchd"]}},)"
            R"( "programs": {"other.exe": {"pool": "io"}}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "parse ok");
        gov::RulesDoc rules = *parsed;

        NT_CHECK(gov::classify_pool("python.exe",
                                    "e:\\gptbridge\\.venv\\python.exe",
                                    "python train.py", gov::Plane::Worker,
                                    rules, nullptr) == gov::Pool::Compute,
                 "member substring");
        const gov::ProgramRule* rule = &rules.programs.at("other.exe");
        NT_CHECK(gov::classify_pool("other.exe", "c:\\bin\\other.exe", "",
                                    gov::Plane::External, rules,
                                    rule) == gov::Pool::Io,
                 "program rule override");
        NT_CHECK(gov::classify_pool("python.exe",
                                    "e:\\gptbridge\\main-system\\.venv\\python.exe",
                                    "python main.py --serve train",
                                    gov::Plane::Governance, rules,
                                    nullptr) == gov::Pool::None,
                 "governance never pooled");
        gov::RulesDoc empty;
        NT_CHECK(gov::classify_pool("python.exe", "x", "train",
                                    gov::Plane::Worker, empty,
                                    nullptr) == gov::Pool::None,
                 "no pools block -> none");

        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        config.worker_job_cap = true;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        engine.procs = {worker_proc(910, 5.0)};
        gov::Snapshot snap =
            gov::govern_once(config, rules, engine, records, regulation, ctx,
                             logs);
        NT_CHECK(has_call(engine.calls, "job:-11:910:30"), "compute pool job");
        NT_CHECK(count_calls(engine.calls, "job:-1:") == 0,
                 "pool member skips worker job");
        NT_CHECK(records.begin()->second.pool_member, "pool_member flag");
        NT_CHECK(snap.top_cpu[0].pool == "compute", "row pool field");
        NT_CHECK(snap.pools.count("compute") == 1, "pool ledger present");
        NT_CHECK(snap.pools.at("compute").processes == 1, "pool count");
        NT_CHECK(snap.pools.at("compute").cpu_budget_pct == 30.0, "budget");

        engine.procs = {worker_proc(910, 5.0)};
        engine.calls.clear();
        gov::govern_once(config, rules, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(count_calls(engine.calls, "job:-11:") == 0,
                 "no repeat pool join");
    }
    NT_END_TEST(SUITE, "pool_classification_and_envelope");

    NT_TEST(SUITE, "pool_over_budget_and_excluded_attribution") {
        const std::string text =
            R"({"pools": {"compute": {"cpu_limit_percent": 30.0,)"
            R"( "members": ["train"]}, "io": {"cpu_limit_percent": 15.0}},)"
            R"( "programs": {"mytool.exe": {"exclude": true, "pool": "io"}}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "parse ok");
        gov::RulesDoc rules = *parsed;

        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        /* 260 per-core / 8 logical = 32.5% machine > 30% compute budget. */
        engine.procs = {worker_proc(920, 260.0)};
        gov::Snapshot snap =
            gov::govern_once(config, rules, engine, records, regulation, ctx,
                             logs);
        NT_CHECK(snap.pools.at("compute").over_budget, "pool over_budget");

        gov::ProcSample tool;
        tool.pid = 930;
        tool.name = "mytool.exe";
        tool.exe = "c:\\tools\\mytool.exe";
        tool.username = "u";
        tool.cpu_percore = 4.0;
        tool.create_ms = 2000000;
        /* 前輪 compute 行程保留（避免 sweep_dead_records 產生 clear 呼叫）。 */
        engine.procs = {worker_proc(920, 3.0), tool};
        engine.calls.clear();
        snap = gov::govern_once(config, rules, engine, records, regulation,
                                ctx, logs);
        NT_CHECK(!has_call(engine.calls, "job:") &&
                     !has_call(engine.calls, "limit:"),
                 "excluded pool member unmanaged");
        NT_CHECK(snap.top_cpu[0].pool == "io",
                 "excluded keeps pool attribution");
        NT_CHECK(snap.pools.count("io") == 1, "io ledger attributed");
    }
    NT_END_TEST(SUITE, "pool_over_budget_and_excluded_attribution");

    NT_TEST(SUITE, "pool_background_demotion_and_priority") {
        const std::string text =
            R"({"pools": {"background": {"cpu_limit_percent": 5.0,)"
            R"( "background": true, "ecoqos": true,)"
            R"( "priority": "below_normal", "members": ["dotnet"]}}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "parse ok");
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        config.background_mode = true;
        config.ecoqos = true;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        gov::ProcSample bg;
        bg.pid = 940;
        bg.name = "dotnet.exe";
        bg.exe = "c:\\program files\\dotnet\\dotnet.exe";
        bg.username = "u";
        bg.cpu_percore = 2.0;
        bg.create_ms = 3000000;
        engine.procs = {bg};
        gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "job:-13:940:5"), "background job");
        NT_CHECK(has_call(engine.calls, "bg:940:1"), "background mode");
        NT_CHECK(has_call(engine.calls, "eco:940:1"), "ecoqos");
        NT_CHECK(has_call(engine.calls,
                          "nice:940:" +
                              std::to_string(gov::kPriorityBelowNormal)),
                 "pool priority floor");
    }
    NT_END_TEST(SUITE, "pool_background_demotion_and_priority");

    NT_TEST(SUITE, "pool_dynamic_resizes_shared_envelope") {
        /* 池動態信封：機器 CPU ≥ pool_relief_cpu_pct 時非互動池共享
         * Job 率逐步收緊至下限；平靜且池需求頂住帽緣時逐步放回
         * 預設；互動層永不擠壓。 */
        const std::string text =
            R"({"defaults": {"pool_dynamic": true,)"
            R"( "pool_relief_cpu_pct": 75, "pool_floor_percent": 5,)"
            R"( "pool_step_percent": 4},)"
            R"( "pools": {"compute": {"cpu_limit_percent": 30.0,)"
            R"( "members": ["trainsvc"]},)"
            R"( "interactive": {"cpu_limit_percent": 40.0,)"
            R"( "members": ["uisvc"]}}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "parse ok");
        gov::RulesDoc rules = *parsed;
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        gov::ProcSample svc;
        svc.pid = 970;
        svc.name = "trainsvc.exe";
        svc.exe = "c:\\apps\\trainsvc.exe";
        svc.cmdline = "trainsvc --serve";
        svc.username = "u";
        svc.cpu_percore = 10.0;
        svc.rss_mb = 100.0;
        svc.create_ms = 5000970;
        gov::ProcSample ui = svc;
        ui.pid = 971;
        ui.name = "uisvc.exe";
        ui.exe = "c:\\apps\\uisvc.exe";
        ui.cmdline = "uisvc";
        ui.create_ms = 5000971;
        engine.procs = {svc, ui};
        gov::govern_once(config, rules, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "job:-11:970:30"), "compute joined");
        NT_CHECK(has_call(engine.calls, "job:-10:971:40"), "ui joined");

        /* 機器壓力（cpu 80 ≥ 75）→ compute 逐步收緊；互動層不動。 */
        engine.sys.cpu_load_machine = 80.0;
        engine.calls.clear();
        gov::govern_once(config, rules, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "job:-11:970:26"),
                 "compute tightened 30→26");
        NT_CHECK(count_calls(engine.calls, "job:-10:") == 0,
                 "interactive never squeezed");
        gov::govern_once(config, rules, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "job:-11:970:22"), "26→22 next cycle");

        /* 平靜＋需求頂帽（200/8=25 ≥ 22×0.9=19.8）→ 逐步放回預設。 */
        engine.sys.cpu_load_machine = 10.0;
        engine.calls.clear();
        svc.cpu_percore = 200.0;
        engine.procs = {svc, ui};
        gov::govern_once(config, rules, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "job:-11:970:26"), "relaxed 22→26");
        gov::govern_once(config, rules, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "job:-11:970:30"), "back at preset");
        const int at_preset = count_calls(engine.calls, "job:-11:");
        gov::govern_once(config, rules, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(count_calls(engine.calls, "job:-11:") == at_preset,
                 "never exceeds preset");

        /* 冷卻下行至下限封底：重進壓力多次後停在 floor。 */
        engine.sys.cpu_load_machine = 80.0;
        for (int i = 0; i < 8; ++i)
            gov::govern_once(config, rules, engine, records, regulation, ctx,
                             logs);
        NT_CHECK(has_call(engine.calls, "job:-11:970:5"), "floor reached");
    }
    NT_END_TEST(SUITE, "pool_dynamic_resizes_shared_envelope");

    return native_tests::report("resource_governor_pools_suite.json");
}

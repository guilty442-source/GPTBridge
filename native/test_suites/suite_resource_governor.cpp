// Suite: resource-governor C++23 — control-law parity with the retired
// Python implementation (A608). All cases run against a fake engine: no OS
// side effects, fully deterministic. Fake engine + fixtures 依 A185 拆至
// governor_fake_engine.h。
#include "harness.hpp"

#include "governor_fake_engine.h"

#include <map>
#include <string>
#include <vector>

namespace {

using namespace governor_suite;
const char* SUITE = "RESOURCE_GOVERNOR_SUITE";

}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "rules_loading_and_validation") {
        const std::string text =
            R"({"defaults": {"probalance": true, "limiter_percent": 25},)"
            R"("programs": {"Train.EXE": {"priority": "below_normal",)"
            R"( "affinity": [0, 1], "cpu_limit_percent": 30,)"
            R"( "background": true, "ecoqos": true},)"
            R"( "chrome.exe": {"exclude": true}}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value(), "parse ok");
        NT_CHECK(parsed->error.empty(), "no error");
        NT_CHECK(gov::json_is_true(&parsed->defaults["probalance"]), "probalance");
        NT_CHECK(parsed->programs.count("train.exe") == 1, "lowercased key");
        const gov::ProgramRule& rule = parsed->programs.at("train.exe");
        NT_CHECK(rule.priority_class.has_value() &&
                     *rule.priority_class == gov::kPriorityBelowNormal,
                 "priority");
        NT_CHECK(rule.affinity.has_value() && rule.affinity->size() == 2 &&
                     (*rule.affinity)[0] == 0 && (*rule.affinity)[1] == 1,
                 "affinity");
        NT_CHECK(rule.cpu_limit_percent == 30.0, "limit");
        NT_CHECK(rule.background && rule.ecoqos, "bg+eco");
        NT_CHECK(parsed->programs.at("chrome.exe").exclude, "exclude");

        auto bad = gov::parse_rules("{not json");
        NT_CHECK(bad.has_value() && !bad->error.empty(), "bad json error");
        NT_CHECK(bad->defaults.empty() && bad->programs.empty(), "fail-closed");

        NT_CHECK(gov::cpu_rate_value(0.1) == 10, "rate floor");
        NT_CHECK(gov::cpu_rate_value(150.0) == 10000, "rate ceiling");
    }
    NT_END_TEST(SUITE, "rules_loading_and_validation");

    NT_TEST(SUITE, "mode_preset_merge_and_unknown_mode") {
        const std::string text =
            R"({"mode": "sleep", "modes": {"sleep": {"cpu_busy": 5.0},)"
            R"( "low": {"cpu_busy": 8.0}}, "defaults": {"cpu_busy": 6.0}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "sleep ok");
        NT_CHECK(parsed->has_mode && parsed->mode == "sleep", "mode name");
        NT_CHECK(gov::json_num_or(&parsed->defaults["cpu_busy"], -1.0) == 6.0,
                 "explicit defaults beat preset");

        const std::string unknown =
            R"({"mode": "warp", "modes": {"sleep": {}}})";
        auto bad = gov::parse_rules(unknown);
        NT_CHECK(bad.has_value() && !bad->error.empty(), "unknown mode error");
    }
    NT_END_TEST(SUITE, "mode_preset_merge_and_unknown_mode");

    NT_TEST(SUITE, "feature_flags_default_off") {
        gov::GovernorConfig config = base_config();
        gov::Features features =
            gov::resolve_features(config, empty_rules().defaults, 1LL << 30);
        NT_CHECK(!features.probalance && !features.cpu_limiter &&
                     !features.background_mode && !features.ecoqos,
                 "all off by default");
        NT_CHECK(features.limiter_percent == gov::kDefaultLimiterPercent, "limiter");
        NT_CHECK(features.resp_ratio == gov::kRespStrainRatio, "ratio");

        config.probalance = false;
        std::map<std::string, gptbridge::jsonlite::JsonValue> on;
        gptbridge::jsonlite::JsonValue flag;
        flag.type = gptbridge::jsonlite::JsonValue::Type::Bool;
        flag.boolean = true;
        on["probalance"] = flag;
        NT_CHECK(!gov::resolve_features(config, on, 1LL << 30).probalance,
                 "explicit off wins");

        config.probalance = true;
        config.ecoqos = true;
        config.limiter_percent = 33.0;
        features = gov::resolve_features(config, {}, 1LL << 30);
        NT_CHECK(features.probalance && features.ecoqos, "cli enables");
        NT_CHECK(features.limiter_percent == 33.0, "cli limiter");
    }
    NT_END_TEST(SUITE, "feature_flags_default_off");

    NT_TEST(SUITE, "thresholds_precedence_cli_over_rules") {
        gov::GovernorConfig config = base_config();
        config.cpu_busy = std::nullopt;
        std::map<std::string, gptbridge::jsonlite::JsonValue> defaults;
        gptbridge::jsonlite::JsonValue value;
        value.type = gptbridge::jsonlite::JsonValue::Type::Number;
        value.number = 5.0;
        defaults["cpu_busy"] = value;
        gov::Thresholds thr = gov::resolve_thresholds(config, defaults);
        NT_CHECK(thr.cpu_busy == 5.0, "rules value used");
        config.cpu_busy = 7.0;
        NT_CHECK(gov::resolve_thresholds(config, defaults).cpu_busy == 7.0,
                 "cli wins");
        config.cpu_busy = std::nullopt;
        NT_CHECK(gov::resolve_thresholds(config, {}).cpu_busy == gov::kCpuBusyPct,
                 "constant fallback");
    }
    NT_END_TEST(SUITE, "thresholds_precedence_cli_over_rules");

    NT_TEST(SUITE, "classify_plane") {
        NT_CHECK(gov::classify_plane("x.exe", "chrome", ROOT) == gov::Plane::External,
                 "external");
        NT_CHECK(gov::classify_plane("e:\\gptbridge\\main-system\\.venv\\python.exe",
                                     "python main.py --serve",
                                     ROOT) == gov::Plane::Governance,
                 "governance");
        NT_CHECK(gov::classify_plane(
                     "e:\\gptbridge\\standalone tools\\local-model\\venv\\python.exe",
                     "", ROOT) == gov::Plane::Toolbox,
                 "toolbox");
        NT_CHECK(gov::classify_plane("e:\\gptbridge\\.venv\\python.exe",
                                     "python e:\\gptbridge\\scripts\\train.py",
                                     ROOT) == gov::Plane::Worker,
                 "worker");
        NT_CHECK(gov::classify_plane("e:\\gptbridge\\notes\\app.exe", "", ROOT) ==
                     gov::Plane::RepoOther,
                 "repo-other");
    }
    NT_END_TEST(SUITE, "classify_plane");

    NT_TEST(SUITE, "worker_ledger_and_hysteresis") {
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        engine.procs = {worker_proc(101, 50.0), worker_proc(102, 40.0)};
        gov::Snapshot snap = gov::govern_once(config, empty_rules(), engine, records,
                                              regulation, ctx, logs);
        NT_CHECK(regulation.active, "first over-budget sample regulates");
        NT_CHECK(regulation.pre, "pre-throttle engages");
        NT_CHECK(snap.worker_admission_hold, "admission hold");
        NT_CHECK(snap.over_budget, "over_budget flag");

        engine.procs = {worker_proc(101, 0.5), worker_proc(102, 0.5)};
        for (int i = 0; i < 4; ++i) {
            logs.clear();
            snap = gov::govern_once(config, empty_rules(), engine, records,
                                    regulation, ctx, logs);
            NT_CHECK(regulation.active, "hysteresis holds");
            NT_CHECK(regulation.pre, "pre holds");
        }
        logs.clear();
        snap = gov::govern_once(config, empty_rules(), engine, records, regulation,
                                ctx, logs);
        NT_CHECK(!regulation.active, "5 under samples release");
        NT_CHECK(!regulation.pre, "pre releases");
        NT_CHECK(!snap.worker_admission_hold, "hold clears");
    }
    NT_END_TEST(SUITE, "worker_ledger_and_hysteresis");

    NT_TEST(SUITE, "prethrottle_middle_band") {
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        engine.procs = {worker_proc(501, 40.0), worker_proc(502, 32.0)};
        gov::Snapshot snap = gov::govern_once(config, empty_rules(), engine, records,
                                              regulation, ctx, logs);
        NT_CHECK(regulation.pre, "middle band engages pre");
        NT_CHECK(!regulation.active, "middle band must not fully regulate");
        NT_CHECK(snap.worker_admission_hold, "hold on pre");

        engine.procs = {worker_proc(501, 0.5), worker_proc(502, 0.5)};
        for (int i = 0; i < 4; ++i) {
            logs.clear();
            gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                             logs);
            NT_CHECK(regulation.pre, "pre holds");
        }
        logs.clear();
        snap = gov::govern_once(config, empty_rules(), engine, records, regulation,
                                ctx, logs);
        NT_CHECK(!regulation.pre, "pre releases after 5");
        NT_CHECK(!snap.worker_admission_hold, "hold clears");
    }
    NT_END_TEST(SUITE, "prethrottle_middle_band");

    NT_TEST(SUITE, "kill_switch_observes_only") {
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        config.worker_job_cap = true;
        gov::CycleContext ctx = base_ctx();
        ctx.disabled = true;
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        engine.procs = {worker_proc(201, 95.0)};
        gov::Snapshot snap = gov::govern_once(config, empty_rules(), engine, records,
                                              regulation, ctx, logs);
        NT_CHECK(snap.disabled, "disabled flag");
        NT_CHECK(engine.calls.empty(), "no OS actions under kill switch");
        NT_CHECK(snap.actions.empty(), "no action entries");
    }
    NT_END_TEST(SUITE, "kill_switch_observes_only");

    NT_TEST(SUITE, "governance_plane_never_regulated") {
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation{0, 0, true, false};
        gov::ProcSample gov_proc = worker_proc(301, 95.0);
        gov_proc.exe = "e:\\gptbridge\\main-system\\.venv\\python.exe";
        gov_proc.cmdline = "python main.py --serve";
        engine.procs = {gov_proc};
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        for (int i = 0; i < 4; ++i)
            gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                             logs);
        NT_CHECK(engine.calls.empty(), "governance plane untouched");
    }
    NT_END_TEST(SUITE, "governance_plane_never_regulated");

    NT_TEST(SUITE, "worker_affinity_capped_and_restored") {
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation{0, 0, true, false};
        engine.procs = {worker_proc(401, 1.0)};
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "aff:401:"), "regulation caps affinity");
        /* budget 10% of 8 logical -> floor(0.8)=0 -> min 1 cpu. */
        NT_CHECK(has_call(engine.calls, "aff:401:0,"), "bounded subset");

        regulation.active = false;
        engine.calls.clear();
        gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                         logs);
        bool restored = false;
        for (const auto& call : engine.calls)
            if (call == "aff:401:0,1,2,3,4,5,6,7,") restored = true;
        NT_CHECK(restored, "affinity restored to full set");
    }
    NT_END_TEST(SUITE, "worker_affinity_capped_and_restored");

    NT_TEST(SUITE, "rule_actions_applied_once_and_held") {
        const std::string text =
            R"({"programs": {"python.exe": {"priority": "below_normal",)"
            R"( "affinity": [0], "cpu_limit_percent": 30,)"
            R"( "background": true, "ecoqos": true}}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "rules parse");
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        config.background_mode = true;
        config.ecoqos = true;
        config.cpu_limiter = true;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        engine.procs = {worker_proc(601, 1.0)};
        gov::govern_once(config, *parsed, engine, records, regulation, ctx, logs);
        NT_CHECK(count_calls(engine.calls, "bg:601:1") == 1, "rule bg once");
        NT_CHECK(count_calls(engine.calls, "eco:601:1") == 1, "rule eco once");
        NT_CHECK(count_calls(engine.calls, "limit:601:") == 1, "rule limit once");
        NT_CHECK(has_call(engine.calls, "aff:601:0,"), "rule affinity");
        NT_CHECK(!has_call(engine.calls, "nice:601:"), "bg rule suppresses priority");

        gov::govern_once(config, *parsed, engine, records, regulation, ctx, logs);
        NT_CHECK(count_calls(engine.calls, "bg:601:1") == 1, "rule never repeats");
        const gov::ProcessRecord& record = records.begin()->second;
        NT_CHECK(record.rule_applied, "rule_applied");
        NT_CHECK(record.rule_hold.count("bg") && record.rule_hold.count("eco") &&
                     record.rule_hold.count("limit") &&
                     record.rule_hold.count("affinity"),
                 "rule_hold set");
    }
    NT_END_TEST(SUITE, "rule_actions_applied_once_and_held");

    NT_TEST(SUITE, "dynamic_tiers_feature_gated") {
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        config.worker_job_cap = false;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        engine.procs = {worker_proc(701, 95.0)};
        for (int i = 0; i < 6; ++i)
            gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                             logs);
        NT_CHECK(!has_lasso_call(engine.calls), "tiers stay off until enabled");

        records.clear();
        regulation = gov::RegState{};
        engine.calls.clear();
        config.background_mode = true;
        config.ecoqos = true;
        config.cpu_limiter = true;
        config.limiter_percent = 5.0;
        for (int i = 0; i < 6; ++i)
            gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                             logs);
        NT_CHECK(has_call(engine.calls, "bg:701:1"), "bg engages");
        NT_CHECK(has_call(engine.calls, "eco:701:1"), "eco engages");
        NT_CHECK(has_call(engine.calls, "limit:701:5"), "limiter engages");
        NT_CHECK(!has_call(engine.calls, "bg:701:0"), "no early release");

        engine.procs = {worker_proc(701, 1.0)};
        for (int i = 0; i < gov::kCalmSamples + 1; ++i)
            gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                             logs);
        NT_CHECK(has_call(engine.calls, "bg:701:0"), "bg released");
        NT_CHECK(has_call(engine.calls, "eco:701:0"), "eco released");
        NT_CHECK(has_call(engine.calls, "clear:"), "limit released");
    }
    NT_END_TEST(SUITE, "dynamic_tiers_feature_gated");

    NT_TEST(SUITE, "probalance_demotes_and_restores") {
        FakeEngine engine;
        engine.sys = sys8();
        engine.latency = 220.0;
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        config.probalance = true;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        regulation.resp_baseline = 100.0;
        regulation.has_baseline = true;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        gov::ProcSample app;
        app.pid = 801;
        app.name = "app.exe";
        app.exe = "c:\\apps\\app.exe";
        app.username = "u";
        app.cpu_percore = 60.0;
        app.create_ms = 2000000;
        engine.procs = {app};
        gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                         logs);
        NT_CHECK(engine.calls.empty(), "one strain sample must not demote");
        gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                         logs);
        NT_CHECK(regulation.strained, "strained after 2 samples");
        NT_CHECK(records.begin()->second.pb_set, "demoted");
        NT_CHECK(has_call(engine.calls,
                          "nice:801:" + std::to_string(gov::kPriorityBelowNormal)),
                 "demote lowers priority");

        engine.latency = 100.0;
        gov::ProcSample calm = app;
        calm.cpu_percore = 1.0;
        engine.procs = {calm};
        for (int i = 0; i < gov::kRespCalmSamples + 1; ++i)
            gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                             logs);
        NT_CHECK(!regulation.strained, "strain clears");
        NT_CHECK(!records.begin()->second.pb_set, "restored");
    }
    NT_END_TEST(SUITE, "probalance_demotes_and_restores");

    NT_TEST(SUITE, "monitoring_surface_fields") {
        FakeEngine engine;
        engine.sys = sys8();
        engine.latency = 42.0;
        gov::GovernorConfig config = base_config();
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        gov::ProcSample proc = worker_proc(901, 1.0);
        proc.io_read_mb = 5.0;
        proc.io_write_mb = 2.0;
        engine.procs = {proc};
        gov::Snapshot snap =
            gov::govern_once(config, empty_rules(), engine, records, regulation,
                             ctx, logs);
        NT_CHECK(!snap.top_cpu.empty(), "top_cpu present");
        NT_CHECK(snap.top_cpu[0].io_read_mb == 5.0, "io read");
        NT_CHECK(snap.top_cpu[0].io_write_mb == 2.0, "io write");
        NT_CHECK(snap.top_cpu[0].flags.empty(), "no flags");
        NT_CHECK(snap.resp_latency_ms == 42.0, "latency");
        NT_CHECK(!snap.resp_strained, "not strained");
        NT_CHECK(!snap.pb_enabled, "probalance off");
        NT_CHECK(!snap.features.probalance && !snap.features.cpu_limiter,
                 "features off");
        NT_CHECK(snap.rules_error.empty(), "no rules error");
        const std::string tail = "resource-governor-rules.json";
        NT_CHECK(snap.rules_path.size() >= tail.size() &&
                     snap.rules_path.compare(snap.rules_path.size() - tail.size(),
                                             tail.size(), tail) == 0,
                 "rules path");
        const std::string text =
            gptbridge::jsonlite::json_serialize(gov::snapshot_to_json(snap));
        for (const char* key :
             {"\"worker_ledger\"", "\"regulation\"", "\"responsiveness\"",
              "\"probalance\"", "\"features\"", "\"worker_admission_hold\"",
              "\"top_cpu\"", "\"top_mem\"", "\"actions\""})
            NT_CHECK(text.find(key) != std::string::npos, key);
    }
    NT_END_TEST(SUITE, "monitoring_surface_fields");

    NT_TEST(SUITE, "worker_job_cap_assigns_shared_job_once") {
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        config.worker_job_cap = true;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        engine.procs = {worker_proc(810, 5.0), worker_proc(811, 5.0)};
        gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                         logs);
        NT_CHECK(count_calls(engine.calls, "job:") == 2, "both join once");
        NT_CHECK(has_call(engine.calls, "job:-1:810:10"), "aggregate budget rate");
        gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                         logs);
        NT_CHECK(count_calls(engine.calls, "job:") == 2, "no repeat joins");

        config.worker_job_cap = false;
        engine.calls.clear();
        records.clear();
        engine.procs = {worker_proc(820, 95.0)};
        gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                         logs);
        NT_CHECK(!has_call(engine.calls, "job:"), "explicit opt-out wins");

        config.worker_job_cap = true;
        config.worker_job_percent = 7.5;
        engine.calls.clear();
        records.clear();
        engine.procs = {worker_proc(830, 5.0)};
        gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "job:-1:830:7.5"), "percent override");

        engine.calls.clear();
        records.clear();
        gov::ProcSample gov_proc = worker_proc(840, 5.0);
        gov_proc.exe = "e:\\gptbridge\\main-system\\.venv\\python.exe";
        gov_proc.cmdline = "python main.py --serve";
        engine.procs = {gov_proc};
        gov::govern_once(config, empty_rules(), engine, records, regulation, ctx,
                         logs);
        NT_CHECK(engine.calls.empty(), "governance plane exempt");
    }
    NT_END_TEST(SUITE, "worker_job_cap_assigns_shared_job_once");

    NT_TEST(SUITE, "dynamic_limiter_tightens_and_relaxes") {
        /* 動態升降：極端持續 → Job 比率逐週期收緊至下限；需求回落 →
         * 逐步放寬回預設上限；首輪套用當週期不即時再調。 */
        const std::string text =
            R"({"defaults": {"cpu_limiter": true, "limiter_dynamic": true,)"
            R"( "limiter_percent": 10, "limiter_min_percent": 5,)"
            R"( "limiter_step_percent": 2}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "rules parse");
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        engine.procs = {worker_proc(910, 95.0)};
        for (int i = 0; i < 6; ++i)
            gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                             logs);
        NT_CHECK(has_call(engine.calls, "limit:910:10"), "initial cap 10");
        NT_CHECK(!has_call(engine.calls, "limit:910:8"),
                 "no same-cycle tighten");

        gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "limit:910:8"), "tighten step 10→8");
        gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "limit:910:6"), "tighten 8→6");
        gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "limit:910:5"), "floor 5 reached");
        const int at_floor = count_calls(engine.calls, "limit:910:");
        gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(count_calls(engine.calls, "limit:910:") == at_floor,
                 "holds at floor while extreme");

        /* 需求回落：量測低於帽緣一半（cap 5%×8核=40 → ≤20）→ 放寬。 */
        engine.procs = {worker_proc(910, 15.0)};
        engine.calls.clear();
        gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "limit:910:7"), "relax step 5→7");
        gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "limit:910:9"), "relax 7→9");
        gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "limit:910:10"), "back at base cap");
        const int at_base = count_calls(engine.calls, "limit:910:");
        gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(count_calls(engine.calls, "limit:910:") == at_base,
                 "never exceeds base preset");
    }
    NT_END_TEST(SUITE, "dynamic_limiter_tightens_and_relaxes");

    NT_TEST(SUITE, "dynamic_limiter_off_keeps_fixed_rate") {
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        config.cpu_limiter = true;
        config.limiter_percent = 10.0;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        engine.procs = {worker_proc(920, 95.0)};
        for (int i = 0; i < 10; ++i)
            gov::govern_once(config, empty_rules(), engine, records, regulation,
                             ctx, logs);
        NT_CHECK(count_calls(engine.calls, "limit:920:") == 1,
                 "legacy fixed rate: one shot only");
    }
    NT_END_TEST(SUITE, "dynamic_limiter_off_keeps_fixed_rate");

    NT_TEST(SUITE, "priority_escalates_to_idle_and_steps_back") {
        /* 個別程序動態優先序：extreme 持續超過 sustain+extreme_sustain
         * → below_normal 再降 idle；跌回 extreme 以下 → 先回
         * below_normal；完整 calm 釋放還是回 normal。 */
        const std::string text =
            R"({"defaults": {"priority_escalate": true}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "rules parse");
        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        gov::ProcSample hog;
        hog.pid = 930;
        hog.name = "hog.exe";
        hog.exe = "c:\\apps\\hog.exe"; /* 非 worker 平面也適用 */
        hog.username = "u";
        hog.cpu_percore = 95.0;
        hog.create_ms = 3000000;
        engine.procs = {hog};
        for (int i = 0; i < 8; ++i)
            gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                             logs);
        NT_CHECK(has_call(engine.calls, "nice:930:16384"), "below_normal set");
        NT_CHECK(!has_call(engine.calls, "nice:930:64"), "not idle yet");
        gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(has_call(engine.calls, "nice:930:64"),
                 "escalated to idle at sample 9");

        hog.cpu_percore = 60.0; /* 仍 busy 但 <extreme 90 */
        engine.procs = {hog};
        gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                         logs);
        NT_CHECK(count_calls(engine.calls, "nice:930:16384") == 2,
                 "stepped back to below_normal");

        hog.cpu_percore = 1.0;
        engine.procs = {hog};
        for (int i = 0; i < gov::kCalmSamples + 1; ++i)
            gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                             logs);
        NT_CHECK(has_call(engine.calls, "nice:930:32"), "calm fully restores");
    }
    NT_END_TEST(SUITE, "priority_escalates_to_idle_and_steps_back");

    NT_TEST(SUITE, "reclaim_pass_trims_largest_under_mem_pressure") {
        /* 回收機制：mem_used ≥ reclaim_mem_pct 時按 RSS 降序批次修整
         * 工作集（每週期 reclaim_batch 個）；前景行程與小行程豁免，
         * 低於閾值完全不動作；跨週期受 trim_cooldown 節制補齊。 */
        const std::string text =
            R"({"defaults": {"reclaim_enabled": true, "reclaim_mem_pct": 80,)"
            R"( "reclaim_batch": 2, "reclaim_min_mb": 500}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "rules parse");
        FakeEngine engine;
        engine.sys = sys8();
        engine.sys.mem_used_pct = 85.0;
        engine.foreground = 960;
        gov::GovernorConfig config = base_config();
        config.dry_run = false;
        /* 高於測試 RSS：把冷靜修整路徑（maybe_trim）與 reclaim 分離。 */
        config.mem_trim_mb = 10000.0;
        gov::CycleContext ctx = base_ctx();
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;
        auto big = [](int pid, double rss) {
            gov::ProcSample s = worker_proc(pid, 1.0);
            s.rss_mb = rss;
            s.create_ms = 4000000 + pid;
            return s;
        };
        engine.procs = {big(951, 3000.0), big(952, 2000.0), big(953, 1000.0),
                        big(954, 100.0), big(960, 4000.0)};
        gov::Snapshot snap = gov::govern_once(config, *parsed, engine, records,
                                              regulation, ctx, logs);
        NT_CHECK(snap.reclaim_active && snap.reclaim_trimmed == 2,
                 "reclaim active, batch bound");
        NT_CHECK(has_call(engine.calls, "trim:951"), "largest trimmed first");
        NT_CHECK(has_call(engine.calls, "trim:952"), "second trimmed");
        NT_CHECK(!has_call(engine.calls, "trim:953"), "batch bound holds");
        NT_CHECK(!has_call(engine.calls, "trim:954"), "below min_mb skipped");
        NT_CHECK(!has_call(engine.calls, "trim:960"), "foreground exempt");

        /* 下一週期仍在 cooldown（+200 < 300）：已修整的 951/952 跳過，
         * 批次補齊第三個候選。 */
        ctx.now_mono += 200.0;
        engine.calls.clear();
        snap = gov::govern_once(config, *parsed, engine, records, regulation,
                                ctx, logs);
        NT_CHECK(has_call(engine.calls, "trim:953"), "next cycle trims rest");
        NT_CHECK(count_calls(engine.calls, "trim:") == 1, "cooldown respected");

        ctx.now_mono += 400.0;
        engine.sys.mem_used_pct = 50.0;
        engine.calls.clear();
        snap = gov::govern_once(config, *parsed, engine, records, regulation,
                                ctx, logs);
        NT_CHECK(!snap.reclaim_active, "inactive below threshold");
        NT_CHECK(!has_call(engine.calls, "trim:"), "no trim when calm");
    }
    NT_END_TEST(SUITE, "reclaim_pass_trims_largest_under_mem_pressure");

    return native_tests::report("resource_governor_suite.json");
}

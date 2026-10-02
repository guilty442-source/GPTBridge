// Suite: resource-governor advisor — 自動模式顧問控制律（B167/B38 原生接替；
// B3 有界自適應：ceiling 上限、streak/cooldown 滯回、夜間省電、fail-closed）。
// 全部經純函式 evaluate_advisor 或假引擎 govern_once 驗證，零 OS 副作用。
#include "harness.hpp"

#include "governor_fake_engine.h"

#include <string>
#include <vector>

namespace {

using namespace governor_suite;
const char* SUITE = "RESOURCE_GOVERNOR_ADVISOR_SUITE";

gov::AdvisorPolicy enabled_policy() {
    gov::AdvisorPolicy policy;
    policy.enabled = true;
    return policy;
}

gov::AdvisorSignals calm_signals() {
    gov::AdvisorSignals sig;
    sig.configured_mode = "medium";
    sig.valid_modes = {"sleep", "low", "medium", "high"};
    sig.cpu_load_pct = 20.0;
    sig.mem_used_pct = 40.0;
    sig.budget_cpu_pct = 10.0;
    sig.budget_ram_pct = 30.0;
    sig.local_minutes = 12 * 60; /* 中午：不在 22:00-07:00 窗口 */
    sig.now_unix = 1'000'000.0;
    sig.now_mono = 1'000.0;
    return sig;
}

}  // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "policy_parse_and_validation") {
        const std::string text =
            R"({"auto_mode": true, "mode": "medium",)"
            R"("auto": {"ceiling": "high", "streak_up": 4,)"
            R"( "cooldown_s": 300, "eval_interval_s": 30},)"
            R"("power_saving_schedule": {"enabled": true, "start": "23:30",)"
            R"( "end": "06:15", "mode": "sleep"},)"
            R"("modes": {"sleep": {}, "low": {}, "medium": {}, "high": {}}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "parse ok");
        NT_CHECK(parsed->auto_mode, "auto_mode parsed");
        NT_CHECK(parsed->advisor.enabled, "advisor enabled");
        NT_CHECK(parsed->advisor.ceiling == "high", "ceiling parsed");
        NT_CHECK(parsed->advisor.streak_up == 4, "streak_up");
        NT_CHECK(parsed->advisor.cooldown_s == 300.0, "cooldown");
        NT_CHECK(parsed->advisor.eval_interval_s == 30.0, "eval interval");
        NT_CHECK(parsed->advisor.schedule_start_min == 23 * 60 + 30 &&
                     parsed->advisor.schedule_end_min == 6 * 60 + 15,
                 "schedule minutes");
        NT_CHECK(parsed->advisor.schedule_mode == "sleep", "schedule mode");

        const std::string bad_ceiling =
            R"({"auto_mode": true, "auto": {"ceiling": "turbo"},)"
            R"("modes": {"low": {}, "medium": {}}})";
        auto bad = gov::parse_rules(bad_ceiling);
        NT_CHECK(bad.has_value() && !bad->error.empty(),
                 "unknown ceiling is a rules error");

        const std::string bad_sched =
            R"({"auto_mode": true,)"
            R"("power_saving_schedule": {"mode": "turbo"},)"
            R"("modes": {"low": {}, "medium": {}, "sleep": {}}})";
        auto bad2 = gov::parse_rules(bad_sched);
        NT_CHECK(bad2.has_value() && !bad2->error.empty(),
                 "unknown schedule mode is a rules error");

        /* 無 auto 區塊：預設 ceiling=medium、auto_mode 預設 false。 */
        auto minimal = gov::parse_rules(
            R"({"mode": "low", "modes": {"low": {}, "medium": {}}})");
        NT_CHECK(minimal.has_value() && minimal->error.empty(), "minimal ok");
        NT_CHECK(!minimal->auto_mode && !minimal->advisor.enabled,
                 "auto off by default");
        NT_CHECK(minimal->advisor.ceiling == "medium", "default ceiling");
    }
    NT_END_TEST(SUITE, "policy_parse_and_validation");

    NT_TEST(SUITE, "mode_rank_and_schedule_window") {
        NT_CHECK(gov::mode_rank("sleep") < gov::mode_rank("low"), "rank low");
        NT_CHECK(gov::mode_rank("low") < gov::mode_rank("medium"), "rank med");
        NT_CHECK(gov::mode_rank("medium") < gov::mode_rank("high"), "rank hi");
        NT_CHECK(gov::mode_rank("bogus") == gov::mode_rank("medium"),
                 "unknown = medium");
        /* 跨午夜窗口 22:00-07:00。 */
        NT_CHECK(gov::in_schedule_window(23 * 60, 22 * 60, 7 * 60), "23:00 in");
        NT_CHECK(gov::in_schedule_window(3 * 60, 22 * 60, 7 * 60), "03:00 in");
        NT_CHECK(!gov::in_schedule_window(12 * 60, 22 * 60, 7 * 60), "12:00 out");
        NT_CHECK(!gov::in_schedule_window(21 * 60 + 59, 22 * 60, 7 * 60),
                 "21:59 out");
        NT_CHECK(!gov::in_schedule_window(-1, 22 * 60, 7 * 60), "unknown out");
        NT_CHECK(!gov::in_schedule_window(100, 60, 60), "empty window off");
    }
    NT_END_TEST(SUITE, "mode_rank_and_schedule_window");

    NT_TEST(SUITE, "night_window_forces_sleep_immediately") {
        gov::AdvisorPolicy policy = enabled_policy();
        gov::AdvisorState state;
        gov::AdvisorSignals sig = calm_signals();
        sig.local_minutes = 23 * 60;
        const gov::AdvisorDecision d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.evaluated && d.changed && d.urgent, "night switches at once");
        NT_CHECK(d.target == "sleep" && state.applied_mode == "sleep",
                 "applied sleep");
        NT_CHECK(d.reason.find("night") != std::string::npos, "night reason");
    }
    NT_END_TEST(SUITE, "night_window_forces_sleep_immediately");

    NT_TEST(SUITE, "strain_forces_low_immediately") {
        gov::AdvisorPolicy policy = enabled_policy();
        {
            gov::AdvisorState state;
            gov::AdvisorSignals sig = calm_signals();
            sig.strained = true;
            const gov::AdvisorDecision d =
                gov::evaluate_advisor(policy, sig, state);
            NT_CHECK(d.changed && d.urgent && d.target == "low",
                     "strained → low now");
        }
        {
            gov::AdvisorState state;
            gov::AdvisorSignals sig = calm_signals();
            sig.cpu_load_pct = 95.0; /* ≥ strain_cpu_pct 85 */
            const gov::AdvisorDecision d =
                gov::evaluate_advisor(policy, sig, state);
            NT_CHECK(d.changed && d.urgent && d.target == "low",
                     "machine overload → low now");
            NT_CHECK(d.reason.find("overload") != std::string::npos,
                     "overload reason");
        }
        /* 降檔不受 cooldown 限制（urgent 豁免）。 */
        gov::AdvisorState state;
        state.applied_mode = "high";
        state.last_switch_unix = calm_signals().now_unix;
        gov::AdvisorSignals sig = calm_signals();
        sig.strained = true;
        const gov::AdvisorDecision d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.changed && d.target == "low", "urgent bypasses cooldown");
    }
    NT_END_TEST(SUITE, "strain_forces_low_immediately");

    NT_TEST(SUITE, "ceiling_bounds_upgrade_and_streak") {
        gov::AdvisorPolicy policy = enabled_policy();
        policy.ceiling = "medium"; /* 使用者可用性上限 */
        policy.streak_up = 2;
        gov::AdvisorState state;
        state.applied_mode = "low"; /* 從 low 升檔 */
        gov::AdvisorSignals sig = calm_signals();
        sig.reg_active = true; /* worker 需求 */

        const gov::AdvisorDecision first =
            gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(first.evaluated && !first.changed, "streak 1/2 holds");
        NT_CHECK(first.target == "medium", "high clamped to ceiling");
        NT_CHECK(first.reason.find("ceiling") != std::string::npos,
                 "ceiling reason recorded");
        NT_CHECK(state.applied_mode == "low", "still low");

        sig.now_mono += 60.0;
        const gov::AdvisorDecision second =
            gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(second.changed && second.target == "medium" &&
                     state.applied_mode == "medium",
                 "streak 2/2 upgrades to ceiling");
        NT_CHECK(!second.urgent, "upgrade not urgent");
    }
    NT_END_TEST(SUITE, "ceiling_bounds_upgrade_and_streak");

    NT_TEST(SUITE, "downgrade_needs_streak_and_cooldown") {
        gov::AdvisorPolicy policy = enabled_policy();
        policy.streak_down = 3;
        policy.cooldown_s = 600.0;
        gov::AdvisorState state;
        state.applied_mode = "high";
        state.last_switch_unix = calm_signals().now_unix;
        gov::AdvisorSignals sig = calm_signals(); /* target = medium baseline */
        sig.user_idle_s = 600.0; /* 閒置中 → high 屬合法（idle_ceiling），
                                    降檔走正常 streak/cooldown 而非 urgent */

        for (int i = 0; i < 2; ++i) {
            const gov::AdvisorDecision d =
                gov::evaluate_advisor(policy, sig, state);
            NT_CHECK(d.evaluated && !d.changed, "downgrade streak holds");
            sig.now_mono += 60.0;
        }
        gov::AdvisorDecision d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(!d.changed && d.reason.find("cooldown") != std::string::npos,
                 "cooldown blocks 3rd-streak downgrade");

        sig.now_mono += 60.0;
        sig.now_unix += 600.0;
        d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.changed && d.target == "medium" &&
                     state.applied_mode == "medium",
                 "downgrade applies after cooldown");
    }
    NT_END_TEST(SUITE, "downgrade_needs_streak_and_cooldown");

    NT_TEST(SUITE, "cadence_skips_between_evals") {
        gov::AdvisorPolicy policy = enabled_policy();
        policy.eval_interval_s = 60.0;
        gov::AdvisorState state;
        gov::AdvisorSignals sig = calm_signals();
        gov::AdvisorDecision d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.evaluated, "first eval runs");
        sig.now_mono += 30.0; /* < 60s 節拍 */
        d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(!d.evaluated && !d.changed, "cadence skips");
        sig.now_mono += 31.0; /* ≥ 60s */
        d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.evaluated, "cadence resumes");
    }
    NT_END_TEST(SUITE, "cadence_skips_between_evals");

    NT_TEST(SUITE, "fail_closed_on_rules_error_and_disable") {
        gov::AdvisorPolicy policy = enabled_policy();
        gov::AdvisorState state;
        state.applied_mode = "low";
        gov::AdvisorSignals sig = calm_signals();
        sig.rules_error = true;
        gov::AdvisorDecision d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.evaluated && !d.changed && state.applied_mode == "low",
                 "rules-error keeps last verified state");

        sig.rules_error = false;
        policy.enabled = false; /* 手動選檔關閉自動（user intent wins） */
        d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.evaluated && state.applied_mode.empty(),
                 "disabled clears takeover");
        NT_CHECK(d.reason == "auto-disabled", "disabled reason");
    }
    NT_END_TEST(SUITE, "fail_closed_on_rules_error_and_disable");

    NT_TEST(SUITE, "govern_once_applies_advisor_mode_next_cycle") {
        const std::string text =
            R"({"auto_mode": true, "mode": "medium",)"
            R"("auto": {"ceiling": "medium", "eval_interval_s": 5},)"
            R"("power_saving_schedule": {"enabled": true, "start": "22:00",)"
            R"( "end": "07:00", "mode": "sleep"},)"
            R"("modes": {"medium": {"worker_job_percent": 10.0},)"
            R"( "low": {"worker_job_percent": 5.0},)"
            R"( "high": {"worker_job_percent": 30.0},)"
            R"( "sleep": {"worker_job_percent": 2.0}}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "rules parse");

        FakeEngine engine;
        engine.sys = sys8();
        gov::GovernorConfig config = base_config();
        gov::CycleContext ctx = base_ctx();
        ctx.local_minutes = 23 * 60; /* 夜間窗口 */
        ctx.now_unix = 1'000'000.0;
        gov::RecordMap records;
        gov::RegState regulation;
        std::vector<gptbridge::jsonlite::JsonValue> logs;

        gov::Snapshot snap = gov::govern_once(config, *parsed, engine, records,
                                              regulation, ctx, logs);
        NT_CHECK(snap.auto_mode, "auto_mode reported");
        NT_CHECK(snap.mode == "medium", "cycle 1 still configured mode");
        NT_CHECK(snap.advisor.has_value(), "advisor record emitted");
        NT_CHECK(snap.mode_audit.has_value(), "mode switch audited");
        NT_CHECK(regulation.advisor.applied_mode == "sleep", "advisor took over");

        ctx.now_mono += 10.0; /* 跨過 eval 節拍 */
        snap = gov::govern_once(config, *parsed, engine, records, regulation,
                                ctx, logs);
        NT_CHECK(snap.mode == "sleep", "effective mode applies next cycle");
        NT_CHECK(snap.features.worker_job_percent == 2.0,
                 "sleep preset drives features");
    }
    NT_END_TEST(SUITE, "govern_once_applies_advisor_mode_next_cycle");

    NT_TEST(SUITE, "idle_full_speed_allows_high") {
        gov::AdvisorPolicy policy = enabled_policy();
        policy.ceiling = "medium";
        policy.idle_ceiling = "high";
        policy.idle_after_s = 300.0;
        policy.streak_up = 2;
        gov::AdvisorState state;
        state.applied_mode = "medium";
        gov::AdvisorSignals sig = calm_signals();
        sig.user_idle_s = 600.0; /* 閒置 10 分鐘 */
        sig.reg_active = true;   /* worker 需求＋整機餘裕 */

        gov::AdvisorDecision d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.evaluated && d.idle_active, "idle detected");
        NT_CHECK(d.eff_ceiling == "high", "idle ceiling effective");
        NT_CHECK(d.target == "high" && !d.changed, "streak 1/2 holds at high");
        sig.now_mono += 60.0;
        d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.changed && state.applied_mode == "high",
                 "idle full speed applies");
    }
    NT_END_TEST(SUITE, "idle_full_speed_allows_high");

    NT_TEST(SUITE, "returning_user_demotes_urgently") {
        gov::AdvisorPolicy policy = enabled_policy();
        policy.ceiling = "medium";
        policy.idle_ceiling = "high";
        policy.streak_down = 3;
        policy.cooldown_s = 600.0;
        gov::AdvisorState state;
        state.applied_mode = "high";      /* 閒置時升到 high */
        state.last_switch_unix = calm_signals().now_unix; /* 剛切換 */
        gov::AdvisorSignals sig = calm_signals();
        sig.user_idle_s = 15.0; /* 使用者回來了 */

        const gov::AdvisorDecision d =
            gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(!d.idle_active, "idle ended");
        NT_CHECK(d.changed && d.urgent && d.target == "medium",
                 "returning user: urgent demote ignores streak+cooldown");
        NT_CHECK(state.applied_mode == "medium", "back under ceiling");
    }
    NT_END_TEST(SUITE, "returning_user_demotes_urgently");

    NT_TEST(SUITE, "idle_unknown_or_disabled_fails_to_ceiling") {
        gov::AdvisorPolicy policy = enabled_policy();
        policy.ceiling = "medium";
        policy.idle_ceiling = "high";
        /* user_idle_s=-1（GetLastInputInfo 失敗）→ 視同使用中。 */
        {
            gov::AdvisorState state;
            gov::AdvisorSignals sig = calm_signals();
            sig.reg_active = true;
            const gov::AdvisorDecision d =
                gov::evaluate_advisor(policy, sig, state);
            NT_CHECK(!d.idle_active && d.target == "medium",
                     "unknown idle → ceiling clamp");
        }
        /* idle_full_speed=false → 閒置也不放行。 */
        {
            policy.idle_full_speed = false;
            gov::AdvisorState state;
            gov::AdvisorSignals sig = calm_signals();
            sig.user_idle_s = 3600.0;
            sig.reg_active = true;
            const gov::AdvisorDecision d =
                gov::evaluate_advisor(policy, sig, state);
            NT_CHECK(!d.idle_active && d.target == "medium",
                     "feature off → ceiling clamp");
        }
    }
    NT_END_TEST(SUITE, "idle_unknown_or_disabled_fails_to_ceiling");

    NT_TEST(SUITE, "govern_once_respects_ceiling_during_day") {
        const std::string text =
            R"({"auto_mode": true, "mode": "low",)"
            R"("auto": {"ceiling": "medium", "eval_interval_s": 5,)"
            R"( "streak_up": 2},)"
            R"("power_saving_schedule": {"enabled": false},)"
            R"("modes": {"low": {"worker_job_percent": 5.0},)"
            R"( "medium": {"worker_job_percent": 10.0},)"
            R"( "high": {"worker_job_percent": 30.0}}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "rules parse");

        FakeEngine engine;
        engine.sys = sys8(); /* 整機低負載 → 有 headroom */
        gov::GovernorConfig config = base_config();
        gov::CycleContext ctx = base_ctx();
        ctx.local_minutes = 12 * 60;
        ctx.now_unix = 1'000'000.0;
        gov::RecordMap records;
        gov::RegState regulation;
        regulation.active = true; /* worker 需求存在 */
        std::vector<gptbridge::jsonlite::JsonValue> logs;

        for (int i = 0; i < 4; ++i) {
            ctx.now_mono += 10.0;
            gov::govern_once(config, *parsed, engine, records, regulation, ctx,
                             logs);
        }
        NT_CHECK(regulation.advisor.applied_mode == "medium",
                 "ceiling medium reached");
        NT_CHECK(regulation.advisor.applied_mode != "high", "never above");
        gov::Snapshot snap = gov::govern_once(config, *parsed, engine, records,
                                              regulation, ctx, logs);
        NT_CHECK(snap.mode == "medium", "effective = ceiling");
        NT_CHECK(snap.features.worker_job_percent == 10.0, "medium preset");
    }
    NT_END_TEST(SUITE, "govern_once_respects_ceiling_during_day");

    NT_TEST(SUITE, "ema_smoothing_policy_parse") {
        const std::string text =
            R"({"auto_mode": true,)"
            R"("auto": {"signal_alpha": 0.3, "strain_instant_margin": 12.5,)"
            R"( "eval_interval_busy_s": 15},)"
            R"("power_saving_schedule": {"enabled": false},)"
            R"("modes": {"low": {}, "medium": {}, "high": {}}})";
        auto parsed = gov::parse_rules(text);
        NT_CHECK(parsed.has_value() && parsed->error.empty(), "parse ok");
        NT_CHECK(std::abs(parsed->advisor.signal_alpha - 0.3) < 1e-9,
                 "signal_alpha parsed");
        NT_CHECK(std::abs(parsed->advisor.strain_instant_margin - 12.5) < 1e-9,
                 "strain_instant_margin parsed");
        NT_CHECK(std::abs(parsed->advisor.eval_interval_busy_s - 15.0) < 1e-9,
                 "busy cadence parsed");
        /* busy ≤0 停用 → 恆用基礎節拍。 */
        const std::string off =
            R"({"auto_mode": true, "auto": {"eval_interval_busy_s": 0},)"
            R"("power_saving_schedule": {"enabled": false},)"
            R"("modes": {"low": {}, "medium": {}, "high": {}}})";
        auto parsed_off = gov::parse_rules(off);
        NT_CHECK(parsed_off.has_value() &&
                     parsed_off->advisor.eval_interval_busy_s == 0.0,
                 "busy cadence 0 = disabled");
    }
    NT_END_TEST(SUITE, "ema_smoothing_policy_parse");

    NT_TEST(SUITE, "ema_absorbs_single_busy_spike") {
        /* 單一繁忙取樣窗口（如 20s 編譯尖峰）不再 urgent 降檔——
         * 只有 EMA 持續越線或達極端門檻才降。 */
        gov::AdvisorPolicy policy = enabled_policy();
        policy.signal_alpha = 0.5;         /* ema = 0.5*ema + 0.5*now */
        policy.strain_instant_margin = 10.0; /* instant ≥95 才即時降 */
        policy.eval_interval_busy_s = 0.0; /* 本測試固定節拍 */
        gov::AdvisorState state;
        gov::AdvisorSignals sig = calm_signals(); /* cpu 20 / mem 40 */
        gov::AdvisorDecision d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(!d.changed, "calm baseline holds");
        NT_CHECK(std::abs(state.cpu_ema - 20.0) < 0.01, "ema seeded");

        sig.cpu_load_pct = 90.0; /* ≥strain 85 但 <極端 95 */
        sig.now_mono += 60.0;
        d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(!d.changed && !d.urgent, "single 90% spike absorbed");
        NT_CHECK(std::abs(d.cpu_load_ema - 55.0) < 0.01, "ema=55");

        /* 持續超載：ema 72.5→81.25→85.625，第五次取樣才降 low。 */
        for (int i = 0; i < 2; ++i) {
            sig.now_mono += 60.0;
            d = gov::evaluate_advisor(policy, sig, state);
            NT_CHECK(!d.changed, "sustained ramp still absorbing");
        }
        sig.now_mono += 60.0;
        d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.changed && d.urgent && d.target == "low",
                 "sustained overload demotes via ema");
        NT_CHECK(state.applied_mode == "low", "low applied");
    }
    NT_END_TEST(SUITE, "ema_absorbs_single_busy_spike");

    NT_TEST(SUITE, "extreme_instant_spike_stays_urgent") {
        /* 極端尖峰（≥strain+margin）不受平滑影響，立即降檔保回應。 */
        gov::AdvisorPolicy policy = enabled_policy();
        policy.signal_alpha = 0.5;
        policy.strain_instant_margin = 10.0;
        policy.eval_interval_busy_s = 0.0;
        gov::AdvisorState state;
        gov::AdvisorSignals sig = calm_signals();
        gov::evaluate_advisor(policy, sig, state); /* 播種 ema=20 */

        sig.cpu_load_pct = 97.0; /* ≥85+10 極端 */
        sig.now_mono += 60.0;
        const gov::AdvisorDecision d =
            gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.changed && d.urgent && d.target == "low",
                 "extreme spike urgent despite ema 58.5");
    }
    NT_END_TEST(SUITE, "extreme_instant_spike_stays_urgent");

    NT_TEST(SUITE, "headroom_gate_uses_smoothed_signal") {
        /* 重載中一個安靜窗口不能放行升檔：headroom 看 EMA。 */
        gov::AdvisorPolicy policy = enabled_policy();
        policy.signal_alpha = 0.5;
        policy.eval_interval_busy_s = 0.0;
        gov::AdvisorState state;
        gov::AdvisorSignals sig = calm_signals();
        sig.cpu_load_pct = 80.0; /* 重載（≥headroom 60） */
        gov::AdvisorDecision d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(!d.headroom, "loaded machine has no headroom");

        sig.cpu_load_pct = 40.0; /* 瞬時安靜（<60），ema=60 仍未達 */
        sig.now_mono += 60.0;
        d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(!d.headroom, "one quiet window not enough (ema=60)");

        sig.now_mono += 60.0;
        d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.headroom, "sustained calm opens headroom (ema=50)");
    }
    NT_END_TEST(SUITE, "headroom_gate_uses_smoothed_signal");

    NT_TEST(SUITE, "busy_cadence_evaluates_sooner_when_hot") {
        /* 熱態（超載持續/轉換進行中）→ busy 節拍；平靜後回基礎節拍。 */
        gov::AdvisorPolicy policy = enabled_policy();
        policy.signal_alpha = 0.5;
        policy.eval_interval_s = 60.0;
        policy.eval_interval_busy_s = 20.0;
        gov::AdvisorState state;
        gov::AdvisorSignals sig = calm_signals();
        sig.cpu_load_pct = 96.0; /* 極端：urgent 降 low，ema=96 */
        gov::AdvisorDecision d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.changed && d.target == "low", "overload demote");

        sig.cpu_load_pct = 20.0; /* 機器轉靜，但 ema 仍 58 → 仍熱 */
        sig.now_mono += 25.0;    /* ≥20 忙碌節拍、<60 基礎節拍 */
        d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(d.evaluated && d.busy_cadence,
                 "hot state evaluates at busy cadence");

        /* 連續平靜直到 ema 落回、streak 歸零 → 回復 60s 節拍。 */
        for (int i = 0; i < 8; ++i) {
            sig.now_mono += 30.0; /* ≥busy：若仍熱會被評估 */
            d = gov::evaluate_advisor(policy, sig, state);
        }
        NT_CHECK(state.streak == 0 && state.cpu_ema < policy.strain_cpu_pct,
                 "settled state reached");
        sig.now_mono += 30.0; /* <60 基礎節拍 */
        d = gov::evaluate_advisor(policy, sig, state);
        NT_CHECK(!d.evaluated && !d.busy_cadence,
                 "calm returns to base cadence");
    }
    NT_END_TEST(SUITE, "busy_cadence_evaluates_sooner_when_hot");

    return native_tests::report("resource_governor_advisor_suite.json");
}

/* governor_advisor.cpp — 自動模式顧問控制律實作（純函式，零 IO）。
 *
 * 語義對齊退役 Python ``auto_adjust_mode``（資產文件），加上
 * auto.ceiling 自動模式上限：advisor 評估出的高檔位一律被 clamp 到
 * ceiling，保證使用者前景互動保有整機餘裕。
 */
#include "governor_advisor.h"

#include <algorithm>
#include <cmath>

namespace gptbridge {
namespace governor {
namespace {

int parse_hhmm(const jsonlite::JsonValue* value, int fallback) {
    if (value == nullptr || value->type != jsonlite::JsonValue::Type::String)
        return fallback;
    const std::string& text = value->string;
    const std::size_t colon = text.find(':');
    try {
        const int hour = std::stoi(text.substr(0, colon));
        const int minute =
            colon == std::string::npos ? 0 : std::stoi(text.substr(colon + 1));
        if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return fallback;
        return hour * 60 + minute;
    } catch (...) {
        return fallback;
    }
}

std::string str_or(const jsonlite::JsonValue* value, const char* fallback) {
    if (value != nullptr && value->type == jsonlite::JsonValue::Type::String &&
        !value->string.empty())
        return value->string;
    return fallback;
}

double num_or(const jsonlite::JsonValue* obj, const char* key, double dflt) {
    return obj != nullptr ? json_num_or(obj->get(key), dflt) : dflt;
}

bool bool_or(const jsonlite::JsonValue* obj, const char* key, bool dflt) {
    if (obj == nullptr) return dflt;
    const jsonlite::JsonValue* v = obj->get(key);
    if (v == nullptr) return dflt;
    if (v->type == jsonlite::JsonValue::Type::Bool) return v->boolean;
    return json_num_or(v, dflt ? 1.0 : 0.0) != 0.0;
}

}  // namespace

int mode_rank(std::string_view mode) {
    if (mode == "sleep") return 0;
    if (mode == "low") return 1;
    if (mode == "high") return 3;
    return 2; /* medium 與未知檔位 */
}

bool in_schedule_window(int local_minutes, int start_min, int end_min) {
    if (local_minutes < 0 || start_min == end_min) return false;
    if (start_min < end_min)
        return local_minutes >= start_min && local_minutes < end_min;
    return local_minutes >= start_min || local_minutes < end_min;
}

AdvisorPolicy parse_advisor_policy(const jsonlite::JsonValue* auto_obj,
                                   const jsonlite::JsonValue* schedule_obj,
                                   bool auto_mode,
                                   const std::set<std::string>& valid_modes,
                                   std::string& error) {
    AdvisorPolicy policy;
    policy.enabled = auto_mode;
    if (auto_obj != nullptr &&
        auto_obj->type == jsonlite::JsonValue::Type::Object) {
        policy.ceiling = str_or(auto_obj->get("ceiling"), "medium");
        policy.eval_interval_s =
            std::clamp(num_or(auto_obj, "eval_interval_s", 60.0), 5.0, 3600.0);
        policy.streak_up = std::max(
            1, static_cast<int>(num_or(auto_obj, "streak_up", 2.0)));
        policy.streak_down = std::max(
            1, static_cast<int>(num_or(auto_obj, "streak_down", 3.0)));
        policy.cooldown_s =
            std::clamp(num_or(auto_obj, "cooldown_s", 600.0), 0.0, 86400.0);
        policy.strain_cpu_pct =
            std::clamp(num_or(auto_obj, "strain_cpu_pct", 85.0), 1.0, 100.0);
        policy.strain_mem_pct =
            std::clamp(num_or(auto_obj, "strain_mem_pct", 90.0), 1.0, 100.0);
        policy.headroom_cpu_pct =
            std::clamp(num_or(auto_obj, "headroom_cpu_pct", 60.0), 1.0, 100.0);
        policy.headroom_mem_pct =
            std::clamp(num_or(auto_obj, "headroom_mem_pct", 75.0), 1.0, 100.0);
        policy.demand_factor =
            std::clamp(num_or(auto_obj, "demand_factor", 0.8), 0.05, 1.0);
    }
    /* ceiling 僅在啟用或顯式宣告時驗證——未啟用且未宣告的預設值不應
     * 讓不含該檔位的 rules 檔報錯（fail-open 於停用態，fail-closed
     * 於啟用態）。 */
    const bool ceiling_declared =
        auto_obj != nullptr &&
        auto_obj->type == jsonlite::JsonValue::Type::Object &&
        auto_obj->get("ceiling") != nullptr;
    if (!valid_modes.empty() && (policy.enabled || ceiling_declared) &&
        valid_modes.count(policy.ceiling) == 0) {
        error = "auto: unknown ceiling '" + policy.ceiling + "'";
        return policy;
    }
    if (schedule_obj != nullptr &&
        schedule_obj->type == jsonlite::JsonValue::Type::Object) {
        policy.schedule_enabled =
            bool_or(schedule_obj, "enabled", policy.schedule_enabled);
        policy.schedule_start =
            str_or(schedule_obj->get("start"), "22:00");
        policy.schedule_end = str_or(schedule_obj->get("end"), "07:00");
        policy.schedule_mode =
            str_or(schedule_obj->get("mode"), "sleep");
        policy.schedule_start_min =
            parse_hhmm(schedule_obj->get("start"), policy.schedule_start_min);
        policy.schedule_end_min =
            parse_hhmm(schedule_obj->get("end"), policy.schedule_end_min);
    }
    const bool schedule_declared =
        schedule_obj != nullptr &&
        schedule_obj->type == jsonlite::JsonValue::Type::Object &&
        schedule_obj->get("mode") != nullptr;
    if (!valid_modes.empty() && policy.schedule_enabled &&
        (policy.enabled || schedule_declared) &&
        valid_modes.count(policy.schedule_mode) == 0) {
        error = "power_saving_schedule: unknown mode '" +
                policy.schedule_mode + "'";
    }
    return policy;
}

AdvisorDecision evaluate_advisor(const AdvisorPolicy& policy,
                                 const AdvisorSignals& sig,
                                 AdvisorState& state) {
    AdvisorDecision out;
    out.current =
        state.applied_mode.empty() ? sig.configured_mode : state.applied_mode;
    if (!policy.enabled) {
        state.applied_mode.clear();
        state.streak = 0;
        state.last_target.clear();
        out.evaluated = true; /* 記錄 enabled=false（同 Python 每 tick 落盤） */
        out.reason = "auto-disabled";
        return out;
    }
    if (sig.rules_error) {
        out.evaluated = true;
        out.reason = "rules-error";
        return out;
    }
    /* 評估節拍：預設 60s（與退役 Python 一致）；未到期不動狀態。 */
    if (state.last_eval_mono >= 0.0 &&
        sig.now_mono - state.last_eval_mono < policy.eval_interval_s) {
        out.target = out.current;
        out.reason = "cadence";
        return out;
    }
    state.last_eval_mono = sig.now_mono;
    out.evaluated = true;

    out.demand =
        sig.admission_hold || sig.reg_pre || sig.reg_active ||
        (sig.budget_cpu_pct > 0 &&
         sig.worker_cpu_pct >= sig.budget_cpu_pct * policy.demand_factor) ||
        (sig.budget_ram_pct > 0 &&
         sig.worker_ram_pct >= sig.budget_ram_pct * policy.demand_factor);
    out.headroom = sig.cpu_load_pct < policy.headroom_cpu_pct &&
                   sig.mem_used_pct < policy.headroom_mem_pct;
    out.schedule_active =
        policy.schedule_enabled &&
        in_schedule_window(sig.local_minutes, policy.schedule_start_min,
                           policy.schedule_end_min);

    std::string reason;
    if (out.schedule_active) {
        out.target = policy.schedule_mode;
        out.urgent = true;
        reason = "night-power-saving (" + policy.schedule_start + "-" +
                 policy.schedule_end + ")";
    } else if (sig.strained || sig.cpu_load_pct >= policy.strain_cpu_pct ||
               sig.mem_used_pct >= policy.strain_mem_pct) {
        out.target = "low";
        out.urgent = true;
        reason = sig.strained ? "strained" : "machine-overload";
    } else if (out.demand && out.headroom) {
        out.target = "high";
        reason = "worker-demand";
    } else if (out.demand) {
        out.target = "medium";
        reason = "demand-no-headroom";
    } else {
        out.target = "medium";
        reason = "baseline";
    }
    /* 使用者可用性上限：自動模式永不升過 ceiling（降檔/省電不受限）。 */
    if (mode_rank(out.target) > mode_rank(policy.ceiling)) {
        out.target = policy.ceiling;
        reason += " (ceiling " + policy.ceiling + ")";
    }

    state.streak = state.last_target == out.target ? state.streak + 1 : 1;
    state.last_target = out.target;
    out.streak = state.streak;

    if (out.target == out.current) {
        out.reason = reason + " (already " + out.target + ")";
        state.streak = 0;
        out.streak = 0;
        return out;
    }
    if (sig.valid_modes.count(out.target) == 0) {
        out.reason = reason + " (mode-not-defined " + out.target + ")";
        return out;
    }
    const bool upgrade = mode_rank(out.target) > mode_rank(out.current);
    if (!out.urgent) {
        if (upgrade && state.streak < policy.streak_up) {
            out.reason = reason + " (streak " + std::to_string(state.streak) +
                         "/" + std::to_string(policy.streak_up) + ")";
            return out;
        }
        if (!upgrade && state.streak < policy.streak_down) {
            out.reason = reason + " (streak " + std::to_string(state.streak) +
                         "/" + std::to_string(policy.streak_down) + ")";
            return out;
        }
        if (!upgrade && state.last_switch_unix > 0.0 &&
            sig.now_unix - state.last_switch_unix < policy.cooldown_s) {
            out.reason = reason + " (cooldown)";
            return out;
        }
    }
    out.changed = true;
    out.reason = reason + " (applied)";
    state.applied_mode = out.target;
    state.last_switch_unix = sig.now_unix;
    return out;
}

}  // namespace governor
}  // namespace gptbridge

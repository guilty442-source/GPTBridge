/* governor_rules.h — 行程平面歸因、Rules 檔解析與組態解析。
 *
 * 由 resource_governor.h 依 A185 拆分而來；內容原樣平移，語義不變：
 *  - Plane/classify_plane：§10.64 worker-plane 歸因。
 *  - ProgramRule/RulesDoc/parse_rules：Process Lasso 式常駐規則；
 *    所有可失敗解析以 expected<T, std::string> 回傳，fail-closed。
 *  - GovernorConfig/Features/Thresholds 與 resolve_*：CLI > defaults >
 *    modes preset > 常數 的優先序與 Python 一致。
 */
#pragma once

#include <algorithm>
#include <cmath>
#include <expected>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "governor_json_utils.h"

namespace gptbridge {
namespace governor {

/* ------------------------------------------------------------------ */
/* 行程平面（§10.64 worker-plane 歸因）                                 */
/* ------------------------------------------------------------------ */
enum class Plane { Governance, Toolbox, Worker, RepoOther, External };

inline std::string plane_name(Plane plane) {
    switch (plane) {
        case Plane::Governance: return "governance";
        case Plane::Toolbox: return "toolbox";
        case Plane::Worker: return "worker";
        case Plane::RepoOther: return "repo-other";
        case Plane::External: return "external";
    }
    return "external";
}

inline bool is_worker_plane(Plane plane) {
    return plane == Plane::Worker || plane == Plane::Toolbox ||
           plane == Plane::RepoOther;
}

/* 輸入皆須為小寫（呼叫端以 to_lower 預處理）。 */
inline Plane classify_plane(std::string_view exe_lower, std::string_view cmdline_lower,
                            std::string_view root_lower) {
    const bool in_exe = exe_lower.find(root_lower) != std::string_view::npos;
    const bool in_cmd = cmdline_lower.find(root_lower) != std::string_view::npos;
    const bool keyword = cmdline_lower.find("gptbridge") != std::string_view::npos;
    if (!in_exe && !in_cmd && !keyword) return Plane::External;
    std::string joined;
    joined.reserve(exe_lower.size() + cmdline_lower.size() + 1);
    joined.append(exe_lower);
    joined.push_back(' ');
    joined.append(cmdline_lower);
    const std::string_view view(joined);
    if (view.find("--serve") != std::string_view::npos ||
        view.find("boot_core") != std::string_view::npos ||
        view.find("governance_rule") != std::string_view::npos)
        return Plane::Governance;
    if (view.find("standalone tools") != std::string_view::npos ||
        view.find("standalone_tools") != std::string_view::npos)
        return Plane::Toolbox;
    if (view.find("scripts") != std::string_view::npos ||
        view.find(".worktrees") != std::string_view::npos ||
        view.find(".kilo") != std::string_view::npos ||
        view.find("pytest") != std::string_view::npos)
        return Plane::Worker;
    return Plane::RepoOther;
}

/* ------------------------------------------------------------------ */
/* Rules 檔（Process Lasso 式常駐規則）                                 */
/* ------------------------------------------------------------------ */
struct ProgramRule {
    bool exclude = false;
    std::optional<int> priority_class;
    std::optional<std::vector<int>> affinity;
    double cpu_limit_percent = 0.0;
    bool background = false;
    bool ecoqos = false;
};

struct RulesDoc {
    std::map<std::string, jsonlite::JsonValue> defaults;
    std::map<std::string, ProgramRule> programs;
    std::string error;
    std::string mode;
    bool has_mode = false;
};

inline std::optional<int> parse_priority_name(std::string_view text) {
    const std::string lowered = to_lower(text);
    if (lowered == "normal") return kPriorityNormal;
    if (lowered == "below_normal") return kPriorityBelowNormal;
    if (lowered == "idle") return kPriorityIdle;
    return std::nullopt;
}

/* CPU limiter 百分比 → Job Object CpuRate（1..10000，與 Python 一致）。 */
inline int cpu_rate_value(double percent) {
    const long rounded = std::lround(percent * 100.0);
    return static_cast<int>(std::clamp<long>(rounded, 1, 10000));
}

/* ------------------------------------------------------------------ */
/* 組態 / Features / Thresholds                                        */
/* ------------------------------------------------------------------ */
struct GovernorConfig {
    double interval = kDefaultInterval;
    std::optional<double> cpu_busy;
    std::optional<double> cpu_extreme;
    double calm = kCpuCalmPct;
    std::optional<int> sustain;
    int calm_samples = kCalmSamples;
    std::optional<double> mem_trim_mb;
    double trim_cooldown = kTrimCooldownSeconds;
    bool affinity = true;
    bool dry_run = false;
    bool log_samples = false;
    std::string rules_path;
    std::optional<bool> probalance;
    std::optional<bool> cpu_limiter;
    std::optional<bool> background_mode;
    std::optional<bool> ecoqos;
    std::optional<double> limiter_percent;
    std::optional<double> resp_ratio;
    std::optional<bool> worker_job_cap;
    std::optional<double> worker_job_percent;
};

struct Features {
    bool probalance = false;
    bool cpu_limiter = false;
    bool background_mode = false;
    bool ecoqos = false;
    double limiter_percent = kDefaultLimiterPercent;
    bool worker_job_cap = false;
    double worker_job_percent = kWorkerCpuBudgetPct;
    long long worker_job_memory_bytes = 0;
    int worker_job_process_limit = 96;
    double resp_ratio = kRespStrainRatio;
};

struct Thresholds {
    double cpu_busy = kCpuBusyPct;
    double cpu_extreme = kCpuExtremePct;
    int sustain = kSustainSamples;
    int extreme_sustain = kExtremeSamples;
    double mem_trim_mb = kMemTrimMb;
    bool affinity = true;
    double worker_cpu_budget = kWorkerCpuBudgetPct;
    double worker_ram_budget = kWorkerRamBudgetPct;
};

inline bool feature_enabled(std::optional<bool> flag,
                            const std::map<std::string, jsonlite::JsonValue>& defaults,
                            std::string_view key) {
    if (flag.has_value()) return *flag;
    auto it = defaults.find(std::string(key));
    return it != defaults.end() && json_is_true(&it->second);
}

std::expected<RulesDoc, std::string> parse_rules(std::string_view text);
Features resolve_features(
    const GovernorConfig& config,
    const std::map<std::string, jsonlite::JsonValue>& defaults,
    long long total_ram_bytes);
Thresholds resolve_thresholds(
    const GovernorConfig& config,
    const std::map<std::string, jsonlite::JsonValue>& defaults);

}  // namespace governor
}  // namespace gptbridge

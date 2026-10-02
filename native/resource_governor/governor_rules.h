/* governor_rules.h — 行程平面歸因、Rules 檔解析與組態解析。
 *
 * 由 resource_governor.h 依 A185 拆分而來：
 *  - Plane/classify_plane：§10.64 worker-plane 歸因。
 *  - ProgramRule/RulesDoc/parse_rules：Process Lasso 式常駐規則；
 *    所有可失敗解析以 expected<T, std::string> 回傳，fail-closed；
 *    2026-10-01 起另解析 auto_mode/auto/power_saving_schedule
 *    （顧問政策）並保留 modes presets 供有效模式重組。
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

#include "governor_advisor.h"
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
/* 資源池（Pool）：Plane 之外的第二歸因軸。                             */
/* 池由規則檔宣告（"pools" 區塊），成員歸屬由 program rule 的           */
/* "pool" 鍵顯式指定，或由 pools.<name>.members 子串表比對              */
/* exe 基名／cmdline。未歸屬行程 → Pool::None（不進池，無池約束）。     */
/* 治理平面永不進池（保護語義優先）。                                   */
/* ------------------------------------------------------------------ */
enum class Pool { None, Interactive, Compute, Io, Background };

inline std::string pool_name(Pool pool) {
    switch (pool) {
        case Pool::Interactive: return "interactive";
        case Pool::Compute: return "compute";
        case Pool::Io: return "io";
        case Pool::Background: return "background";
        case Pool::None: return "none";
    }
    return "none";
}

inline std::optional<Pool> pool_from_name(std::string_view text) {
    const std::string lowered = to_lower(text);
    if (lowered == "interactive") return Pool::Interactive;
    if (lowered == "compute") return Pool::Compute;
    if (lowered == "io") return Pool::Io;
    if (lowered == "background") return Pool::Background;
    return std::nullopt;
}

/* 各池信封（共享 Job Object 硬上限＋一次性靜態屬性；皆為有界值）。 */
struct PoolPolicy {
    bool enabled = true;
    double cpu_limit_percent = 0.0;   /* 0 = 不設 CPU 上限 */
    double memory_percent = 0.0;      /* 0 = 不設記憶體上限 */
    long long memory_mb = 0;
    int process_limit = 0;
    std::optional<int> priority_class;
    bool background = false;
    bool ecoqos = false;
    std::vector<std::string> members; /* 已轉小寫子串 */
};

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
    std::optional<Pool> pool;
};

struct RulesDoc {
    std::map<std::string, jsonlite::JsonValue> defaults;
    std::map<std::string, ProgramRule> programs;
    std::map<Pool, PoolPolicy> pools;
    bool pools_enabled = false;
    std::string error;
    std::string mode;
    bool has_mode = false;
    /* 自動模式顧問（governor_advisor.*）：raw presets＋顯式 defaults
     * 供有效模式重組；advisor 為已解析政策（含 ceiling 上限）。 */
    std::map<std::string, std::map<std::string, jsonlite::JsonValue>> modes;
    std::map<std::string, jsonlite::JsonValue> explicit_defaults;
    bool auto_mode = false;
    AdvisorPolicy advisor;

    /* 指定模式的合成 defaults（preset 併入 explicit defaults 覆寫；
     * 未知/無 modes 時回退已解析的 defaults）。 */
    std::map<std::string, jsonlite::JsonValue> defaults_for(
        const std::string& mode) const {
        if (mode == this->mode || modes.empty()) return defaults;
        std::map<std::string, jsonlite::JsonValue> merged;
        auto it = modes.find(mode);
        if (it == modes.end()) return defaults;
        merged = it->second;
        for (const auto& [key, value] : explicit_defaults)
            merged[key] = value;
        return merged;
    }
};

/* 池歸因：program rule 顯式 pool > pools.members 子串比對 > None。
 * 輸入皆須為小寫；Governance 平面永不進池。 */
Pool classify_pool(std::string_view name_lower, std::string_view exe_lower,
                   std::string_view cmdline_lower, Plane plane,
                   const RulesDoc& rules, const ProgramRule* rule);

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
    /* 動態升降：limiter_dynamic 啟用時每週期按需求重算 Job 比率
     * （pressed→收緊 limiter_step 至 limiter_min；slack→放寬回
     * limiter_percent）；priority_escalate 啟用時 busy 持續超過
     * sustain+extreme_sustain 且當下仍 extreme 的行程由
     * below_normal 再降 idle，跌回 extreme 以下先回 below_normal。 */
    bool limiter_dynamic = false;
    double limiter_min_percent = kLimiterMinDynamicPct;
    double limiter_step_percent = kLimiterStepPct;
    bool priority_escalate = false;
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

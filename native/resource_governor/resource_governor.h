/* resource_governor.h — C++23 資源管制器控制律核心（平台無關部分）。
 *
 * 由 scripts/resource-governor.py（Python）完整遷移而來，對應法典 A608
 * RULE_RESOURCE_GOVERNOR_CPP23_V1：language_id=cpp23，memory_strategy=raii。
 *
 * 設計要點（C++23）：
 *  - RAII：所有控制狀態皆為值語義 struct；OS 句柄由 .cpp 的 Handle 守衛持有，
 *    本標頭不擁有任何裸資源，析構具確定性。
 *  - std::expected：所有可失敗的解析（rules JSON、數值萃取）以
 *    expected<T, std::string> 回傳，fail-closed（錯誤 → 僅監控模式）。
 *  - std::span：非擁有的行程/動作視圖以 span 傳遞，零拷貝。
 *  - 本標頭僅依賴標準庫 + 共用 jsonlite.h（無第三方依賴）。
 *
 * JSON 狀態契約（main-system/runtime/state/resource-governor.json 與
 * resource-governor.jsonl）與 Python 版逐鍵相容，後端
 * tasks/resource_governor_signal.py 無需任何修改即可繼續讀取。
 */
#pragma once

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <expected>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <span>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "jsonlite.h"

namespace gptbridge {
namespace governor {

/* ------------------------------------------------------------------ */
/* 常數（與 Python 版 Final 值一致）                                    */
/* ------------------------------------------------------------------ */
inline constexpr double kDefaultInterval = 20.0;
inline constexpr double kCpuBusyPct = 10.0;
inline constexpr double kCpuExtremePct = 20.0;
inline constexpr double kCpuCalmPct = 5.0;
inline constexpr int kSustainSamples = 3;
inline constexpr int kExtremeSamples = 6;
inline constexpr int kCalmSamples = 15;
inline constexpr double kMemTrimMb = 1500.0;
inline constexpr double kGlobalCpuLimitPct = 10.0;
inline constexpr double kGlobalRamLimitPct = 30.0;
inline constexpr double kTrimCooldownSeconds = 300.0;
inline constexpr int kAffinityMinCpus = 1;

inline constexpr double kWorkerCpuBudgetPct = 10.0;
inline constexpr double kWorkerRamBudgetPct = 30.0;
inline constexpr int kRegulateOverSamples = 1;
inline constexpr int kRegulateUnderSamples = 5;
inline constexpr double kRegulateUnderFactor = 0.8;
inline constexpr double kRegulatedWorkerBusyPct = 2.0;

inline constexpr const char* kGovernorDisableEnv = "GPTBRIDGE_GOVERNOR_DISABLE";

inline constexpr int kRespProbeIters = 200000;
inline constexpr int kRespProbeRuns = 3;
inline constexpr double kRespBaselineAlpha = 0.2;
inline constexpr double kRespStrainRatio = 1.8;
inline constexpr double kRespReleaseRatio = 1.2;
inline constexpr int kRespStrainSamples = 2;
inline constexpr int kRespCalmSamples = 3;
inline constexpr int kProbBalanceMaxDemotions = 5;
inline constexpr double kDefaultLimiterPercent = 10.0;
inline constexpr double kLimiterMinPercent = 1.0;
inline constexpr double kLimiterMaxPercent = 100.0;

/* Win32 priority class 數值（跨平台標頭內僅作代碼傳遞，實際呼叫在 .cpp）。 */
inline constexpr int kPriorityNormal = 0x20;
inline constexpr int kPriorityBelowNormal = 0x4000;
inline constexpr int kPriorityIdle = 0x40;

/* ------------------------------------------------------------------ */
/* 小工具                                                              */
/* ------------------------------------------------------------------ */
inline std::string to_lower(std::string_view text) {
    std::string out(text);
    std::transform(out.begin(), out.end(), out.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return out;
}

inline double round1(double value) { return std::round(value * 10.0) / 10.0; }
inline double round2(double value) { return std::round(value * 100.0) / 100.0; }

/* 路徑基名（同時處理 '\\' 與 '/'）。 */
inline std::string base_name(std::string_view path) {
    const std::size_t pos = path.find_last_of("\\/");
    return std::string(pos == std::string_view::npos ? path : path.substr(pos + 1));
}

/* 僅 Bool 型別視為功能開關（與 Python _feature_enabled 一致：非 bool → false）。 */
inline bool json_is_true(const jsonlite::JsonValue* value) {
    return value != nullptr && value->type == jsonlite::JsonValue::Type::Bool &&
           value->boolean;
}

/* 數值萃取（與 Python _num_default 一致：Number/Bool/可解析字串，否則 fallback）。 */
inline double json_num_or(const jsonlite::JsonValue* value, double fallback) {
    if (value == nullptr) return fallback;
    using T = jsonlite::JsonValue::Type;
    if (value->type == T::Number) return value->number;
    if (value->type == T::Bool) return value->boolean ? 1.0 : 0.0;
    if (value->type == T::String) {
        try {
            std::size_t used = 0;
            const double parsed = std::stod(value->string, &used);
            if (used > 0) return parsed;
        } catch (...) {
        }
    }
    return fallback;
}

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

/* 解析 rules JSON 文字；fail-closed：任何異形回傳 error（defaults/programs 為空）。 */
inline std::expected<RulesDoc, std::string> parse_rules(std::string_view text) {
    using jsonlite::JsonParser;
    using jsonlite::JsonValue;
    using T = JsonValue::Type;
    RulesDoc doc;
    JsonValue root;
    try {
        root = JsonParser(std::string(text)).parse();
    } catch (const jsonlite::JsonError&) {
        doc.error = "JsonError: invalid JSON";
        return doc;
    }
    if (root.type != T::Object) {
        doc.error = "root must be an object";
        return doc;
    }
    if (const JsonValue* raw_defaults = root.get("defaults");
        raw_defaults != nullptr && raw_defaults->type == T::Object) {
        for (const auto& [key, value] : raw_defaults->object)
            doc.defaults.emplace(key, value);
    }
    if (const JsonValue* raw_modes = root.get("modes");
        raw_modes != nullptr && raw_modes->type == T::Object) {
        std::string raw_mode = "medium";
        if (const JsonValue* mode_value = root.get("mode");
            mode_value != nullptr && mode_value->type == T::String &&
            !mode_value->string.empty()) {
            raw_mode = mode_value->string;
        }
        const JsonValue* preset = nullptr;
        if (const JsonValue* found = raw_modes->get(raw_mode); found != nullptr &&
            found->type == T::Object) {
            preset = found;
        }
        if (preset == nullptr) {
            doc.error = "unknown mode '" + raw_mode + "'";
        } else {
            doc.mode = raw_mode;
            doc.has_mode = true;
            for (const auto& [key, value] : preset->object)
                doc.defaults.emplace(key, value);
            /* 顯式 defaults 鍵覆寫 preset（Python {**preset, **defaults}）。 */
            if (const JsonValue* raw_explicit = root.get("defaults");
                raw_explicit != nullptr && raw_explicit->type == T::Object) {
                for (const auto& [key, value] : raw_explicit->object)
                    doc.defaults[key] = value;
            }
        }
    }
    if (const JsonValue* raw_programs = root.get("programs");
        raw_programs != nullptr && raw_programs->type == T::Object) {
        for (const auto& [key, entry] : raw_programs->object) {
            if (key.empty() || entry.type != T::Object) continue;
            ProgramRule rule;
            try {
                rule.exclude = json_is_true(entry.get("exclude"));
                if (const JsonValue* prio = entry.get("priority");
                    prio != nullptr && prio->type == T::String &&
                    !prio->string.empty()) {
                    rule.priority_class = parse_priority_name(prio->string);
                }
                if (const JsonValue* aff = entry.get("affinity");
                    aff != nullptr && aff->type == T::Array && !aff->array.empty()) {
                    std::vector<int> cpus;
                    bool valid = true;
                    for (const JsonValue& item : aff->array) {
                        if (item.type != T::Number || item.number < 0 ||
                            std::floor(item.number) != item.number) {
                            valid = false;
                            break;
                        }
                        cpus.push_back(static_cast<int>(item.number));
                    }
                    if (valid) rule.affinity = std::move(cpus);
                }
                if (const JsonValue* limit = entry.get("cpu_limit_percent");
                    limit != nullptr && limit->type == T::Number &&
                    limit->number > 0) {
                    rule.cpu_limit_percent = std::clamp(limit->number,
                                                        kLimiterMinPercent,
                                                        kLimiterMaxPercent);
                }
                rule.background = json_is_true(entry.get("background"));
                rule.ecoqos = json_is_true(entry.get("ecoqos"));
            } catch (...) {
                doc.error = key + ": invalid rule entry";
                return doc;
            }
            doc.programs.emplace(to_lower(key), std::move(rule));
        }
    }
    return doc;
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

inline Features resolve_features(
    const GovernorConfig& config,
    const std::map<std::string, jsonlite::JsonValue>& defaults,
    long long total_ram_bytes) {
    Features out;
    auto find = [&](std::string_view key) -> const jsonlite::JsonValue* {
        auto it = defaults.find(std::string(key));
        return it != defaults.end() ? &it->second : nullptr;
    };
    out.probalance = feature_enabled(config.probalance, defaults, "probalance");
    out.cpu_limiter = feature_enabled(config.cpu_limiter, defaults, "cpu_limiter");
    out.background_mode =
        feature_enabled(config.background_mode, defaults, "background_mode");
    out.ecoqos = feature_enabled(config.ecoqos, defaults, "ecoqos");
    const double limiter = config.limiter_percent.has_value()
                               ? *config.limiter_percent
                               : json_num_or(find("limiter_percent"),
                                             kDefaultLimiterPercent);
    out.limiter_percent = std::clamp(limiter, kLimiterMinPercent, kLimiterMaxPercent);
    out.worker_job_cap =
        feature_enabled(config.worker_job_cap, defaults, "worker_job_cap");
    const double job_percent = config.worker_job_percent.has_value()
                                   ? *config.worker_job_percent
                                   : json_num_or(find("worker_job_percent"),
                                                 kWorkerCpuBudgetPct);
    out.worker_job_percent = std::clamp(job_percent, 1.0, 100.0);
    /* worker_job_memory_mb 優先，否則 worker_job_memory_percent（預設 30）。 */
    if (const jsonlite::JsonValue* mb = find("worker_job_memory_mb");
        mb != nullptr && mb->type == jsonlite::JsonValue::Type::Number &&
        mb->number > 0) {
        out.worker_job_memory_bytes =
            static_cast<long long>(mb->number) * 1024LL * 1024LL;
    } else {
        const double pct = json_num_or(find("worker_job_memory_percent"), 30.0);
        if (pct > 0 && total_ram_bytes > 0) {
            out.worker_job_memory_bytes = static_cast<long long>(
                static_cast<double>(total_ram_bytes) * std::min(pct, 100.0) / 100.0);
        }
    }
    const double proc_limit =
        json_num_or(find("worker_job_process_limit"), 96.0);
    out.worker_job_process_limit =
        proc_limit > 0 ? static_cast<int>(proc_limit) : 0;
    const double ratio = config.resp_ratio.has_value()
                             ? *config.resp_ratio
                             : json_num_or(find("resp_strain_ratio"), kRespStrainRatio);
    out.resp_ratio = std::max(1.05, ratio);
    return out;
}

inline Thresholds resolve_thresholds(
    const GovernorConfig& config,
    const std::map<std::string, jsonlite::JsonValue>& defaults) {
    Thresholds out;
    auto find = [&](std::string_view key) -> const jsonlite::JsonValue* {
        auto it = defaults.find(std::string(key));
        return it != defaults.end() ? &it->second : nullptr;
    };
    out.cpu_busy = config.cpu_busy.has_value()
                       ? *config.cpu_busy
                       : json_num_or(find("cpu_busy"), kCpuBusyPct);
    out.cpu_extreme = config.cpu_extreme.has_value()
                          ? *config.cpu_extreme
                          : json_num_or(find("cpu_extreme"), kCpuExtremePct);
    const double sustain_raw = config.sustain.has_value()
                                   ? static_cast<double>(*config.sustain)
                                   : json_num_or(find("sustain"),
                                                 static_cast<double>(kSustainSamples));
    out.sustain = static_cast<int>(std::max(1.0, sustain_raw));
    const double extreme_default =
        std::max(static_cast<double>(out.sustain) * 2.0,
                 static_cast<double>(kExtremeSamples));
    out.extreme_sustain =
        std::max(1, static_cast<int>(json_num_or(find("extreme_sustain"),
                                                extreme_default)));
    out.mem_trim_mb = config.mem_trim_mb.has_value()
                          ? *config.mem_trim_mb
                          : json_num_or(find("mem_trim_mb"), kMemTrimMb);
    const jsonlite::JsonValue* affinity_value = find("affinity");
    bool rules_affinity = true;
    if (affinity_value != nullptr) {
        /* 與 Python bool(defaults.get("affinity", True)) 一致的真值語義。 */
        using T = jsonlite::JsonValue::Type;
        switch (affinity_value->type) {
            case T::Null: rules_affinity = false; break;
            case T::Bool: rules_affinity = affinity_value->boolean; break;
            case T::Number: rules_affinity = affinity_value->number != 0.0; break;
            case T::String: rules_affinity = !affinity_value->string.empty(); break;
            case T::Array: rules_affinity = !affinity_value->array.empty(); break;
            case T::Object: rules_affinity = !affinity_value->object.empty(); break;
        }
    }
    out.affinity = config.affinity && rules_affinity;
    out.worker_cpu_budget =
        std::max(1.0, json_num_or(find("worker_cpu_budget"), kWorkerCpuBudgetPct));
    out.worker_ram_budget =
        std::max(1.0, json_num_or(find("worker_ram_budget"), kWorkerRamBudgetPct));
    return out;
}

/* ------------------------------------------------------------------ */
/* 調節狀態（hysteresis 控制律 §10.64）                                 */
/* ------------------------------------------------------------------ */
struct RegState {
    int over = 0;
    int under = 0;
    bool active = false;
    bool pre = false;
    double resp_baseline = 0.0;
    bool has_baseline = false;
    int strain_hits = 0;
    int calm_hits = 0;
    bool strained = false;
    double resp_ratio_out = 1.0;
    std::string rules_error;
};

enum class RegEvent { PreEntered, Entered, Released, PreReleased };

struct RegUpdate {
    std::vector<RegEvent> events;
    bool over_budget = false;
    bool under_budget = false;
};

/* 回應探針滯後偵測（與 Python _responsiveness_update 一致）。 */
inline bool responsiveness_update(RegState& state, double latency_ms, double ratio) {
    if (!state.has_baseline || state.resp_baseline <= 0) {
        state.resp_baseline = latency_ms;
        state.strain_hits = 0;
        state.calm_hits = 0;
        state.strained = false;
        state.resp_ratio_out = 1.0;
        return false;
    }
    const double current_ratio =
        state.resp_baseline > 0 ? latency_ms / state.resp_baseline : 1.0;
    state.resp_ratio_out = std::round(current_ratio * 1000.0) / 1000.0;
    if (current_ratio >= ratio) {
        state.strain_hits += 1;
        state.calm_hits = 0;
    } else if (current_ratio <= kRespReleaseRatio) {
        state.calm_hits += 1;
        state.strain_hits = 0;
        if (!state.strained) {
            state.resp_baseline = (1.0 - kRespBaselineAlpha) * state.resp_baseline +
                                  kRespBaselineAlpha * latency_ms;
        }
    } else {
        state.strain_hits = 0;
        state.calm_hits = 0;
    }
    if (!state.strained && state.strain_hits >= kRespStrainSamples)
        state.strained = true;
    else if (state.strained && state.calm_hits >= kRespCalmSamples)
        state.strained = false;
    return state.strained;
}

/* worker 聚合預算控制律（strict INT-10 語義：首次超標即調節）。 */
inline RegUpdate regulation_update(RegState& state, double worker_cpu_machine,
                                   double worker_ram_pct, double budget_cpu,
                                   double budget_ram) {
    RegUpdate out;
    out.over_budget =
        worker_cpu_machine > budget_cpu || worker_ram_pct > budget_ram;
    out.under_budget = worker_cpu_machine <= budget_cpu * kRegulateUnderFactor &&
                       worker_ram_pct <= budget_ram * kRegulateUnderFactor;
    if (out.over_budget) {
        state.over += 1;
        state.under = 0;
    } else if (out.under_budget) {
        state.under += 1;
        state.over = 0;
    } else {
        state.over = 0;
        state.under = 0;
    }
    if (!state.pre && !out.under_budget) {
        state.pre = true;
        out.events.push_back(RegEvent::PreEntered);
    }
    if (!state.active && state.over >= kRegulateOverSamples) {
        state.active = true;
        out.events.push_back(RegEvent::Entered);
    } else if (state.active && state.under >= kRegulateUnderSamples) {
        state.active = false;
        out.events.push_back(RegEvent::Released);
    }
    if (state.pre && state.under >= kRegulateUnderSamples) {
        state.pre = false;
        out.events.push_back(RegEvent::PreReleased);
    }
    return out;
}

/* ------------------------------------------------------------------ */
/* 引擎介面（列舉 / 系統 / 動作）— 測試以假實作注入，零 OS 依賴        */
/* ------------------------------------------------------------------ */
struct ProcKey {
    int pid = 0;
    std::int64_t create_ms = 0;
    bool operator<(const ProcKey& other) const {
        if (pid != other.pid) return pid < other.pid;
        return create_ms < other.create_ms;
    }
    bool operator==(const ProcKey& other) const {
        return pid == other.pid && create_ms == other.create_ms;
    }
};

struct ProcSample {
    int pid = 0;
    std::string name;
    std::string exe;
    std::string cmdline;
    std::string username;
    double cpu_percore = 0.0;
    double rss_mb = 0.0;
    double io_read_mb = 0.0;
    double io_write_mb = 0.0;
    std::int64_t create_ms = 0;
};

struct SysInfo {
    double cpu_load_machine = 0.0;
    double mem_used_pct = 0.0;
    double mem_avail_mb = 0.0;
    double total_ram_mb = 0.0;
    long long total_ram_bytes = 0;
    int logical = 1;
};

class IEngine {
 public:
    virtual ~IEngine() = default;
    virtual std::vector<ProcSample> enumerate() = 0;
    virtual SysInfo system() = 0;
    virtual bool set_priority(int pid, int prio_class) = 0;
    virtual std::expected<std::vector<int>, std::string> get_affinity(int pid) = 0;
    virtual bool set_affinity(int pid, std::span<const int> cpus) = 0;
    virtual bool trim(int pid) = 0;
    virtual bool background(int pid, bool enable) = 0;
    virtual bool ecoqos(int pid, bool enable) = 0;
    virtual bool cpu_limit(const ProcKey& key, int pid, double percent,
                           long long job_memory_bytes, int job_process_limit) = 0;
    virtual bool cpu_limit_clear(const ProcKey& key) = 0;
    virtual int foreground_pid() = 0;
    virtual double responsiveness() = 0;
};

/* ------------------------------------------------------------------ */
/* 逐行程紀錄                                                          */
/* ------------------------------------------------------------------ */
struct ProcessRecord {
    int busy = 0;
    int calm = 0;
    bool prio_set = false;
    bool aff_set = false;
    bool reg_aff_set = false;
    double last_trim_mono = 0.0;
    bool pb_set = false;
    bool bg_set = false;
    bool eco_set = false;
    bool limit_set = false;
    bool rule_applied = false;
    std::set<std::string> rule_hold;
    std::optional<int> rule_priority;
    bool rule_aff_set = false;
    bool job_member = false;
};

using RecordMap = std::map<ProcKey, ProcessRecord>;

/* ------------------------------------------------------------------ */
/* 快照（與 Python snapshot 逐鍵相容）                                  */
/* ------------------------------------------------------------------ */
struct ProcRow {
    int pid = 0;
    std::string name;
    double cpu = 0.0;
    double mem_mb = 0.0;
    std::string plane;
    double io_read_mb = 0.0;
    double io_write_mb = 0.0;
    std::vector<std::string> flags;
};

struct Snapshot {
    double interval = kDefaultInterval;
    std::string mode;
    bool has_mode = false;
    std::size_t processes = 0;
    std::size_t tracked = 0;
    double cpu_load_pct = 0.0;
    double mem_used_pct = 0.0;
    double mem_avail_mb = 0.0;
    double res_cpu_pct = kGlobalCpuLimitPct;
    double res_ram_pct = kGlobalRamLimitPct;
    bool res_cpu_over = false;
    bool res_ram_over = false;
    double worker_cpu_pct = 0.0;
    double worker_ram_mb = 0.0;
    double worker_ram_pct = 0.0;
    double budget_cpu_pct = kWorkerCpuBudgetPct;
    double budget_ram_pct = kWorkerRamBudgetPct;
    bool over_budget = false;
    std::map<std::string, int> planes;
    bool reg_active = false;
    bool reg_pre = false;
    int reg_over = 0;
    int reg_under = 0;
    bool reg_strained = false;
    bool resp_enabled = false;
    double resp_latency_ms = 0.0;
    double resp_baseline_ms = 0.0;
    bool has_resp_baseline = false;
    double resp_ratio = 1.0;
    bool resp_strained = false;
    int resp_strain_samples = 0;
    int resp_calm_samples = 0;
    bool pb_enabled = false;
    bool pb_strained = false;
    int pb_demoted = 0;
    int pb_max = kProbBalanceMaxDemotions;
    Features features;
    Thresholds thresholds;
    std::string rules_path;
    bool rules_loaded = false;
    std::string rules_error;
    bool worker_admission_hold = false;
    std::vector<jsonlite::JsonValue> actions;
    std::vector<ProcRow> top_cpu;
    std::vector<ProcRow> top_mem;
    bool dry_run = false;
    bool disabled = false;
};

namespace detail {
inline jsonlite::JsonValue jstr(std::string_view value) {
    jsonlite::JsonValue out;
    out.type = jsonlite::JsonValue::Type::String;
    out.string = std::string(value);
    return out;
}
inline jsonlite::JsonValue jnum(double value) {
    jsonlite::JsonValue out;
    out.type = jsonlite::JsonValue::Type::Number;
    out.number = value;
    return out;
}
inline jsonlite::JsonValue jint(long long value) {
    jsonlite::JsonValue out;
    out.type = jsonlite::JsonValue::Type::Number;
    out.number = static_cast<double>(value);
    return out;
}
inline jsonlite::JsonValue jbool(bool value) {
    jsonlite::JsonValue out;
    out.type = jsonlite::JsonValue::Type::Bool;
    out.boolean = value;
    return out;
}
inline jsonlite::JsonValue jnull() { return jsonlite::JsonValue{}; }
inline jsonlite::JsonValue jobj(
    std::vector<std::pair<std::string, jsonlite::JsonValue>> fields) {
    jsonlite::JsonValue out;
    out.type = jsonlite::JsonValue::Type::Object;
    out.object = std::move(fields);
    return out;
}
inline jsonlite::JsonValue jarr(std::vector<jsonlite::JsonValue> items) {
    jsonlite::JsonValue out;
    out.type = jsonlite::JsonValue::Type::Array;
    out.array = std::move(items);
    return out;
}
inline jsonlite::JsonValue row_json(const ProcRow& row) {
    std::vector<jsonlite::JsonValue> flags;
    for (const auto& flag : row.flags) flags.push_back(jstr(flag));
    return jobj({{"pid", jint(row.pid)},
                 {"name", jstr(row.name)},
                 {"cpu", jnum(row.cpu)},
                 {"mem_mb", jnum(row.mem_mb)},
                 {"plane", jstr(row.plane)},
                 {"io_read_mb", jnum(row.io_read_mb)},
                 {"io_write_mb", jnum(row.io_write_mb)},
                 {"flags", jarr(std::move(flags))}});
}
}  // namespace detail

inline jsonlite::JsonValue snapshot_to_json(const Snapshot& snap) {
    using namespace detail;
    std::vector<jsonlite::JsonValue> top_cpu;
    for (const auto& row : snap.top_cpu) top_cpu.push_back(row_json(row));
    std::vector<jsonlite::JsonValue> top_mem;
    for (const auto& row : snap.top_mem) top_mem.push_back(row_json(row));
    std::vector<std::pair<std::string, jsonlite::JsonValue>> plane_fields;
    for (const auto& [name, count] : snap.planes)
        plane_fields.emplace_back(name, jint(count));
    return jobj({
        {"interval", jnum(snap.interval)},
        {"mode", snap.has_mode ? jstr(snap.mode) : jnull()},
        {"processes", jint(static_cast<long long>(snap.processes))},
        {"tracked", jint(static_cast<long long>(snap.tracked))},
        {"cpu_load_pct", jnum(snap.cpu_load_pct)},
        {"mem_used_pct", jnum(snap.mem_used_pct)},
        {"mem_available_mb", jnum(snap.mem_avail_mb)},
        {"resource_limits",
         jobj({{"cpu_pct", jnum(snap.res_cpu_pct)},
               {"ram_pct", jnum(snap.res_ram_pct)},
               {"cpu_over_limit", jbool(snap.res_cpu_over)},
               {"ram_over_limit", jbool(snap.res_ram_over)}})},
        {"worker_ledger",
         jobj({{"cpu_pct", jnum(snap.worker_cpu_pct)},
               {"ram_mb", jnum(snap.worker_ram_mb)},
               {"ram_pct", jnum(snap.worker_ram_pct)},
               {"budget_cpu_pct", jnum(snap.budget_cpu_pct)},
               {"budget_ram_pct", jnum(snap.budget_ram_pct)},
               {"over_budget", jbool(snap.over_budget)},
               {"planes", jobj(std::move(plane_fields))}})},
        {"regulation",
         jobj({{"active", jbool(snap.reg_active)},
               {"pre", jbool(snap.reg_pre)},
               {"over_samples", jint(snap.reg_over)},
               {"under_samples", jint(snap.reg_under)},
               {"strained", jbool(snap.reg_strained)}})},
        {"responsiveness",
         jobj({{"enabled", jbool(snap.resp_enabled)},
               {"latency_ms", jnum(snap.resp_latency_ms)},
               {"baseline_ms", snap.has_resp_baseline
                                   ? jnum(snap.resp_baseline_ms)
                                   : jnull()},
               {"ratio", jnum(snap.resp_ratio)},
               {"strained", jbool(snap.resp_strained)},
               {"strain_samples", jint(snap.resp_strain_samples)},
               {"calm_samples", jint(snap.resp_calm_samples)}})},
        {"probalance",
         jobj({{"enabled", jbool(snap.pb_enabled)},
               {"strained", jbool(snap.pb_strained)},
               {"demoted", jint(snap.pb_demoted)},
               {"max_demotions", jint(snap.pb_max)}})},
        {"features",
         jobj({{"probalance", jbool(snap.features.probalance)},
               {"cpu_limiter", jbool(snap.features.cpu_limiter)},
               {"background_mode", jbool(snap.features.background_mode)},
               {"ecoqos", jbool(snap.features.ecoqos)},
               {"limiter_percent", jnum(snap.features.limiter_percent)},
               {"worker_job_cap", jbool(snap.features.worker_job_cap)},
               {"worker_job_percent", jnum(snap.features.worker_job_percent)},
               {"resp_strain_ratio", jnum(snap.features.resp_ratio)},
               {"mode", snap.has_mode ? jstr(snap.mode) : jnull()},
               {"cpu_busy", jnum(snap.thresholds.cpu_busy)},
               {"cpu_extreme", jnum(snap.thresholds.cpu_extreme)},
               {"sustain", jint(snap.thresholds.sustain)},
               {"extreme_sustain", jint(snap.thresholds.extreme_sustain)},
               {"mem_trim_mb", jnum(snap.thresholds.mem_trim_mb)},
               {"affinity", jbool(snap.thresholds.affinity)},
               {"rules_path", jstr(snap.rules_path)},
               {"rules_loaded", jbool(snap.rules_loaded)},
               {"rules_error", snap.rules_error.empty() ? jnull()
                                                        : jstr(snap.rules_error)}})},
        {"worker_admission_hold", jbool(snap.worker_admission_hold)},
        {"actions", jarr(snap.actions)},
        {"top_cpu", jarr(std::move(top_cpu))},
        {"top_mem", jarr(std::move(top_mem))},
        {"dry_run", jbool(snap.dry_run)},
        {"disabled", jbool(snap.disabled)},
    });
}

/* ------------------------------------------------------------------ */
/* 單一治理週期（與 Python govern_once 語義一致）                       */
/* ------------------------------------------------------------------ */
struct CycleContext {
    std::string self_username;
    std::set<int> self_tree;
    std::string project_root_lower;
    std::string system_root_lower = "c:\\windows";
    double now_mono = 0.0;
    bool disabled = false;
};

Snapshot govern_once(const GovernorConfig& config, const RulesDoc& rules,
                     IEngine& engine, RecordMap& records, RegState& regulation,
                     const CycleContext& ctx, std::vector<jsonlite::JsonValue>& logs);

/* Windows 實作（定義於 resource_governor.cpp；非 Windows 平臺回傳 nullptr）。 */
std::unique_ptr<IEngine> make_windows_engine();

}  // namespace governor
}  // namespace gptbridge

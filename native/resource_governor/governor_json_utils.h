/* governor_json_utils.h — 資源管制器共用常數與 JSON/字串小工具。
 *
 * 由 resource_governor.h 依 A185（≤500 effective 行）拆分而來；內容原樣
 * 平移，語義不變。僅依賴標準庫與共用 jsonlite.h。
 */
#pragma once

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <string>
#include <string_view>

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
/* 動態升降（個別程序）：limiter 下限與每週期步進；持續 extreme
 * 逐步收緊至下限、需求回落逐步放寬回預設上限。 */
inline constexpr double kLimiterMinDynamicPct = 5.0;
inline constexpr double kLimiterStepPct = 2.0;

/* 回收機制（reclaim）：機器 RAM ≥ reclaim_mem_pct 時按 RSS 降序批次
 * 修整工作集，自動釋放實體記憶體；每週期最多 reclaim_batch 個行程。 */
inline constexpr double kReclaimMemPct = 80.0;
inline constexpr int kReclaimBatch = 4;
inline constexpr double kReclaimMinMb = 500.0;

/* 池動態信封（pool_dynamic）：機器 CPU ≥ pool_relief_cpu_pct 時非互動
 * 池共享 Job 率每週期收緊 pool_step_percent 至 pool_floor_percent；
 * 平靜且池需求頂住帽緣時逐步放回預設。 */
inline constexpr double kPoolReliefCpuPct = 75.0;
inline constexpr double kPoolFloorPercent = 5.0;
inline constexpr double kPoolStepPercent = 4.0;

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

}  // namespace governor
}  // namespace gptbridge

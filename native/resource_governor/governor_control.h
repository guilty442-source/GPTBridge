/* governor_control.h — 調節滯回 / 回應度偵測（平台無關控制律狀態）。
 *
 * 由 resource_governor.h 依 A185（≤500 effective 行）拆分而來；內容原樣
 * 平移，語義不變：
 *  - RegState/RegEvent/RegUpdate + regulation_update：worker 聚合預算
 *    滯回（strict INT-10：首次超標即調節）。
 *  - responsiveness_update：回應探針滯後偵測（與 Python
 *    _responsiveness_update 一致）。
 */
#pragma once

#include <cmath>
#include <map>
#include <string>
#include <vector>

#include "governor_advisor.h"
#include "governor_json_utils.h"

namespace gptbridge {
namespace governor {

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
    /* A590/A622：concurrency-budget 世代綁定（跨 watch 週期持續）。 */
    long long budget_generation = 0;
    std::string budget_signature;
    /* 自動模式顧問跨週期滯回狀態（B167/B38 原生接替；
     * streak/last_switch 另經 resource-mode-advisor.json 跨重啟）。 */
    AdvisorState advisor;
    /* 池動態信封：pool → 目前套用的 CPU Job 率（跨週期；未記錄=預設）。 */
    std::map<int, double> pool_cpu_applied;
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

}  // namespace governor
}  // namespace gptbridge

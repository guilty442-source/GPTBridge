/* governor_cycle_env.h — 單一治理週期的內部環境（僅控制律 cpp 使用）。
 *
 * 由 resource_governor.cpp 的 govern_once 依 A185（函式 ≤50 effective 行）
 * 拆分而來：把週期內共享的可變狀態收斂為一個環境結構，逐行程處理步驟在
 * governor_cycle_steps.cpp，週期編排在 resource_governor.cpp。
 * 語義與原單函式實作逐行一致。
 */
#pragma once

#include <map>
#include <set>
#include <vector>

#include "resource_governor.h"

namespace gptbridge {
namespace governor {

struct PbCandidate {
    double cpu = 0.0;
    ProcSample sample;
    ProcKey key;
};

struct CycleEnv {
    const GovernorConfig& config;
    const RulesDoc& rules;
    IEngine& engine;
    RecordMap& records;
    RegState& regulation;
    const CycleContext& ctx;
    std::vector<jsonlite::JsonValue>& logs;
    Snapshot& snap;

    Features features;
    Thresholds thr;
    SysInfo sys;
    int logical = 1;
    int foreground = -1;
    bool dry_run = false;
    double latency_ms = 0.0;
    bool strained = false;
    std::vector<int> cap_affinity;
    std::vector<int> worker_affinity;

    double worker_cpu_sum = 0.0;
    double worker_rss_mb = 0.0;
    std::map<Pool, double> pool_cpu_sum;
    std::map<Pool, double> pool_rss_mb;
    std::map<Pool, int> pool_count;
    std::vector<jsonlite::JsonValue> actions;
    std::vector<ProcRow> rows;
    std::set<ProcKey> seen;
    std::vector<PbCandidate> pb_candidates;
};

/* 規則一次性套用（governor_cycle_rules.cpp）：Lasso 層與靜態層。 */
void apply_rule_lasso(CycleEnv& env, const ProcSample& sample,
                      const ProgramRule& rule, const ProcKey& key,
                      ProcessRecord& record);
void apply_rule_static(CycleEnv& env, const ProcSample& sample,
                       const ProgramRule& rule, ProcessRecord& record);

/* 逐行程處理步驟（governor_cycle_steps.cpp）。 */
void process_sample(CycleEnv& env, ProcSample sample);

}  // namespace governor
}  // namespace gptbridge

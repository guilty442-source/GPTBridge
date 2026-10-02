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

/* 回收候選：reclaim_pass 依 RSS 降序取用；登記於 process_sample
 * （前景/排除/治理平面在候選登記前已豁免）。 */
struct ReclaimCandidate {
    ProcKey key;
    int pid = 0;
    std::string name;
    double rss_mb = 0.0;
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
    /* 有效模式（advisor 接管時 ≠ rules.mode）與其合成 defaults。 */
    std::string effective_mode;
    std::map<std::string, jsonlite::JsonValue> eff_defaults;
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
    /* 池動態信封：各池第一位存活成員 pid（resize 共享 Job 時用）。 */
    std::map<Pool, int> pool_member_pid;
    std::vector<ReclaimCandidate> reclaim_candidates;
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

/* 週期末批次步驟（governor_cycle_steps.cpp）：
 *  - reclaim_pass：RAM 壓力下按 RSS 降序批次修整工作集（自動釋放）。
 *  - pool_rebalance：池動態信封——非互動池 CPU Job 率隨壓力升降。 */
void reclaim_pass(CycleEnv& env);
void pool_rebalance(CycleEnv& env);

}  // namespace governor
}  // namespace gptbridge

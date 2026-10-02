/* governor_snapshot.h — 逐行程紀錄與快照 JSON 契約。
 *
 * 由 resource_governor.h 依 A185（≤500 effective 行）拆分而來；內容原樣
 * 平移，語義不變：
 *  - ProcessRecord/RecordMap：逐行程滯回與規則持有狀態。
 *  - ProcRow/Snapshot + detail::j* + snapshot_to_json：與 Python
 *    snapshot 逐鍵相容的狀態輸出（後端 signal 層消費）。
 */
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "governor_budget.h"
#include "governor_engine.h"
#include "governor_json_utils.h"
#include "governor_rules.h"

namespace gptbridge {
namespace governor {

/* ------------------------------------------------------------------ */
/* 逐行程紀錄                                                          */
/* ------------------------------------------------------------------ */
struct ProcessRecord {
    int busy = 0;
    int calm = 0;
    bool prio_set = false;
    bool prio_idle = false; /* 動態升降：below_normal 之上再降 idle 層 */
    double limit_percent = 0.0; /* 目前套用的 Job CpuRate（0 = 未限速） */
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
    Pool pool = Pool::None;
    bool pool_member = false;
    int pool_join_fails = 0;
    bool pool_join_blocked = false;
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
    std::string pool;
    double io_read_mb = 0.0;
    double io_write_mb = 0.0;
    std::vector<std::string> flags;
};

/* 池聚合帳本（snapshot "pools" 區塊）。 */
struct PoolLedger {
    int processes = 0;
    double cpu_pct = 0.0;      /* 整機 % */
    double ram_mb = 0.0;
    double ram_pct = 0.0;
    double cpu_budget_pct = 0.0;
    double ram_budget_pct = 0.0;
    bool over_budget = false;
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
    std::map<std::string, PoolLedger> pools;
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
    /* 自動模式顧問：auto_mode 開關＋本週期決策記錄（resource-mode-
     * advisor.json 形狀）＋模式切換稽核條目（僅切換週期設定）。 */
    bool auto_mode = false;
    std::optional<jsonlite::JsonValue> advisor;
    std::optional<jsonlite::JsonValue> mode_audit;
    /* A590/A593：全域 concurrency 配額（concurrency-budget/v1；
     * rules 關閉 concurrency_budget 時為 nullopt → JSON null）。 */
    std::optional<ConcurrencyBudget> concurrency_budget;
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
                 {"pool", jstr(row.pool)},
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
    std::vector<std::pair<std::string, jsonlite::JsonValue>> pool_fields;
    for (const auto& [name, ledger] : snap.pools)
        pool_fields.emplace_back(
            name, jobj({{"processes", jint(ledger.processes)},
                        {"cpu_pct", jnum(ledger.cpu_pct)},
                        {"ram_mb", jnum(ledger.ram_mb)},
                        {"ram_pct", jnum(ledger.ram_pct)},
                        {"budget_cpu_pct", jnum(ledger.cpu_budget_pct)},
                        {"budget_ram_pct", jnum(ledger.ram_budget_pct)},
                        {"over_budget", jbool(ledger.over_budget)}}));
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
        {"pools", jobj(std::move(pool_fields))},
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
               {"limiter_dynamic", jbool(snap.features.limiter_dynamic)},
               {"limiter_min_percent",
                jnum(snap.features.limiter_min_percent)},
               {"limiter_step_percent",
                jnum(snap.features.limiter_step_percent)},
               {"priority_escalate",
                jbool(snap.features.priority_escalate)},
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
        {"auto_mode", jbool(snap.auto_mode)},
        {"advisor", snap.advisor ? *snap.advisor : jnull()},
        {"concurrency_budget", snap.concurrency_budget
                                   ? budget_to_json(*snap.concurrency_budget)
                                   : jnull()},
        {"actions", jarr(snap.actions)},
        {"top_cpu", jarr(std::move(top_cpu))},
        {"top_mem", jarr(std::move(top_mem))},
        {"dry_run", jbool(snap.dry_run)},
        {"disabled", jbool(snap.disabled)},
    });
}

}  // namespace governor
}  // namespace gptbridge

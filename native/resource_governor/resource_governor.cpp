/* resource_governor.cpp — C++23 資源管制器：控制律編排（平台無關）。
 *
 * govern_once() 經 IEngine 介面作用，可注入假引擎測試；逐行程處理步驟在
 * governor_cycle_steps.cpp、governor_cycle_rules.cpp，Windows 引擎在
 * governor_engine_win32.cpp。控制律語義與 Python 版一致：
 *  per-core CPU 百分比（100 = 一核滿載）、machine-% 系統負載、Job Object
 *  CPU 硬上限、background-equivalent、EcoQoS、working-set 修整。
 */
#include "governor_cycle_env.h"

#include <algorithm>
#include <cmath>
#include <string>

#include "governor_budget.h"

namespace gptbridge {
namespace governor {
namespace {

using namespace detail;

/* 週期前置：快照前置欄位＋rules 錯誤記錄（fail-closed 僅監控）。 */
void cycle_init(CycleEnv& env) {
    env.snap.interval = env.config.interval;
    env.snap.disabled = env.ctx.disabled;
    env.snap.dry_run = env.config.dry_run || env.ctx.disabled;
    env.dry_run = env.snap.dry_run;
    env.snap.mode = env.rules.mode;
    env.snap.has_mode = env.rules.has_mode;
    env.snap.rules_path = env.config.rules_path;
    env.snap.rules_loaded = !env.rules.defaults.empty() || !env.rules.programs.empty();
    env.snap.rules_error = env.rules.error;
    if (!env.rules.error.empty() && env.regulation.rules_error != env.rules.error) {
        env.logs.push_back(jobj({{"action", jstr("rules-invalid")},
                                 {"path", jstr(env.config.rules_path)},
                                 {"error", jstr(env.rules.error)}}));
    }
    env.regulation.rules_error = env.rules.error;
}

/* 系統取樣＋features/thresholds 解析＋回應探針＋親和性封頂集合。 */
void cycle_prepare(CycleEnv& env) {
    env.sys = env.engine.system();
    env.logical = std::max(1, env.sys.logical);
    env.features = resolve_features(env.config, env.rules.defaults,
                                    env.sys.total_ram_bytes);
    env.thr = resolve_thresholds(env.config, env.rules.defaults);
    env.snap.features = env.features;
    env.snap.thresholds = env.thr;

    env.latency_ms = env.engine.responsiveness();
    env.strained =
        responsiveness_update(env.regulation, env.latency_ms, env.features.resp_ratio);
    env.foreground = env.engine.foreground_pid();

    const int cap_count = std::max(
        kAffinityMinCpus,
        static_cast<int>(std::floor((env.logical * kGlobalCpuLimitPct + 99.0) / 100.0)));
    for (int i = 0; i < std::min(cap_count, env.logical); ++i)
        env.cap_affinity.push_back(i);
    const int worker_cap = std::max(
        kAffinityMinCpus,
        static_cast<int>(std::floor(env.logical * env.thr.worker_cpu_budget / 100.0)));
    for (int i = 0; i < std::min(worker_cap, env.logical); ++i)
        env.worker_affinity.push_back(i);
}

/* 已消失行程：清 Job 上限並移除紀錄。 */
void sweep_dead_records(CycleEnv& env) {
    for (auto it = env.records.begin(); it != env.records.end();) {
        if (env.seen.count(it->first) == 0) {
            env.engine.cpu_limit_clear(it->first);
            it = env.records.erase(it);
        } else {
            ++it;
        }
    }
}

/* probalance：回應緊張時對最高 CPU 候選降優先序（上限 kProbBalanceMaxDemotions）。 */
void probalance_pass(CycleEnv& env) {
    if (!(env.features.probalance && env.strained && !env.ctx.disabled)) return;
    int active_count = 0;
    for (const auto& [existing_key, existing_record] : env.records)
        if (existing_record.pb_set) ++active_count;
    std::vector<PbCandidate> ordered = env.pb_candidates;
    std::sort(ordered.begin(), ordered.end(),
              [](const PbCandidate& a, const PbCandidate& b) { return a.cpu > b.cpu; });
    int demoted = 0;
    for (const PbCandidate& cand : ordered) {
        if (active_count + demoted >= kProbBalanceMaxDemotions) break;
        auto rec_it = env.records.find(cand.key);
        if (rec_it == env.records.end()) continue;
        ProcessRecord& pb_record = rec_it->second;
        if (pb_record.pb_set || pb_record.prio_set || pb_record.bg_set) continue;
        if (cand.cpu < env.thr.cpu_busy) continue;
        if (!env.dry_run) {
            if (!env.engine.set_priority(cand.sample.pid, kPriorityBelowNormal))
                continue;
        }
        pb_record.pb_set = true;
        ++demoted;
        env.actions.push_back(jobj({{"action", jstr("probalance-demote")},
                                    {"pid", jint(cand.sample.pid)},
                                    {"name", jstr(cand.sample.name)},
                                    {"cpu", jnum(round1(cand.cpu))},
                                    {"plane", jstr(plane_name(classify_plane(
                                                 to_lower(cand.sample.exe),
                                                 to_lower(cand.sample.cmdline),
                                                 env.ctx.project_root_lower)))}}));
    }
}

const char* reg_event_name(RegEvent event) {
    switch (event) {
        case RegEvent::PreEntered: return "prethrottle-entered";
        case RegEvent::Entered: return "regulation-entered";
        case RegEvent::Released: return "regulation-released";
        case RegEvent::PreReleased: return "prethrottle-released";
    }
    return "";
}

/* P8 單位修正：逐核累計 → 整機百分比（與 Python 一致除以 logical）。 */
RegUpdate finalize_ledger(CycleEnv& env) {
    const double worker_cpu_machine = env.worker_cpu_sum / std::max(1, env.logical);
    const double worker_ram_pct = env.sys.total_ram_mb > 0
                                      ? env.worker_rss_mb / env.sys.total_ram_mb * 100.0
                                      : 0.0;
    const RegUpdate update = regulation_update(
        env.regulation, worker_cpu_machine, worker_ram_pct,
        env.thr.worker_cpu_budget, env.thr.worker_ram_budget);
    for (RegEvent event : update.events) {
        env.logs.push_back(jobj({{"action", jstr(reg_event_name(event))},
                                 {"worker_cpu_pct", jnum(round1(worker_cpu_machine))},
                                 {"worker_ram_pct", jnum(round2(worker_ram_pct))}}));
    }
    for (const auto& entry : env.actions) env.logs.push_back(entry);
    env.snap.worker_cpu_pct = round1(worker_cpu_machine);
    env.snap.worker_ram_mb = round1(env.worker_rss_mb);
    env.snap.worker_ram_pct = round2(worker_ram_pct);
    return update;
}

void fill_snapshot_core(CycleEnv& env, const RegUpdate& update) {
    std::vector<ProcRow> by_cpu = env.rows;
    std::sort(by_cpu.begin(), by_cpu.end(),
              [](const ProcRow& a, const ProcRow& b) { return a.cpu > b.cpu; });
    std::vector<ProcRow> by_mem = env.rows;
    std::sort(by_mem.begin(), by_mem.end(),
              [](const ProcRow& a, const ProcRow& b) { return a.mem_mb > b.mem_mb; });
    env.snap.processes = env.rows.size();
    env.snap.tracked = env.records.size();
    env.snap.cpu_load_pct = std::max(0.0, env.sys.cpu_load_machine);
    env.snap.mem_used_pct = env.sys.mem_used_pct;
    env.snap.mem_avail_mb = round1(env.sys.mem_avail_mb);
    env.snap.res_cpu_over = env.snap.cpu_load_pct > kGlobalCpuLimitPct;
    env.snap.res_ram_over = env.sys.mem_used_pct > kGlobalRamLimitPct;
    env.snap.budget_cpu_pct = env.thr.worker_cpu_budget;
    env.snap.budget_ram_pct = env.thr.worker_ram_budget;
    env.snap.over_budget = update.over_budget;
    for (const auto& row : env.rows) env.snap.planes[row.plane] += 1;
    /* 池帳本：逐池聚合＋envelope 預算對照（Job 為硬上限，over_budget 為證據旗標）。 */
    for (const auto& [pool, cpu_sum] : env.pool_cpu_sum) {
        PoolLedger ledger;
        ledger.processes = env.pool_count[pool];
        ledger.cpu_pct = round1(cpu_sum / std::max(1, env.logical));
        ledger.ram_mb = round1(env.pool_rss_mb[pool]);
        ledger.ram_pct = env.sys.total_ram_mb > 0
                             ? round2(env.pool_rss_mb[pool] / env.sys.total_ram_mb *
                                      100.0)
                             : 0.0;
        auto it = env.rules.pools.find(pool);
        if (it != env.rules.pools.end()) {
            const PoolPolicy& policy = it->second;
            ledger.cpu_budget_pct = policy.cpu_limit_percent;
            ledger.ram_budget_pct = policy.memory_percent > 0
                                        ? policy.memory_percent
                                        : (policy.memory_mb > 0 &&
                                                   env.sys.total_ram_mb > 0
                                               ? round2(policy.memory_mb /
                                                        env.sys.total_ram_mb *
                                                        100.0)
                                               : 0.0);
            ledger.over_budget =
                (policy.cpu_limit_percent > 0 &&
                 ledger.cpu_pct > policy.cpu_limit_percent) ||
                (ledger.ram_budget_pct > 0 &&
                 ledger.ram_pct > ledger.ram_budget_pct);
        }
        env.snap.pools[pool_name(pool)] = ledger;
    }
    env.snap.top_cpu.assign(by_cpu.begin(),
                            by_cpu.begin() + std::min<std::size_t>(5, by_cpu.size()));
    env.snap.top_mem.assign(by_mem.begin(),
                            by_mem.begin() + std::min<std::size_t>(5, by_mem.size()));
}

void fill_snapshot_state(CycleEnv& env) {
    env.snap.reg_active = env.regulation.active;
    env.snap.reg_pre = env.regulation.pre;
    env.snap.reg_over = env.regulation.over;
    env.snap.reg_under = env.regulation.under;
    env.snap.reg_strained = env.regulation.strained;
    env.snap.resp_enabled = env.features.probalance;
    env.snap.resp_latency_ms = round2(env.latency_ms);
    env.snap.has_resp_baseline = env.regulation.has_baseline;
    env.snap.resp_baseline_ms = round2(env.regulation.resp_baseline);
    env.snap.resp_ratio = env.regulation.resp_ratio_out;
    env.snap.resp_strained = env.regulation.strained;
    env.snap.resp_strain_samples = env.regulation.strain_hits;
    env.snap.resp_calm_samples = env.regulation.calm_hits;
    env.snap.pb_enabled = env.features.probalance;
    env.snap.pb_strained = env.strained;
    env.snap.pb_demoted = 0;
    for (const auto& [existing_key, existing_record] : env.records)
        if (existing_record.pb_set) ++env.snap.pb_demoted;
    env.snap.worker_admission_hold = env.regulation.active || env.regulation.pre;
    env.snap.actions = std::move(env.actions);
}

/* A590/A593/A598：全域 concurrency 配額 — 由 worker 預算滯回、回應探針與
 * 整機過載推得壓力層級，發佈 8 類 quota；配額變動才遞增 generation 並在
 * 日誌留下變更證據（A622 generation 綁定＋before/after 簽章）。 */
void fill_concurrency_budget(CycleEnv& env) {
    const BudgetPolicy policy = resolve_budget_policy(env.rules.defaults);
    if (!policy.enabled) return;
    const bool machine_hot =
        env.sys.cpu_load_machine > kGlobalCpuLimitPct ||
        env.sys.mem_used_pct > kGlobalRamLimitPct;
    PressureTier tier = PressureTier::None;
    if (env.regulation.active || env.strained) {
        tier = PressureTier::Active;
    } else if (env.regulation.pre || machine_hot) {
        tier = PressureTier::Pre;
    }
    ConcurrencyBudget budget = compute_budget(
        policy, env.logical, tier, env.regulation.budget_generation);
    const std::string sig = budget_signature(budget);
    if (sig != env.regulation.budget_signature) {
        budget.generation = env.regulation.budget_generation + 1;
        env.regulation.budget_generation = budget.generation;
        env.logs.push_back(
            jobj({{"action", jstr("concurrency-budget")},
                  {"generation", jint(budget.generation)},
                  {"pressure", jstr(pressure_name(tier))},
                  {"total_quota", jint(budget.total_quota)},
                  {"from", jstr(env.regulation.budget_signature)},
                  {"to", jstr(sig)}}));
        env.regulation.budget_signature = sig;
    }
    env.snap.concurrency_budget = budget;
}

void maybe_log_sample(CycleEnv& env) {
    if (!env.config.log_samples) return;
    env.logs.push_back(jobj(
        {{"action", jstr("sample")},
         {"worker_ledger",
          jobj({{"cpu_pct", jnum(env.snap.worker_cpu_pct)},
                {"ram_mb", jnum(env.snap.worker_ram_mb)},
                {"ram_pct", jnum(env.snap.worker_ram_pct)},
                {"budget_cpu_pct", jnum(env.snap.budget_cpu_pct)},
                {"budget_ram_pct", jnum(env.snap.budget_ram_pct)},
                {"over_budget", jbool(env.snap.over_budget)}})},
         {"regulation",
          jobj({{"active", jbool(env.snap.reg_active)},
                {"pre", jbool(env.snap.reg_pre)},
                {"over_samples", jint(env.snap.reg_over)},
                {"under_samples", jint(env.snap.reg_under)},
                {"strained", jbool(env.snap.reg_strained)}})}}));
}

}  // namespace

/* ================================================================== */
/* govern_once — 控制律編排（平台無關）                                 */
/* ================================================================== */
Snapshot govern_once(const GovernorConfig& config, const RulesDoc& rules,
                     IEngine& engine, RecordMap& records, RegState& regulation,
                     const CycleContext& ctx, std::vector<jsonlite::JsonValue>& logs) {
    Snapshot snap;
    CycleEnv env{config, rules, engine, records, regulation, ctx, logs, snap};

    cycle_init(env);
    cycle_prepare(env);
    for (ProcSample sample : env.engine.enumerate())
        process_sample(env, std::move(sample));
    sweep_dead_records(env);
    probalance_pass(env);
    const RegUpdate update = finalize_ledger(env);
    fill_concurrency_budget(env);
    fill_snapshot_core(env, update);
    fill_snapshot_state(env);
    maybe_log_sample(env);
    return snap;
}

}  // namespace governor
}  // namespace gptbridge

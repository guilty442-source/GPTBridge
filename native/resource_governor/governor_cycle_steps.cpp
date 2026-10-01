/* governor_cycle_steps.cpp — 治理週期逐行程處理步驟。
 *
 * 由 resource_governor.cpp 的 govern_once 依 A185（函式 ≤50 effective 行）
 * 拆分而來；控制律順序與語義逐行一致：分類列 row → 規則套用 → 調節親和性 →
 * busy/extreme 階梯 → working-set 修整 → pb 恢復 → 冷靜釋放。
 */
#include "governor_cycle_env.h"

namespace gptbridge {
namespace governor {
namespace {

using namespace detail;

/* ---------------- 受保護行程 ---------------- */
const std::set<std::string>& protected_names() {
    static const std::set<std::string> names = {
        "uninstallmonitor",  "msi_tracefps",      "processlasso",
        "processgovernor",   "rtss",              "afterburner",
        "system",            "registry",          "idle",
        "memcompression",    "smss",              "csrss",
        "wininit",           "winlogon",          "services",
        "lsass",             "dwm",               "fontdrvhost",
        "svchost",           "audiodg",           "conhost",
        "ctfmon",            "sihost",            "taskhostw",
        "explorer",          "searchhost",        "searchindexer",
        "startmenuexperiencehost", "shellexperiencehost", "textinputhost",
        "runtimebroker",     "securityhealthservice", "securityhealthsystray",
        "msmpeng",           "nissrv",            "spoolsv",
        "wudfhost",          "dllhost",           "wlanext",
        "python-service",
    };
    return names;
}

bool starts_with(std::string_view text, std::string_view prefix) {
    return text.size() >= prefix.size() &&
           text.compare(0, prefix.size(), prefix) == 0;
}

/* 列 row＋flags＋worker/池帳本累計（所有列舉行程皆入列，含 governance/exclude）。 */
void record_row(CycleEnv& env, const ProcSample& sample, Plane plane, Pool pool,
                const ProcessRecord& record) {
    const std::string plane_text = plane_name(plane);
    std::vector<std::string> flags;
    if (record.pb_set) flags.push_back("probalance");
    if (record.bg_set) flags.push_back("background");
    if (record.eco_set) flags.push_back("ecoqos");
    if (record.limit_set) flags.push_back("limit");
    if (record.rule_applied) flags.push_back("rule");
    if (record.pool_member) flags.push_back("pool-job");
    env.rows.push_back(ProcRow{sample.pid, sample.name, round1(sample.cpu_percore),
                               round1(sample.rss_mb), plane_text, pool_name(pool),
                               round1(sample.io_read_mb),
                               round1(sample.io_write_mb), flags});
    if (is_worker_plane(plane)) {
        env.worker_cpu_sum += sample.cpu_percore;
        env.worker_rss_mb += sample.rss_mb;
    }
    if (pool != Pool::None) {
        env.pool_cpu_sum[pool] += sample.cpu_percore;
        env.pool_rss_mb[pool] += sample.rss_mb;
        env.pool_count[pool] += 1;
    }
}

/* 規則查找：exe 基名 → 行程名（程式表鍵皆小寫）。 */
const ProgramRule* find_rule(const CycleEnv& env, const std::string& name_lower,
                             const std::string& exe_lower) {
    if (env.rules.programs.empty()) return nullptr;
    const std::string exe_base = to_lower(base_name(exe_lower));
    auto it = env.rules.programs.find(exe_base);
    if (it == env.rules.programs.end()) it = env.rules.programs.find(name_lower);
    return it != env.rules.programs.end() ? &it->second : nullptr;
}

/* 池 Job sentinel key：-10 interactive、-11 compute、-12 io、-13 background
 * （-1 保留給 worker 共享 Job；負 pid 皆走引擎共享 Job 路徑）。 */
ProcKey pool_job_key(Pool pool) {
    switch (pool) {
        case Pool::Interactive: return ProcKey{-10, 0};
        case Pool::Compute: return ProcKey{-11, 0};
        case Pool::Io: return ProcKey{-12, 0};
        case Pool::Background: return ProcKey{-13, 0};
        case Pool::None: return ProcKey{0, 0};
    }
    return ProcKey{0, 0};
}

/* 池信封執法：成員加入該池共享 Job（CPU/記憶體/行程數上限）並於首次入池
 * 套用一次性靜態屬性（priority/background/ecoqos，受 features 閘門）。 */
void pool_envelope(CycleEnv& env, const ProcSample& sample, Pool pool,
                   ProcessRecord& record) {
    if (pool == Pool::None || record.pool_member) return;
    /* Windows：進程一旦入任一 Job Object 便無法移出。已被 worker 共享 Job
     * 捕獲的進程永遠無法遷入池 Job —— 記錄一次後停止重試。 */
    if (record.job_member) {
        if (!record.pool_join_blocked) {
            record.pool_join_blocked = true;
            env.actions.push_back(
                jobj({{"action", jstr("pool-join-blocked")},
                      {"pid", jint(sample.pid)},
                      {"name", jstr(sample.name)},
                      {"pool", jstr(pool_name(pool))},
                      {"reason", jstr("in-worker-job")}}));
        }
        return;
    }
    auto it = env.rules.pools.find(pool);
    if (it == env.rules.pools.end() || !it->second.enabled) return;
    const PoolPolicy& policy = it->second;
    long long mem_bytes = policy.memory_mb * 1024LL * 1024LL;
    if (mem_bytes <= 0 && policy.memory_percent > 0 &&
        env.sys.total_ram_bytes > 0)
        mem_bytes = static_cast<long long>(
            static_cast<double>(env.sys.total_ram_bytes) *
            std::min(policy.memory_percent, 100.0) / 100.0);
    const bool has_limits = policy.cpu_limit_percent > 0 || mem_bytes > 0 ||
                            policy.process_limit > 0;
    if (has_limits) {
        const bool ok =
            env.dry_run ||
            env.engine.cpu_limit(pool_job_key(pool), sample.pid,
                                 policy.cpu_limit_percent > 0
                                     ? policy.cpu_limit_percent
                                     : 100.0,
                                 mem_bytes, policy.process_limit);
        if (ok || env.dry_run) record.pool_member = true;
        env.actions.push_back(
            jobj({{"action", jstr("pool-joined")},
                  {"pid", jint(sample.pid)},
                  {"name", jstr(sample.name)},
                  {"pool", jstr(pool_name(pool))},
                  {"limiter_percent", jnum(policy.cpu_limit_percent)},
                  {"ok", jbool(ok)}}));
    } else {
        record.pool_member = true;
        env.actions.push_back(jobj({{"action", jstr("pool-joined")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"pool", jstr(pool_name(pool))},
                                    {"ok", jbool(true)}}));
    }
    if (policy.priority_class.has_value() && !env.dry_run &&
        !record.prio_set && !record.bg_set)
        env.engine.set_priority(sample.pid, *policy.priority_class);
    if (policy.background && env.features.background_mode &&
        !record.bg_set) {
        const bool bg_ok = env.dry_run || env.engine.background(sample.pid, true);
        record.bg_set = true;
        env.actions.push_back(jobj({{"action", jstr("pool-background")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"pool", jstr(pool_name(pool))},
                                    {"ok", jbool(bg_ok)}}));
    }
    if (policy.ecoqos && env.features.ecoqos && !record.eco_set) {
        const bool eco_ok = env.dry_run || env.engine.ecoqos(sample.pid, true);
        record.eco_set = true;
        env.actions.push_back(jobj({{"action", jstr("pool-ecoqos")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"pool", jstr(pool_name(pool))},
                                    {"ok", jbool(eco_ok)}}));
    }
}

/* worker 共享 Job 上限＋pb 候選登記（池成員已有池 Job，不再進 worker Job：
 * Windows 單一行程僅能隸屬一個 Job Object）。 */
void job_cap_and_pb(CycleEnv& env, const ProcSample& sample, Plane plane,
                    const ProcKey& key, ProcessRecord& record) {
    /* 只對未分池（Pool::None）的 worker 套用共享 Job：池分類進程若池加入
     * 失敗，寧可本輪不受控，也不可進 worker Job —— Job 成員身分不可逆，
     * 一旦捕獲便永久喪失遷入池的資格。 */
    if (env.features.worker_job_cap && is_worker_plane(plane) &&
        record.pool == Pool::None &&
        !record.job_member && !record.pool_member) {
        static const ProcKey kWorkerJob{-1, 0};
        const bool ok = env.dry_run || env.engine.cpu_limit(kWorkerJob, sample.pid,
                                                          env.features.worker_job_percent,
                                                          env.features.worker_job_memory_bytes,
                                                          env.features.worker_job_process_limit);
        if (ok || env.dry_run) record.job_member = true;
        env.actions.push_back(jobj({{"action", jstr("worker-job-capped")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"limiter_percent", jnum(env.features.worker_job_percent)},
                                    {"ok", jbool(ok)}}));
    }
    if (env.features.probalance && sample.pid != env.foreground)
        env.pb_candidates.push_back(PbCandidate{sample.cpu_percore, sample, key});
}

/* 調節中 worker 親和性封頂；解除調節後還原全集。 */
void regulation_affinity(CycleEnv& env, const ProcSample& sample, Plane plane,
                         ProcessRecord& record) {
    if (!(is_worker_plane(plane) && env.thr.affinity)) return;
    if (env.regulation.active && !record.reg_aff_set) {
        auto current = env.engine.get_affinity(sample.pid);
        const std::vector<int> cur =
            current.has_value() ? *current : env.worker_affinity;
        if (cur != env.worker_affinity && !env.dry_run)
            env.engine.set_affinity(sample.pid, env.worker_affinity);
        record.reg_aff_set = true;
        env.actions.push_back(jobj({{"action", jstr("worker-affinity-capped")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"cpus", jint(static_cast<long long>(
                                                  env.worker_affinity.size()))}}));
    } else if (!env.regulation.active && record.reg_aff_set) {
        std::vector<int> full;
        for (int i = 0; i < env.logical; ++i) full.push_back(i);
        if (!env.dry_run) env.engine.set_affinity(sample.pid, full);
        record.reg_aff_set = false;
        env.actions.push_back(jobj({{"action", jstr("worker-affinity-restored")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)}}));
    }
}

/* busy→priority 階梯＋extreme→affinity 封頂。 */
void busy_tiers(CycleEnv& env, const ProcSample& sample, ProcessRecord& record,
                bool busy_now, bool extreme_now, int sustain_need) {
    if (busy_now && record.busy >= sustain_need && !record.prio_set) {
        if (!env.dry_run) env.engine.set_priority(sample.pid, kPriorityBelowNormal);
        record.prio_set = true;
        env.actions.push_back(jobj({{"action", jstr("priority-below-normal")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"cpu", jnum(round1(sample.cpu_percore))},
                                    {"mem_mb", jnum(round1(sample.rss_mb))}}));
    }
    if (extreme_now && env.thr.affinity && record.busy >= env.thr.extreme_sustain &&
        !record.aff_set) {
        auto current = env.engine.get_affinity(sample.pid);
        const std::vector<int> cur =
            current.has_value() ? *current : env.cap_affinity;
        if (cur != env.cap_affinity) {
            if (!env.dry_run) env.engine.set_affinity(sample.pid, env.cap_affinity);
            record.aff_set = true;
            env.actions.push_back(jobj({{"action", jstr("affinity-capped")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"cpu", jnum(round1(sample.cpu_percore))},
                                        {"cpus", jint(static_cast<long long>(
                                                          env.cap_affinity.size()))}}));
        }
    }
}

/* extreme＋worker 持續 → background/EcoQoS/CPU limiter 階梯。 */
void tier_lasso(CycleEnv& env, const ProcSample& sample, Plane plane,
                const ProcKey& key, ProcessRecord& record, bool extreme_now) {
    if (!(extreme_now && is_worker_plane(plane) &&
          record.busy >= env.thr.extreme_sustain))
        return;
    if (env.features.background_mode && !record.bg_set) {
        const bool ok = env.dry_run || env.engine.background(sample.pid, true);
        record.bg_set = true;
        env.actions.push_back(jobj({{"action", jstr("background-mode")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"cpu", jnum(round1(sample.cpu_percore))},
                                    {"ok", jbool(ok)}}));
    }
    if (env.features.ecoqos && !record.eco_set) {
        const bool ok = env.dry_run || env.engine.ecoqos(sample.pid, true);
        record.eco_set = true;
        env.actions.push_back(jobj({{"action", jstr("ecoqos")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"cpu", jnum(round1(sample.cpu_percore))},
                                    {"ok", jbool(ok)}}));
    }
    if (env.features.cpu_limiter && !record.limit_set) {
        const bool ok =
            env.dry_run ||
            env.engine.cpu_limit(key, sample.pid, env.features.limiter_percent, 0, 0);
        record.limit_set = true;
        env.actions.push_back(jobj({{"action", jstr("cpu-limited")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"cpu", jnum(round1(sample.cpu_percore))},
                                    {"limiter_percent",
                                     jnum(env.features.limiter_percent)},
                                    {"ok", jbool(ok)}}));
    }
}

/* 冷靜高 RSS → working-set 修整（coldown 受 trim_cooldown 限制）。 */
void maybe_trim(CycleEnv& env, const ProcSample& sample, ProcessRecord& record,
                bool calm_now) {
    if (!(sample.rss_mb >= env.thr.mem_trim_mb && calm_now &&
          env.ctx.now_mono - record.last_trim_mono >= env.config.trim_cooldown))
        return;
    bool trimmed = true;
    if (!env.dry_run) trimmed = env.engine.trim(sample.pid);
    record.last_trim_mono = env.ctx.now_mono;
    env.actions.push_back(jobj({{"action", jstr("working-set-trimmed")},
                                {"pid", jint(sample.pid)},
                                {"name", jstr(sample.name)},
                                {"cpu", jnum(round1(sample.cpu_percore))},
                                {"mem_mb", jnum(round1(sample.rss_mb))},
                                {"ok", jbool(trimmed)}}));
}

/* pb 冷靜恢復（規則優先序優先；bg 持有者不降回）。 */
void release_pb(CycleEnv& env, const ProcSample& sample, ProcessRecord& record,
                bool calm_now) {
    if (!(record.pb_set && !env.strained && calm_now)) return;
    if (!env.dry_run && !record.bg_set) {
        const int target = record.rule_priority.has_value()
                               ? *record.rule_priority
                               : kPriorityNormal;
        env.engine.set_priority(sample.pid, target);
    }
    record.pb_set = false;
    env.actions.push_back(jobj({{"action", jstr("probalance-restored")},
                                {"pid", jint(sample.pid)},
                                {"name", jstr(sample.name)}}));
}

/* 冷靜期釋放 priority/affinity（尊重規則持有）。 */
void release_static(CycleEnv& env, const ProcSample& sample,
                    ProcessRecord& record) {
    if (record.prio_set && !record.pb_set && !record.bg_set) {
        if (!env.dry_run) {
            const int target = record.rule_priority.has_value()
                                   ? *record.rule_priority
                                   : kPriorityNormal;
            env.engine.set_priority(sample.pid, target);
        }
        record.prio_set = false;
        env.actions.push_back(jobj({{"action", jstr("restored")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"cpu", jnum(round1(sample.cpu_percore))}}));
    }
    if (record.aff_set) {
        if (!env.dry_run && !record.rule_aff_set) {
            std::vector<int> full;
            for (int i = 0; i < env.logical; ++i) full.push_back(i);
            env.engine.set_affinity(sample.pid, full);
        }
        record.aff_set = false;
        env.actions.push_back(jobj({{"action", jstr("affinity-restored")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)}}));
    }
}

/* 冷靜期釋放 background/EcoQoS/CPU limiter（尊重 rule_hold）。 */
void release_lasso(CycleEnv& env, const ProcSample& sample, const ProcKey& key,
                   ProcessRecord& record) {
    if (record.bg_set && record.rule_hold.count("bg") == 0) {
        const bool ok = env.dry_run || env.engine.background(sample.pid, false);
        record.bg_set = false;
        if (record.rule_priority.has_value() && !env.dry_run)
            env.engine.set_priority(sample.pid, *record.rule_priority);
        env.actions.push_back(jobj({{"action", jstr("background-mode-released")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"ok", jbool(ok)}}));
    }
    if (record.eco_set && record.rule_hold.count("eco") == 0) {
        const bool ok = env.dry_run || env.engine.ecoqos(sample.pid, false);
        record.eco_set = false;
        env.actions.push_back(jobj({{"action", jstr("ecoqos-released")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"ok", jbool(ok)}}));
    }
    if (record.limit_set && record.rule_hold.count("limit") == 0) {
        const bool ok = env.dry_run || env.engine.cpu_limit_clear(key);
        record.limit_set = false;
        env.actions.push_back(jobj({{"action", jstr("cpu-limit-released")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"ok", jbool(ok)}}));
    }
}

}  // namespace

void process_sample(CycleEnv& env, ProcSample sample) {
    if (sample.pid <= 4 || sample.name.empty()) return;
    if (env.ctx.self_tree.count(sample.pid) != 0) return;
    if (sample.cmdline.find("resource-governor") != std::string::npos) return;
    const std::string name_lower = to_lower(sample.name);
    const std::string exe_lower = to_lower(sample.exe);
    if (protected_names().count(name_lower) != 0) return;
    if (!exe_lower.empty() && starts_with(exe_lower, env.ctx.system_root_lower)) return;
    if (!sample.username.empty() && !env.ctx.self_username.empty() &&
        to_lower(sample.username) != to_lower(env.ctx.self_username))
        return;

    const ProcKey key{sample.pid, sample.create_ms};
    env.seen.insert(key);
    ProcessRecord& record = env.records[key];

    const std::string cmdline_lower = to_lower(sample.cmdline);
    const Plane plane =
        classify_plane(exe_lower, cmdline_lower, env.ctx.project_root_lower);
    const ProgramRule* rule = find_rule(env, name_lower, exe_lower);
    const Pool pool = classify_pool(name_lower, exe_lower, cmdline_lower, plane,
                                    env.rules, rule);
    record.pool = pool;
    record_row(env, sample, plane, pool, record);

    if (rule != nullptr && rule->exclude) return;
    if (plane == Plane::Governance) return;

    /* Kill switch（§10.64 驗收 ⑤）：僅觀測、不動作。注意此處比 Python
     * 版更嚴格——Python 版在 dry-run/disabled 下仍會寫入動作紀錄
     * （ok:true）並標記 record 狀態；C++ 版在 disabled 時跳過整個控制
     * 區：不寫動作、不標記狀態，下一輪啟用時重新正常收斂。 */
    if (env.ctx.disabled) return;

    pool_envelope(env, sample, pool, record);
    job_cap_and_pb(env, sample, plane, key, record);
    if (rule != nullptr && !record.rule_applied) {
        record.rule_applied = true;
        apply_rule_lasso(env, sample, *rule, key, record);
        apply_rule_static(env, sample, *rule, record);
    }
    regulation_affinity(env, sample, plane, record);

    const bool throttling_workers =
        (env.regulation.active || env.regulation.pre) && is_worker_plane(plane);
    const double busy_floor =
        throttling_workers ? kRegulatedWorkerBusyPct : env.thr.cpu_busy;
    const int sustain_need = throttling_workers ? 1 : env.thr.sustain;
    const bool busy_now = sample.cpu_percore >= busy_floor;
    const bool extreme_now = sample.cpu_percore >= env.thr.cpu_extreme;
    const bool calm_now = sample.cpu_percore < env.config.calm;

    record.busy = busy_now ? record.busy + 1 : 0;
    record.calm = calm_now ? record.calm + 1 : 0;

    busy_tiers(env, sample, record, busy_now, extreme_now, sustain_need);
    tier_lasso(env, sample, plane, key, record, extreme_now);
    maybe_trim(env, sample, record, calm_now);
    release_pb(env, sample, record, calm_now);
    if (calm_now && record.calm >= env.config.calm_samples) {
        release_static(env, sample, record);
        release_lasso(env, sample, key, record);
    }
}

}  // namespace governor
}  // namespace gptbridge

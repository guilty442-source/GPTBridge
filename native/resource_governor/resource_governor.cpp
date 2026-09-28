/* resource_governor.cpp — C++23 資源管制器：控制律編排 + Windows 引擎。
 *
 * govern_once() 為平台無關的控制律（經 IEngine 介面作用，可注入假引擎測試）；
 * WindowsEngine 以 Win32/NT API 實作列舉與動作，與 Python 版行為一致：
 *  per-core CPU 百分比（100 = 一核滿載）、machine-% 系統負載、Job Object
 *  CPU 硬上限、background-equivalent、EcoQoS、working-set 修整。
 *
 * RAII：Handle 守衛 OS 句柄；Job 句柄以 map 持有、解構全數釋放。
 */
#include "resource_governor.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <psapi.h>
#include <tlhelp32.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <memory>
#include <numeric>

#pragma comment(lib, "psapi.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "user32.lib")

namespace gptbridge {
namespace governor {
namespace {

/* ---------------- RAII 句柄 ---------------- */
class Handle {
 public:
    explicit Handle(HANDLE raw = nullptr) : raw_(raw) {}
    ~Handle() { reset(); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    Handle(Handle&& other) noexcept : raw_(other.raw_) { other.raw_ = nullptr; }
    Handle& operator=(Handle&& other) noexcept {
        if (this != &other) {
            reset();
            raw_ = other.raw_;
            other.raw_ = nullptr;
        }
        return *this;
    }
    explicit operator bool() const { return raw_ != nullptr && raw_ != INVALID_HANDLE_VALUE; }
    HANDLE get() const { return raw_; }
    HANDLE release() {
        HANDLE out = raw_;
        raw_ = nullptr;
        return out;
    }
    void reset(HANDLE raw = nullptr) {
        if (raw_ != nullptr && raw_ != INVALID_HANDLE_VALUE) ::CloseHandle(raw_);
        raw_ = raw;
    }

 private:
    HANDLE raw_;
};

std::wstring widen(std::string_view text) {
    if (text.empty()) return L"";
    const int needed = ::MultiByteToWideChar(CP_UTF8, 0, text.data(),
                                             static_cast<int>(text.size()), nullptr, 0);
    std::wstring out(static_cast<std::size_t>(needed), L'\0');
    ::MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()),
                          out.data(), needed);
    return out;
}

std::string narrow(const std::wstring& text) {
    if (text.empty()) return {};
    const int needed =
        ::WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<std::size_t>(needed > 0 ? needed - 1 : 0), '\0');
    if (needed > 0)
        ::WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, out.data(), needed, nullptr,
                              nullptr);
    return out;
}

unsigned long long filetime_to_u64(const FILETIME& ft) {
    ULARGE_INTEGER value{};
    value.LowPart = ft.dwLowDateTime;
    value.HighPart = ft.dwHighDateTime;
    return value.QuadPart;
}

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

}  // namespace

/* ================================================================== */
/* govern_once — 控制律編排（平台無關）                                 */
/* ================================================================== */
Snapshot govern_once(const GovernorConfig& config, const RulesDoc& rules,
                     IEngine& engine, RecordMap& records, RegState& regulation,
                     const CycleContext& ctx, std::vector<jsonlite::JsonValue>& logs) {
    using namespace detail;
    Snapshot snap;
    snap.interval = config.interval;
    snap.disabled = ctx.disabled;
    snap.dry_run = config.dry_run || ctx.disabled;
    const bool dry_run = snap.dry_run;
    snap.mode = rules.mode;
    snap.has_mode = rules.has_mode;
    snap.rules_path = config.rules_path;
    snap.rules_loaded = !rules.defaults.empty() || !rules.programs.empty();
    snap.rules_error = rules.error;
    if (!rules.error.empty() && regulation.rules_error != rules.error) {
        logs.push_back(jobj({{"action", jstr("rules-invalid")},
                             {"path", jstr(config.rules_path)},
                             {"error", jstr(rules.error)}}));
    }
    regulation.rules_error = rules.error;

    const SysInfo sys = engine.system();
    const int logical = std::max(1, sys.logical);
    const Features features = resolve_features(config, rules.defaults, sys.total_ram_bytes);
    const Thresholds thr = resolve_thresholds(config, rules.defaults);
    snap.features = features;
    snap.thresholds = thr;

    const double latency_ms = engine.responsiveness();
    const bool strained =
        responsiveness_update(regulation, latency_ms, features.resp_ratio);
    const int foreground = engine.foreground_pid();

    const int cap_count = std::max(
        kAffinityMinCpus,
        static_cast<int>(std::floor((logical * kGlobalCpuLimitPct + 99.0) / 100.0)));
    std::vector<int> cap_affinity;
    for (int i = 0; i < std::min(cap_count, logical); ++i) cap_affinity.push_back(i);
    const int worker_cap = std::max(
        kAffinityMinCpus,
        static_cast<int>(std::floor(logical * thr.worker_cpu_budget / 100.0)));
    std::vector<int> worker_affinity;
    for (int i = 0; i < std::min(worker_cap, logical); ++i) worker_affinity.push_back(i);

    double worker_cpu_sum = 0.0;
    double worker_rss_mb = 0.0;
    std::vector<jsonlite::JsonValue> actions;
    std::vector<ProcRow> rows;
    std::set<ProcKey> seen;
    struct PbCandidate {
        double cpu = 0.0;
        ProcSample sample;
        ProcKey key;
    };
    std::vector<PbCandidate> pb_candidates;

    for (ProcSample sample : engine.enumerate()) {
        if (sample.pid <= 4 || sample.name.empty()) continue;
        if (ctx.self_tree.count(sample.pid) != 0) continue;
        if (sample.cmdline.find("resource-governor") != std::string::npos) continue;
        const std::string name_lower = to_lower(sample.name);
        const std::string exe_lower = to_lower(sample.exe);
        if (protected_names().count(name_lower) != 0) continue;
        if (!exe_lower.empty() && starts_with(exe_lower, ctx.system_root_lower)) continue;
        if (!sample.username.empty() && !ctx.self_username.empty() &&
            to_lower(sample.username) != to_lower(ctx.self_username))
            continue;

        const ProcKey key{sample.pid, sample.create_ms};
        seen.insert(key);
        ProcessRecord& record = records[key];

        const Plane plane =
            classify_plane(exe_lower, to_lower(sample.cmdline), ctx.project_root_lower);
        const std::string plane_text = plane_name(plane);
        std::vector<std::string> flags;
        if (record.pb_set) flags.push_back("probalance");
        if (record.bg_set) flags.push_back("background");
        if (record.eco_set) flags.push_back("ecoqos");
        if (record.limit_set) flags.push_back("limit");
        if (record.rule_applied) flags.push_back("rule");
        rows.push_back(ProcRow{sample.pid, sample.name, round1(sample.cpu_percore),
                               round1(sample.rss_mb), plane_text, round1(sample.io_read_mb),
                               round1(sample.io_write_mb), flags});
        if (is_worker_plane(plane)) {
            worker_cpu_sum += sample.cpu_percore;
            worker_rss_mb += sample.rss_mb;
        }

        const ProgramRule* rule = nullptr;
        if (!rules.programs.empty()) {
            const std::string exe_base = to_lower(base_name(sample.exe));
            auto it = rules.programs.find(exe_base);
            if (it == rules.programs.end()) it = rules.programs.find(name_lower);
            if (it != rules.programs.end()) rule = &it->second;
        }
        if (rule != nullptr && rule->exclude) continue;
        if (plane == Plane::Governance) continue;

        /* Kill switch（§10.64 驗收 ⑤）：僅觀測、不動作。注意此處比 Python
         * 版更嚴格——Python 版在 dry-run/disabled 下仍會寫入動作紀錄
         * （ok:true）並標記 record 狀態；C++ 版在 disabled 時跳過整個控制
         * 區：不寫動作、不標記狀態，下一輪啟用時重新正常收斂。 */
        const bool observe_only = ctx.disabled;
        if (!observe_only) {
        if (features.worker_job_cap && is_worker_plane(plane) && !record.job_member) {
            static const ProcKey kWorkerJob{-1, 0};
            const bool ok = dry_run || engine.cpu_limit(kWorkerJob, sample.pid,
                                                        features.worker_job_percent,
                                                        features.worker_job_memory_bytes,
                                                        features.worker_job_process_limit);
            if (ok || dry_run) record.job_member = true;
            actions.push_back(jobj({{"action", jstr("worker-job-capped")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"limiter_percent", jnum(features.worker_job_percent)},
                                    {"ok", jbool(ok)}}));
        }
        if (features.probalance && sample.pid != foreground)
            pb_candidates.push_back(PbCandidate{sample.cpu_percore, sample, key});

        if (rule != nullptr && !record.rule_applied) {
            record.rule_applied = true;
            if (rule->background && features.background_mode) {
                const bool ok = dry_run || engine.background(sample.pid, true);
                record.bg_set = true;
                record.rule_hold.insert("bg");
                actions.push_back(jobj({{"action", jstr("rule-background-mode")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"ok", jbool(ok)}}));
            }
            if (rule->ecoqos && features.ecoqos) {
                const bool ok = dry_run || engine.ecoqos(sample.pid, true);
                record.eco_set = true;
                record.rule_hold.insert("eco");
                actions.push_back(jobj({{"action", jstr("rule-ecoqos")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"ok", jbool(ok)}}));
            }
            if (rule->cpu_limit_percent > 0 && features.cpu_limiter) {
                const bool ok =
                    dry_run ||
                    engine.cpu_limit(key, sample.pid, rule->cpu_limit_percent, 0, 0);
                record.limit_set = true;
                record.rule_hold.insert("limit");
                actions.push_back(jobj({{"action", jstr("rule-cpu-limited")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"limiter_percent", jnum(rule->cpu_limit_percent)},
                                        {"ok", jbool(ok)}}));
            }
            if (rule->priority_class.has_value() &&
                !(rule->background && features.background_mode)) {
                if (!dry_run) engine.set_priority(sample.pid, *rule->priority_class);
                record.rule_priority = *rule->priority_class;
                actions.push_back(jobj({{"action", jstr("rule-priority")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"priority", jint(*rule->priority_class)}}));
            }
            if (rule->affinity.has_value() && config.affinity) {
                if (!dry_run) engine.set_affinity(sample.pid, *rule->affinity);
                record.rule_aff_set = true;
                record.rule_hold.insert("affinity");
                actions.push_back(jobj({{"action", jstr("rule-affinity")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"cpus", jint(static_cast<long long>(
                                                      rule->affinity->size()))}}));
            }
        }

        const bool throttling_workers =
            (regulation.active || regulation.pre) && is_worker_plane(plane);
        const double busy_floor =
            throttling_workers ? kRegulatedWorkerBusyPct : thr.cpu_busy;
        const int sustain_need = throttling_workers ? 1 : thr.sustain;
        const bool busy_now = sample.cpu_percore >= busy_floor;
        const bool extreme_now = sample.cpu_percore >= thr.cpu_extreme;
        const bool calm_now = sample.cpu_percore < config.calm;

        if (is_worker_plane(plane) && thr.affinity) {
            if (regulation.active && !record.reg_aff_set) {
                auto current = engine.get_affinity(sample.pid);
                const std::vector<int> cur =
                    current.has_value() ? *current : worker_affinity;
                if (cur != worker_affinity && !dry_run)
                    engine.set_affinity(sample.pid, worker_affinity);
                record.reg_aff_set = true;
                actions.push_back(jobj({{"action", jstr("worker-affinity-capped")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"cpus", jint(static_cast<long long>(
                                                      worker_affinity.size()))}}));
            } else if (!regulation.active && record.reg_aff_set) {
                std::vector<int> full;
                for (int i = 0; i < logical; ++i) full.push_back(i);
                if (!dry_run) engine.set_affinity(sample.pid, full);
                record.reg_aff_set = false;
                actions.push_back(jobj({{"action", jstr("worker-affinity-restored")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)}}));
            }
        }

        record.busy = busy_now ? record.busy + 1 : 0;
        record.calm = calm_now ? record.calm + 1 : 0;

        if (busy_now && record.busy >= sustain_need && !record.prio_set) {
            if (!dry_run) engine.set_priority(sample.pid, kPriorityBelowNormal);
            record.prio_set = true;
            actions.push_back(jobj({{"action", jstr("priority-below-normal")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"cpu", jnum(round1(sample.cpu_percore))},
                                    {"mem_mb", jnum(round1(sample.rss_mb))}}));
        }
        if (extreme_now && thr.affinity && record.busy >= thr.extreme_sustain &&
            !record.aff_set) {
            auto current = engine.get_affinity(sample.pid);
            const std::vector<int> cur = current.has_value() ? *current : cap_affinity;
            if (cur != cap_affinity) {
                if (!dry_run) engine.set_affinity(sample.pid, cap_affinity);
                record.aff_set = true;
                actions.push_back(jobj({{"action", jstr("affinity-capped")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"cpu", jnum(round1(sample.cpu_percore))},
                                        {"cpus", jint(static_cast<long long>(
                                                          cap_affinity.size()))}}));
            }
        }
        if (extreme_now && is_worker_plane(plane) &&
            record.busy >= thr.extreme_sustain) {
            if (features.background_mode && !record.bg_set) {
                const bool ok = dry_run || engine.background(sample.pid, true);
                record.bg_set = true;
                actions.push_back(jobj({{"action", jstr("background-mode")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"cpu", jnum(round1(sample.cpu_percore))},
                                        {"ok", jbool(ok)}}));
            }
            if (features.ecoqos && !record.eco_set) {
                const bool ok = dry_run || engine.ecoqos(sample.pid, true);
                record.eco_set = true;
                actions.push_back(jobj({{"action", jstr("ecoqos")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"cpu", jnum(round1(sample.cpu_percore))},
                                        {"ok", jbool(ok)}}));
            }
            if (features.cpu_limiter && !record.limit_set) {
                const bool ok =
                    dry_run ||
                    engine.cpu_limit(key, sample.pid, features.limiter_percent, 0, 0);
                record.limit_set = true;
                actions.push_back(jobj({{"action", jstr("cpu-limited")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"cpu", jnum(round1(sample.cpu_percore))},
                                        {"limiter_percent",
                                         jnum(features.limiter_percent)},
                                        {"ok", jbool(ok)}}));
            }
        }
        if (sample.rss_mb >= thr.mem_trim_mb && calm_now &&
            ctx.now_mono - record.last_trim_mono >= config.trim_cooldown) {
            bool trimmed = true;
            if (!dry_run) trimmed = engine.trim(sample.pid);
            record.last_trim_mono = ctx.now_mono;
            actions.push_back(jobj({{"action", jstr("working-set-trimmed")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"cpu", jnum(round1(sample.cpu_percore))},
                                    {"mem_mb", jnum(round1(sample.rss_mb))},
                                    {"ok", jbool(trimmed)}}));
        }
        if (record.pb_set && !strained && calm_now) {
            if (!dry_run && !record.bg_set) {
                const int target = record.rule_priority.has_value()
                                       ? *record.rule_priority
                                       : kPriorityNormal;
                engine.set_priority(sample.pid, target);
            }
            record.pb_set = false;
            actions.push_back(jobj({{"action", jstr("probalance-restored")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)}}));
        }
        if (calm_now && record.calm >= config.calm_samples) {
            if (record.prio_set && !record.pb_set && !record.bg_set) {
                if (!dry_run) {
                    const int target = record.rule_priority.has_value()
                                           ? *record.rule_priority
                                           : kPriorityNormal;
                    engine.set_priority(sample.pid, target);
                }
                record.prio_set = false;
                actions.push_back(jobj({{"action", jstr("restored")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"cpu", jnum(round1(sample.cpu_percore))}}));
            }
            if (record.aff_set) {
                if (!dry_run && !record.rule_aff_set) {
                    std::vector<int> full;
                    for (int i = 0; i < logical; ++i) full.push_back(i);
                    engine.set_affinity(sample.pid, full);
                }
                record.aff_set = false;
                actions.push_back(jobj({{"action", jstr("affinity-restored")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)}}));
            }
            if (record.bg_set && record.rule_hold.count("bg") == 0) {
                const bool ok = dry_run || engine.background(sample.pid, false);
                record.bg_set = false;
                if (record.rule_priority.has_value() && !dry_run)
                    engine.set_priority(sample.pid, *record.rule_priority);
                actions.push_back(jobj({{"action", jstr("background-mode-released")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"ok", jbool(ok)}}));
            }
            if (record.eco_set && record.rule_hold.count("eco") == 0) {
                const bool ok = dry_run || engine.ecoqos(sample.pid, false);
                record.eco_set = false;
                actions.push_back(jobj({{"action", jstr("ecoqos-released")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"ok", jbool(ok)}}));
            }
            if (record.limit_set && record.rule_hold.count("limit") == 0) {
                const bool ok = dry_run || engine.cpu_limit_clear(key);
                record.limit_set = false;
                actions.push_back(jobj({{"action", jstr("cpu-limit-released")},
                                        {"pid", jint(sample.pid)},
                                        {"name", jstr(sample.name)},
                                        {"ok", jbool(ok)}}));
            }
        }      /* calm-restore block */
        } /* !observe_only */
    }

    for (auto it = records.begin(); it != records.end();) {
        if (seen.count(it->first) == 0) {
            engine.cpu_limit_clear(it->first);
            it = records.erase(it);
        } else {
            ++it;
        }
    }

    if (features.probalance && strained && !ctx.disabled) {
        int active_count = 0;
        for (const auto& [existing_key, existing_record] : records)
            if (existing_record.pb_set) ++active_count;
        std::vector<PbCandidate> ordered = pb_candidates;
        std::sort(ordered.begin(), ordered.end(),
                  [](const PbCandidate& a, const PbCandidate& b) { return a.cpu > b.cpu; });
        int demoted = 0;
        for (const PbCandidate& cand : ordered) {
            if (active_count + demoted >= kProbBalanceMaxDemotions) break;
            auto rec_it = records.find(cand.key);
            if (rec_it == records.end()) continue;
            ProcessRecord& pb_record = rec_it->second;
            if (pb_record.pb_set || pb_record.prio_set || pb_record.bg_set) continue;
            if (cand.cpu < thr.cpu_busy) continue;
            if (!dry_run) {
                if (!engine.set_priority(cand.sample.pid, kPriorityBelowNormal))
                    continue;
            }
            pb_record.pb_set = true;
            ++demoted;
            actions.push_back(jobj({{"action", jstr("probalance-demote")},
                                    {"pid", jint(cand.sample.pid)},
                                    {"name", jstr(cand.sample.name)},
                                    {"cpu", jnum(round1(cand.cpu))},
                                    {"plane", jstr(plane_name(classify_plane(
                                                 to_lower(cand.sample.exe),
                                                 to_lower(cand.sample.cmdline),
                                                 ctx.project_root_lower)))}}));
        }
    }

    /* P8 單位修正：逐核累計 → 整機百分比（與 Python 一致除以 logical）。 */
    const double worker_cpu_machine = worker_cpu_sum / std::max(1, logical);
    const double worker_ram_pct = sys.total_ram_mb > 0
                                      ? worker_rss_mb / sys.total_ram_mb * 100.0
                                      : 0.0;
    const RegUpdate update = regulation_update(regulation, worker_cpu_machine,
                                               worker_ram_pct, thr.worker_cpu_budget,
                                               thr.worker_ram_budget);
    for (RegEvent event : update.events) {
        const char* name = "";
        switch (event) {
            case RegEvent::PreEntered: name = "prethrottle-entered"; break;
            case RegEvent::Entered: name = "regulation-entered"; break;
            case RegEvent::Released: name = "regulation-released"; break;
            case RegEvent::PreReleased: name = "prethrottle-released"; break;
        }
        logs.push_back(jobj({{"action", jstr(name)},
                             {"worker_cpu_pct", jnum(round1(worker_cpu_machine))},
                             {"worker_ram_pct", jnum(round2(worker_ram_pct))}}));
    }
    for (const auto& entry : actions) logs.push_back(entry);

    std::vector<ProcRow> by_cpu = rows;
    std::sort(by_cpu.begin(), by_cpu.end(),
              [](const ProcRow& a, const ProcRow& b) { return a.cpu > b.cpu; });
    std::vector<ProcRow> by_mem = rows;
    std::sort(by_mem.begin(), by_mem.end(),
              [](const ProcRow& a, const ProcRow& b) { return a.mem_mb > b.mem_mb; });
    snap.processes = rows.size();
    snap.tracked = records.size();
    snap.cpu_load_pct = std::max(0.0, sys.cpu_load_machine);
    snap.mem_used_pct = sys.mem_used_pct;
    snap.mem_avail_mb = round1(sys.mem_avail_mb);
    snap.res_cpu_over = snap.cpu_load_pct > kGlobalCpuLimitPct;
    snap.res_ram_over = sys.mem_used_pct > kGlobalRamLimitPct;
    snap.worker_cpu_pct = round1(worker_cpu_machine);
    snap.worker_ram_mb = round1(worker_rss_mb);
    snap.worker_ram_pct = round2(worker_ram_pct);
    snap.budget_cpu_pct = thr.worker_cpu_budget;
    snap.budget_ram_pct = thr.worker_ram_budget;
    snap.over_budget = update.over_budget;
    for (const auto& row : rows) snap.planes[row.plane] += 1;
    snap.reg_active = regulation.active;
    snap.reg_pre = regulation.pre;
    snap.reg_over = regulation.over;
    snap.reg_under = regulation.under;
    snap.reg_strained = regulation.strained;
    snap.resp_enabled = features.probalance;
    snap.resp_latency_ms = round2(latency_ms);
    snap.has_resp_baseline = regulation.has_baseline;
    snap.resp_baseline_ms = round2(regulation.resp_baseline);
    snap.resp_ratio = regulation.resp_ratio_out;
    snap.resp_strained = regulation.strained;
    snap.resp_strain_samples = regulation.strain_hits;
    snap.resp_calm_samples = regulation.calm_hits;
    snap.pb_enabled = features.probalance;
    snap.pb_strained = strained;
    snap.pb_demoted = 0;
    for (const auto& [existing_key, existing_record] : records)
        if (existing_record.pb_set) ++snap.pb_demoted;
    snap.worker_admission_hold = regulation.active || regulation.pre;
    snap.actions = std::move(actions);
    snap.top_cpu.assign(by_cpu.begin(), by_cpu.begin() + std::min<std::size_t>(5, by_cpu.size()));
    snap.top_mem.assign(by_mem.begin(), by_mem.begin() + std::min<std::size_t>(5, by_mem.size()));
    if (config.log_samples) {
        logs.push_back(jobj(
            {{"action", jstr("sample")},
             {"worker_ledger",
              jobj({{"cpu_pct", jnum(snap.worker_cpu_pct)},
                    {"ram_mb", jnum(snap.worker_ram_mb)},
                    {"ram_pct", jnum(snap.worker_ram_pct)},
                    {"budget_cpu_pct", jnum(snap.budget_cpu_pct)},
                    {"budget_ram_pct", jnum(snap.budget_ram_pct)},
                    {"over_budget", jbool(snap.over_budget)}})},
             {"regulation",
              jobj({{"active", jbool(snap.reg_active)},
                    {"pre", jbool(snap.reg_pre)},
                    {"over_samples", jint(snap.reg_over)},
                    {"under_samples", jint(snap.reg_under)},
                    {"strained", jbool(snap.reg_strained)}})}}));
    }
    return snap;
}

/* ================================================================== */
/* WindowsEngine — Win32/NT 實作                                       */
/* ================================================================== */
namespace {

using NtSetInformationProcessFn = LONG(WINAPI*)(HANDLE, ULONG, PVOID, ULONG);
using NtQueryInformationProcessFn = LONG(WINAPI*)(HANDLE, ULONG, PVOID, ULONG, PULONG);

NtSetInformationProcessFn nt_set_info() {
    static NtSetInformationProcessFn fn = nullptr;
    static bool tried = false;
    if (!tried) {
        tried = true;
        if (HMODULE mod = ::GetModuleHandleW(L"ntdll.dll"))
            fn = reinterpret_cast<NtSetInformationProcessFn>(
                ::GetProcAddress(mod, "NtSetInformationProcess"));
    }
    return fn;
}

NtQueryInformationProcessFn nt_query_info() {
    static NtQueryInformationProcessFn fn = nullptr;
    static bool tried = false;
    if (!tried) {
        tried = true;
        if (HMODULE mod = ::GetModuleHandleW(L"ntdll.dll"))
            fn = reinterpret_cast<NtQueryInformationProcessFn>(
                ::GetProcAddress(mod, "NtQueryInformationProcess"));
    }
    return fn;
}

struct CpuRateControl {
    DWORD ControlFlags = 0;
    DWORD CpuRate = 0;
};

}  // namespace

class WindowsEngine : public IEngine {
 public:
    std::vector<ProcSample> enumerate() override;
    SysInfo system() override;
    bool set_priority(int pid, int prio_class) override;
    std::expected<std::vector<int>, std::string> get_affinity(int pid) override;
    bool set_affinity(int pid, std::span<const int> cpus) override;
    bool trim(int pid) override;
    bool background(int pid, bool enable) override;
    bool ecoqos(int pid, bool enable) override;
    bool cpu_limit(const ProcKey& key, int pid, double percent,
                   long long job_memory_bytes, int job_process_limit) override;
    bool cpu_limit_clear(const ProcKey& key) override;
    int foreground_pid() override;
    double responsiveness() override;
    ~WindowsEngine() override;

 private:
    struct Track {
        unsigned long long cpu_100ns = 0;
        unsigned long long wall_100ns = 0;
    };
    std::map<int, Track> tracks_;
    bool sys_primed_ = false;
    unsigned long long sys_idle_ = 0;
    unsigned long long sys_kernel_ = 0;
    unsigned long long sys_user_ = 0;
    std::map<ProcKey, HANDLE> jobs_;
    HANDLE shared_job_ = nullptr;

    static unsigned long long now_100ns() {
        FILETIME ft{};
        ::GetSystemTimeAsFileTime(&ft);
        return filetime_to_u64(ft);
    }
};

std::vector<ProcSample> WindowsEngine::enumerate() {
    std::vector<ProcSample> out;
    Handle snap(::CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0));
    if (!snap) return out;
    PROCESSENTRY32W entry{};
    entry.dwSize = sizeof(entry);
    if (!::Process32FirstW(snap.get(), &entry)) return out;
    const unsigned long long wall = now_100ns();
    std::set<int> live;
    do {
        const int pid = static_cast<int>(entry.th32ProcessID);
        live.insert(pid);
        Handle proc(::OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ |
                                      PROCESS_QUERY_LIMITED_INFORMATION,
                                  FALSE, entry.th32ProcessID));
        if (!proc)
            proc.reset(::OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE,
                                     entry.th32ProcessID));
        if (!proc) continue;
        ProcSample sample;
        sample.pid = pid;
        wchar_t path[MAX_PATH * 2]{};
        DWORD path_len = static_cast<DWORD>(std::size(path));
        if (::GetModuleFileNameExW(proc.get(), nullptr, path, path_len) != 0) {
            sample.exe = narrow(path);
        } else {
            wchar_t device[MAX_PATH * 2]{};
            DWORD device_len = static_cast<DWORD>(std::size(device));
            if (::GetProcessImageFileNameW(proc.get(), device, device_len) != 0)
                sample.exe = narrow(device);
        }
        sample.name = sample.exe.empty() ? narrow(entry.szExeFile)
                                         : base_name(sample.exe);
        /* 命令列（NT 內部資訊類，失敗則留空 —— 分類降級而非中斷）。 */
        if (auto query = nt_query_info()) {
            struct RemoteString {
                USHORT Length = 0;
                USHORT MaximumLength = 0;
                PWSTR Buffer = nullptr;
            };
            RemoteString remote{};
            ULONG returned = 0;
            if (query(proc.get(), 60 /*ProcessCommandLineInformation*/, &remote,
                      sizeof(remote), &returned) == 0 &&
                remote.Buffer != nullptr && remote.Length > 0 &&
                remote.Length < 32768) {
                std::vector<wchar_t> buffer(
                    static_cast<std::size_t>(remote.Length / 2) + 1, L'\0');
                SIZE_T read = 0;
                if (::ReadProcessMemory(proc.get(), remote.Buffer, buffer.data(),
                                        remote.Length, &read) &&
                    read > 0) {
                    sample.cmdline =
                        narrow(std::wstring(buffer.data(), read / 2));
                }
            }
        }
        /* 擁有者（Token SID → DOMAIN\\user；失敗留空＝不過濾）。 */
        HANDLE token = nullptr;
        if (::OpenProcessToken(proc.get(), TOKEN_QUERY, &token)) {
            Handle token_guard(token);
            DWORD needed = 0;
            ::GetTokenInformation(token_guard.get(), TokenUser, nullptr, 0, &needed);
            std::vector<std::uint8_t> buffer(needed > 0 ? needed : 1);
            if (::GetTokenInformation(token_guard.get(), TokenUser, buffer.data(),
                                      needed, &needed)) {
                const TOKEN_USER* user =
                    reinterpret_cast<const TOKEN_USER*>(buffer.data());
                wchar_t name[256]{};
                wchar_t domain[256]{};
                DWORD name_len = static_cast<DWORD>(std::size(name));
                DWORD domain_len = static_cast<DWORD>(std::size(domain));
                SID_NAME_USE use = SidTypeUnknown;
                if (::LookupAccountSidW(nullptr, user->User.Sid, name, &name_len,
                                        domain, &domain_len, &use)) {
                    std::string account = narrow(domain);
                    if (!account.empty()) account += "\\";
                    account += narrow(name);
                    sample.username = std::move(account);
                }
            }
        }
        /* CPU（GetProcessTimes 增量；首次見到回 0 並播種）。 */
        FILETIME created{}, exited{}, kernel{}, user{};
        if (::GetProcessTimes(proc.get(), &created, &exited, &kernel, &user)) {
            const unsigned long long cpu =
                filetime_to_u64(kernel) + filetime_to_u64(user);
            ULARGE_INTEGER created_value{};
            created_value.LowPart = created.dwLowDateTime;
            created_value.HighPart = created.dwHighDateTime;
            sample.create_ms =
                static_cast<std::int64_t>(created_value.QuadPart / 10000ULL);
            auto it = tracks_.find(pid);
            if (it != tracks_.end() && wall > it->second.wall_100ns) {
                const unsigned long long dcpu =
                    cpu >= it->second.cpu_100ns ? cpu - it->second.cpu_100ns : 0;
                sample.cpu_percore =
                    100.0 * static_cast<double>(dcpu) /
                    static_cast<double>(wall - it->second.wall_100ns);
            }
            tracks_[pid] = Track{cpu, wall};
        }
        PROCESS_MEMORY_COUNTERS counters{};
        if (::GetProcessMemoryInfo(proc.get(), &counters, sizeof(counters)))
            sample.rss_mb =
                static_cast<double>(counters.WorkingSetSize) / (1024.0 * 1024.0);
        IO_COUNTERS io{};
        if (::GetProcessIoCounters(proc.get(), &io)) {
            sample.io_read_mb =
                static_cast<double>(io.ReadTransferCount) / (1024.0 * 1024.0);
            sample.io_write_mb =
                static_cast<double>(io.WriteTransferCount) / (1024.0 * 1024.0);
        }
        out.push_back(std::move(sample));
    } while (::Process32NextW(snap.get(), &entry));
    for (auto it = tracks_.begin(); it != tracks_.end();) {
        if (live.count(it->first) == 0)
            it = tracks_.erase(it);
        else
            ++it;
    }
    return out;
}

SysInfo WindowsEngine::system() {
    SysInfo info;
    FILETIME idle{}, kernel{}, user{};
    if (::GetSystemTimes(&idle, &kernel, &user)) {
        const unsigned long long idle_now = filetime_to_u64(idle);
        const unsigned long long kernel_now = filetime_to_u64(kernel);
        const unsigned long long user_now = filetime_to_u64(user);
        if (sys_primed_) {
            const unsigned long long didle =
                idle_now >= sys_idle_ ? idle_now - sys_idle_ : 0;
            const unsigned long long dtotal =
                (kernel_now >= sys_kernel_ ? kernel_now - sys_kernel_ : 0) +
                (user_now >= sys_user_ ? user_now - sys_user_ : 0);
            if (dtotal > 0)
                info.cpu_load_machine =
                    100.0 * (1.0 - static_cast<double>(didle) /
                                       static_cast<double>(dtotal));
        }
        sys_idle_ = idle_now;
        sys_kernel_ = kernel_now;
        sys_user_ = user_now;
        sys_primed_ = true;
    }
    MEMORYSTATUSEX status{};
    status.dwLength = sizeof(status);
    if (::GlobalMemoryStatusEx(&status)) {
        info.mem_used_pct = static_cast<double>(status.dwMemoryLoad);
        info.mem_avail_mb =
            static_cast<double>(status.ullAvailPhys) / (1024.0 * 1024.0);
        info.total_ram_mb =
            static_cast<double>(status.ullTotalPhys) / (1024.0 * 1024.0);
        info.total_ram_bytes = static_cast<long long>(status.ullTotalPhys);
    }
    SYSTEM_INFO sysinfo{};
    ::GetSystemInfo(&sysinfo);
    info.logical = static_cast<int>(sysinfo.dwNumberOfProcessors);
    if (info.logical <= 0) info.logical = 1;
    return info;
}

bool WindowsEngine::set_priority(int pid, int prio_class) {
    Handle proc(::OpenProcess(PROCESS_SET_INFORMATION, FALSE,
                              static_cast<DWORD>(pid)));
    if (!proc) return false;
    return ::SetPriorityClass(proc.get(), static_cast<DWORD>(prio_class)) != 0;
}

std::expected<std::vector<int>, std::string> WindowsEngine::get_affinity(int pid) {
    Handle proc(::OpenProcess(PROCESS_QUERY_INFORMATION, FALSE,
                              static_cast<DWORD>(pid)));
    if (!proc) return std::unexpected<std::string>("open-failed");
    DWORD_PTR process_mask = 0;
    DWORD_PTR system_mask = 0;
    if (!::GetProcessAffinityMask(proc.get(), &process_mask, &system_mask))
        return std::unexpected<std::string>("query-failed");
    std::vector<int> cpus;
    for (int i = 0; i < static_cast<int>(sizeof(DWORD_PTR) * 8); ++i)
        if ((process_mask >> i) & 1) cpus.push_back(i);
    return cpus;
}

bool WindowsEngine::set_affinity(int pid, std::span<const int> cpus) {
    DWORD_PTR mask = 0;
    for (int cpu : cpus) {
        if (cpu < 0 || cpu >= static_cast<int>(sizeof(DWORD_PTR) * 8)) continue;
        mask |= (static_cast<DWORD_PTR>(1) << cpu);
    }
    if (mask == 0) return false;
    Handle proc(::OpenProcess(PROCESS_SET_INFORMATION, FALSE,
                              static_cast<DWORD>(pid)));
    if (!proc) return false;
    return ::SetProcessAffinityMask(proc.get(), mask) != 0;
}

bool WindowsEngine::trim(int pid) {
    Handle proc(::OpenProcess(PROCESS_SET_QUOTA | PROCESS_QUERY_LIMITED_INFORMATION,
                              FALSE, static_cast<DWORD>(pid)));
    if (!proc) return false;
    if (::EmptyWorkingSet(proc.get()) != 0) return true;
    return ::SetProcessWorkingSetSize(proc.get(), static_cast<SIZE_T>(-1),
                                      static_cast<SIZE_T>(-1)) != 0;
}

bool WindowsEngine::background(int pid, bool enable) {
    Handle proc(::OpenProcess(PROCESS_SET_INFORMATION, FALSE,
                              static_cast<DWORD>(pid)));
    if (!proc) return false;
    const DWORD prio = enable ? IDLE_PRIORITY_CLASS : NORMAL_PRIORITY_CLASS;
    const bool cpu_ok = ::SetPriorityClass(proc.get(), prio) != 0;
    struct MemoryPriority {
        ULONG Value = 0;
    };
    MemoryPriority memory{enable ? 1UL /*VERY_LOW*/ : 5UL /*NORMAL*/};
    const bool memory_ok =
        ::SetProcessInformation(proc.get(), static_cast<PROCESS_INFORMATION_CLASS>(0),
                                &memory, sizeof(memory)) != 0;
    bool io_ok = false;
    if (auto set = nt_set_info()) {
        ULONG io_class = enable ? 0 /*VERY_LOW*/ : 2 /*NORMAL*/;
        io_ok = set(proc.get(), 33 /*ProcessIoPriority*/, &io_class, sizeof(io_class)) == 0;
    }
    return cpu_ok && (memory_ok || io_ok);
}

bool WindowsEngine::ecoqos(int pid, bool enable) {
    Handle proc(::OpenProcess(PROCESS_SET_INFORMATION, FALSE,
                              static_cast<DWORD>(pid)));
    if (!proc) return false;
    struct PowerThrottling {
        ULONG Version = 1;
        ULONG ControlMask = 0x1;
        ULONG StateMask = 0;
    };
    PowerThrottling state{};
    state.StateMask = enable ? 0x1 : 0;
    return ::SetProcessInformation(proc.get(), static_cast<PROCESS_INFORMATION_CLASS>(4),
                                   &state, sizeof(state)) != 0;
}

bool WindowsEngine::cpu_limit(const ProcKey& key, int pid, double percent,
                              long long job_memory_bytes, int job_process_limit) {
    const bool shared = (key.pid == -1);
    HANDLE job = shared ? shared_job_ : nullptr;
    if (!shared) {
        auto it = jobs_.find(key);
        if (it != jobs_.end()) job = it->second;
    }
    bool created = false;
    if (job == nullptr) {
        job = ::CreateJobObjectW(nullptr, nullptr);
        if (job == nullptr) return false;
        created = true;
        if (shared && (job_memory_bytes > 0 || job_process_limit > 0)) {
            struct IoCounters {
                ULONGLONG ReadOperationCount = 0;
                ULONGLONG WriteOperationCount = 0;
                ULONGLONG OtherOperationCount = 0;
                ULONGLONG ReadTransferCount = 0;
                ULONGLONG WriteTransferCount = 0;
                ULONGLONG OtherTransferCount = 0;
            };
            struct BasicLimit {
                LARGE_INTEGER PerProcessUserTimeLimit{};
                LARGE_INTEGER PerJobUserTimeLimit{};
                DWORD LimitFlags = 0;
                SIZE_T MinimumWorkingSetSize = 0;
                SIZE_T MaximumWorkingSetSize = 0;
                DWORD ActiveProcessLimit = 0;
                ULONG_PTR Affinity = 0;
                DWORD PriorityClass = 0;
                DWORD SchedulingClass = 0;
            };
            struct ExtendedLimit {
                BasicLimit Basic{};
                IoCounters Io{};
                SIZE_T ProcessMemoryLimit = 0;
                SIZE_T JobMemoryLimit = 0;
                SIZE_T PeakProcessMemoryUsed = 0;
                SIZE_T PeakJobMemoryUsed = 0;
            };
            ExtendedLimit limits{};
            if (job_memory_bytes > 0) {
                limits.Basic.LimitFlags |= 0x0200; /*JOB_OBJECT_LIMIT_JOB_MEMORY*/
                limits.JobMemoryLimit = static_cast<SIZE_T>(job_memory_bytes);
            }
            if (job_process_limit > 0) {
                limits.Basic.LimitFlags |= 0x0008; /*JOB_OBJECT_LIMIT_ACTIVE_PROCESS*/
                limits.Basic.ActiveProcessLimit = static_cast<DWORD>(job_process_limit);
            }
            ::SetInformationJobObject(
                job, static_cast<JOBOBJECTINFOCLASS>(9), &limits, sizeof(limits));
        }
    }
    CpuRateControl rate{0x1 | 0x4, static_cast<DWORD>(cpu_rate_value(percent))};
    bool ok = ::SetInformationJobObject(job, static_cast<JOBOBJECTINFOCLASS>(15),
                                        &rate, sizeof(rate)) != 0;
    Handle proc(::OpenProcess(PROCESS_SET_QUOTA | PROCESS_SET_INFORMATION |
                                  PROCESS_TERMINATE |
                                  PROCESS_QUERY_LIMITED_INFORMATION,
                              FALSE, static_cast<DWORD>(pid)));
    if (!proc) {
        if (created) ::CloseHandle(job);
        return false;
    }
    BOOL in_job = FALSE;
    if (::IsProcessInJob(proc.get(), job, &in_job) && in_job) {
        /* 已是成員：僅更新比率。 */
    } else {
        ok = ok && (::AssignProcessToJobObject(job, proc.get()) != 0);
    }
    if (!ok) {
        if (created) ::CloseHandle(job);
        return false;
    }
    if (created) {
        if (shared)
            shared_job_ = job;
        else
            jobs_.emplace(key, job);
    }
    return true;
}

bool WindowsEngine::cpu_limit_clear(const ProcKey& key) {
    HANDLE job = nullptr;
    if (key.pid == -1) {
        job = shared_job_;
        shared_job_ = nullptr;
    } else {
        auto it = jobs_.find(key);
        if (it == jobs_.end()) return false;
        job = it->second;
        jobs_.erase(it);
    }
    if (job == nullptr) return false;
    CpuRateControl rate{0, 0};
    ::SetInformationJobObject(job, static_cast<JOBOBJECTINFOCLASS>(15), &rate,
                              sizeof(rate));
    ::CloseHandle(job);
    return true;
}

int WindowsEngine::foreground_pid() {
    const HWND hwnd = ::GetForegroundWindow();
    if (hwnd == nullptr) return -1;
    DWORD pid = 0;
    ::GetWindowThreadProcessId(hwnd, &pid);
    return static_cast<int>(pid);
}

double WindowsEngine::responsiveness() {
    double samples[kRespProbeRuns]{};
    for (int run = 0; run < kRespProbeRuns; ++run) {
        const auto start = std::chrono::steady_clock::now();
        volatile long long accumulator = 0;
        for (int i = 0; i < kRespProbeIters; ++i) accumulator += i;
        (void)accumulator;
        const auto end = std::chrono::steady_clock::now();
        samples[run] =
            std::chrono::duration<double, std::milli>(end - start).count();
    }
    std::sort(std::begin(samples), std::end(samples));
    return samples[kRespProbeRuns / 2];
}

WindowsEngine::~WindowsEngine() {
    for (const auto& [key, job] : jobs_) {
        CpuRateControl rate{0, 0};
        ::SetInformationJobObject(job, static_cast<JOBOBJECTINFOCLASS>(15), &rate,
                                  sizeof(rate));
        ::CloseHandle(job);
    }
    jobs_.clear();
    if (shared_job_ != nullptr) {
        CpuRateControl rate{0, 0};
        ::SetInformationJobObject(shared_job_, static_cast<JOBOBJECTINFOCLASS>(15),
                                  &rate, sizeof(rate));
        ::CloseHandle(shared_job_);
        shared_job_ = nullptr;
    }
}

std::unique_ptr<IEngine> make_windows_engine();

std::unique_ptr<IEngine> make_windows_engine() {
    return std::make_unique<WindowsEngine>();
}

}  // namespace governor
}  // namespace gptbridge

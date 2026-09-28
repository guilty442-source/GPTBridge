// governor_fake_engine.h — resource-governor suite 共用假引擎與 fixtures。
//
// 由 suite_resource_governor.cpp 依 A185（≤500 effective 行）拆分而來；
// 內容原樣平移，語義不變：FakeEngine 記錄呼叫字串、零 OS 副作用、
// 完全決定性。
#pragma once

#include "../resource_governor/resource_governor.h"

#include <map>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace governor_suite {

namespace gov = gptbridge::governor;
inline const std::string ROOT = "e:\\gptbridge";

struct FakeEngine : gov::IEngine {
    std::vector<gov::ProcSample> procs;
    gov::SysInfo sys;
    double latency = 50.0;
    int foreground = -1;
    std::vector<std::string> calls;
    std::vector<int> full_cpus{0, 1, 2, 3, 4, 5, 6, 7};

    std::vector<gov::ProcSample> enumerate() override { return procs; }
    gov::SysInfo system() override { return sys; }
    bool set_priority(int pid, int prio) override {
        calls.push_back("nice:" + std::to_string(pid) + ":" + std::to_string(prio));
        return true;
    }
    std::expected<std::vector<int>, std::string> get_affinity(int) override {
        return full_cpus;
    }
    bool set_affinity(int pid, std::span<const int> cpus) override {
        std::string entry = "aff:" + std::to_string(pid) + ":";
        for (int cpu : cpus) entry += std::to_string(cpu) + ",";
        calls.push_back(entry);
        return true;
    }
    bool trim(int pid) override {
        calls.push_back("trim:" + std::to_string(pid));
        return true;
    }
    bool background(int pid, bool enable) override {
        calls.push_back(std::string("bg:") + std::to_string(pid) + ":" +
                        (enable ? "1" : "0"));
        return true;
    }
    bool ecoqos(int pid, bool enable) override {
        calls.push_back(std::string("eco:") + std::to_string(pid) + ":" +
                        (enable ? "1" : "0"));
        return true;
    }
    bool cpu_limit(const gov::ProcKey&, int pid, double percent, long long,
                   int) override {
        calls.push_back("limit:" + std::to_string(pid) + ":" + std::to_string(percent));
        return true;
    }
    bool cpu_limit_clear(const gov::ProcKey& key) override {
        calls.push_back("clear:" + std::to_string(key.pid));
        return true;
    }
    int foreground_pid() override { return foreground; }
    double responsiveness() override { return latency; }
};

inline gov::ProcSample worker_proc(int pid, double cpu) {
    gov::ProcSample sample;
    sample.pid = pid;
    sample.name = "python.exe";
    sample.exe = "e:\\gptbridge\\.venv\\python.exe";
    sample.cmdline = "python e:\\gptbridge\\scripts\\train.py";
    sample.username = "u";
    sample.cpu_percore = cpu;
    sample.rss_mb = 20.0;
    sample.create_ms = 1000000LL + pid;
    return sample;
}

inline gov::SysInfo sys8() {
    gov::SysInfo sys;
    sys.cpu_load_machine = 5.0;
    sys.mem_used_pct = 37.5;
    sys.mem_avail_mb = 40000.0;
    sys.total_ram_mb = 65536.0;
    sys.total_ram_bytes = 65536LL * 1024LL * 1024LL;
    sys.logical = 8;
    return sys;
}

inline gov::GovernorConfig base_config() {
    gov::GovernorConfig config;
    config.interval = 5.0;
    config.cpu_busy = 50.0;
    config.cpu_extreme = 90.0;
    config.sustain = 3;
    config.mem_trim_mb = 1500.0;
    config.affinity = true;
    config.dry_run = true;
    config.rules_path = "nonexistent/resource-governor-rules.json";
    return config;
}

inline gov::CycleContext base_ctx() {
    gov::CycleContext ctx;
    ctx.self_username = "u";
    ctx.project_root_lower = ROOT;
    ctx.system_root_lower = "c:\\windows";
    ctx.now_mono = 1000.0;
    return ctx;
}

inline gov::RulesDoc empty_rules() { return gov::RulesDoc{}; }

inline bool has_call(std::span<const std::string> calls, std::string_view prefix) {
    for (const auto& call : calls)
        if (call.rfind(prefix.data(), 0) == 0) return true;
    return false;
}

/* 僅 Lasso 層動作（與 Python _patch_lasso_actions 捕捉面一致；
 * priority/affinity 走行程方法，不在此列）。 */
inline bool has_lasso_call(std::span<const std::string> calls) {
    for (const auto& call : calls)
        if (call.rfind("bg:", 0) == 0 || call.rfind("eco:", 0) == 0 ||
            call.rfind("limit:", 0) == 0 || call.rfind("clear:", 0) == 0)
            return true;
    return false;
}

inline int count_calls(std::span<const std::string> calls, std::string_view prefix) {
    int count = 0;
    for (const auto& call : calls)
        if (call.rfind(prefix.data(), 0) == 0) ++count;
    return count;
}

}  // namespace governor_suite

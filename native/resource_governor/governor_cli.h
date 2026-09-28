/* governor_cli.h — CLI 選項與解析。
 *
 * 由 main.cpp 依 A185（≤500 effective 行）拆分而來；內容原樣平移，
 * 語義不變：CLI 選項對映 Python 版旗標，parse_cli 失敗回 nullopt →
 * usage + exit 2。
 */
#pragma once

#include <filesystem>
#include <optional>
#include <string>
#include <vector>

#include "resource_governor.h"

namespace governor_host {

namespace fs = std::filesystem;

struct CliOptions {
    bool once = false;
    bool watch = false;
    bool dry_run = false;
    bool start = false;
    bool stop = false;
    bool status = false;
    bool install_task = false;
    bool uninstall_task = false;
    bool install_logon = false;
    bool uninstall_logon = false;
    double interval = gptbridge::governor::kDefaultInterval;
    std::optional<double> cpu_busy;
    std::optional<double> cpu_extreme;
    std::optional<double> mem_trim_mb;
    std::optional<int> sustain;
    bool no_affinity = false;
    bool log_samples = false;
    std::string rules;
    std::optional<bool> probalance;
    std::optional<bool> cpu_limiter;
    std::optional<bool> background_mode;
    std::optional<bool> ecoqos;
    std::optional<double> limiter_percent;
    std::optional<double> resp_ratio;
    std::optional<bool> worker_job_cap;
    std::optional<double> worker_job_percent;
    std::string root;
};

int usage(const char* prog);
std::optional<CliOptions> parse_cli(const std::vector<std::string>& args);
gptbridge::governor::GovernorConfig to_config(const CliOptions& opt,
                                              const fs::path& default_rules);

}  // namespace governor_host

/* governor_cli.cpp — CLI 選項解析實作。
 *
 * 由 main.cpp 依 A185 拆分而來；語義不變。
 */
#include "governor_cli.h"

#include <iostream>

#include "governor_host.h"

namespace governor = gptbridge::governor;

namespace governor_host {

int usage(const char* prog) {
    std::cout << "usage: " << prog << " [--once|--watch|--start|--stop|--status]\n"
              << "       [--dry-run] [--interval S] [--cpu-busy PCT] [--cpu-extreme PCT]\n"
              << "       [--mem-trim-mb MB] [--sustain N] [--no-affinity] [--log-samples]\n"
              << "       [--rules PATH] [--root PATH]\n"
              << "       [--probalance|--no-probalance] [--cpu-limiter|--no-cpu-limiter]\n"
              << "       [--background-mode|--no-background-mode] [--ecoqos|--no-ecoqos]\n"
              << "       [--limiter-percent PCT] [--resp-ratio R]\n"
              << "       [--worker-job-cap|--no-worker-job-cap] [--worker-job-percent PCT]\n"
              << "       [--install-task|--uninstall-task|--install-logon|--uninstall-logon]\n";
    return 2;
}

std::optional<CliOptions> parse_cli(const std::vector<std::string>& args) {
    CliOptions opt;
    for (std::size_t i = 1; i < args.size(); ++i) {
        const std::string& arg = args[i];
        auto need_value = [&](std::string& slot) -> bool {
            if (i + 1 >= args.size()) return false;
            slot = args[++i];
            return true;
        };
        auto need_double = [&](std::optional<double>& slot) -> bool {
            if (i + 1 >= args.size()) return false;
            double value = 0;
            if (!parse_number(args[++i], value)) return false;
            slot = value;
            return true;
        };
        if (arg == "--once")
            opt.once = true;
        else if (arg == "--watch")
            opt.watch = true;
        else if (arg == "--dry-run")
            opt.dry_run = true;
        else if (arg == "--start")
            opt.start = true;
        else if (arg == "--stop")
            opt.stop = true;
        else if (arg == "--status")
            opt.status = true;
        else if (arg == "--install-task")
            opt.install_task = true;
        else if (arg == "--uninstall-task")
            opt.uninstall_task = true;
        else if (arg == "--install-logon")
            opt.install_logon = true;
        else if (arg == "--uninstall-logon")
            opt.uninstall_logon = true;
        else if (arg == "--no-affinity")
            opt.no_affinity = true;
        else if (arg == "--log-samples")
            opt.log_samples = true;
        else if (arg == "--probalance")
            opt.probalance = true;
        else if (arg == "--no-probalance")
            opt.probalance = false;
        else if (arg == "--cpu-limiter")
            opt.cpu_limiter = true;
        else if (arg == "--no-cpu-limiter")
            opt.cpu_limiter = false;
        else if (arg == "--background-mode")
            opt.background_mode = true;
        else if (arg == "--no-background-mode")
            opt.background_mode = false;
        else if (arg == "--ecoqos")
            opt.ecoqos = true;
        else if (arg == "--no-ecoqos")
            opt.ecoqos = false;
        else if (arg == "--worker-job-cap")
            opt.worker_job_cap = true;
        else if (arg == "--no-worker-job-cap")
            opt.worker_job_cap = false;
        else if (arg == "--interval") {
            double value = 0;
            if (++i >= args.size() || !parse_number(args[i], value)) return std::nullopt;
            opt.interval = value;
        } else if (arg == "--cpu-busy") {
            if (!need_double(opt.cpu_busy)) return std::nullopt;
        } else if (arg == "--cpu-extreme") {
            if (!need_double(opt.cpu_extreme)) return std::nullopt;
        } else if (arg == "--mem-trim-mb") {
            if (!need_double(opt.mem_trim_mb)) return std::nullopt;
        } else if (arg == "--sustain") {
            int value = 0;
            if (++i >= args.size() || !parse_int(args[i], value)) return std::nullopt;
            opt.sustain = value;
        } else if (arg == "--limiter-percent") {
            if (!need_double(opt.limiter_percent)) return std::nullopt;
        } else if (arg == "--resp-ratio") {
            if (!need_double(opt.resp_ratio)) return std::nullopt;
        } else if (arg == "--worker-job-percent") {
            if (!need_double(opt.worker_job_percent)) return std::nullopt;
        } else if (arg == "--rules") {
            if (!need_value(opt.rules)) return std::nullopt;
        } else if (arg == "--root") {
            if (!need_value(opt.root)) return std::nullopt;
        } else {
            return std::nullopt;
        }
    }
    return opt;
}

governor::GovernorConfig to_config(const CliOptions& opt,
                                   const fs::path& default_rules) {
    governor::GovernorConfig config;
    config.interval = opt.interval;
    config.cpu_busy = opt.cpu_busy;
    config.cpu_extreme = opt.cpu_extreme;
    config.sustain = opt.sustain;
    config.mem_trim_mb = opt.mem_trim_mb;
    config.affinity = !opt.no_affinity;
    config.dry_run = opt.dry_run;
    config.log_samples = opt.log_samples;
    config.rules_path = opt.rules.empty() ? path_u8(default_rules) : opt.rules;
    config.probalance = opt.probalance;
    config.cpu_limiter = opt.cpu_limiter;
    config.background_mode = opt.background_mode;
    config.ecoqos = opt.ecoqos;
    config.limiter_percent = opt.limiter_percent;
    config.resp_ratio = opt.resp_ratio;
    config.worker_job_cap = opt.worker_job_cap;
    config.worker_job_percent = opt.worker_job_percent;
    return config;
}

}  // namespace governor_host

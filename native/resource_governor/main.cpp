/* main.cpp — resource-governor.exe 入口與 CLI 分派。
 *
 * CLI 與 Python 版旗標對映：
 *   --once / --watch / --dry-run / --start / --stop / --status
 *   --install-task / --uninstall-task / --install-logon / --uninstall-logon
 *   --interval / --cpu-busy / --cpu-extreme / --mem-trim-mb / --sustain
 *   --no-affinity / --log-samples / --rules
 *   --probalance[=off] / --cpu-limiter / --background-mode / --ecoqos
 *   --limiter-percent / --resp-ratio / --worker-job-cap / --worker-job-percent
 *   --root（預設自 exe 位置向上找 main-system/config/rules.json）
 *
 * 依 A185 拆分為內聚子模組：host（字串/IO）、lock（行程鎖/身分）、
 * state_io（狀態/日誌）、cli（選項解析）、tasks（排程/登入註冊）、
 * lifecycle（stop/status/start＋once/watch 迴圈）；main() 僅編排分派。
 * 控制律引擎在 resource_governor.cpp ＋ governor_engine_win32.cpp。
 * 產生的 runtime/state、runtime/logs 與 Python 版逐鍵相容，後端 signal
 * 層無需修改即可繼續讀取。
 */
#include "resource_governor.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <iostream>

#include "governor_cli.h"
#include "governor_host.h"
#include "governor_lifecycle.h"
#include "governor_lock.h"
#include "governor_tasks.h"

namespace governor = gptbridge::governor;
using namespace governor_host;

int main() {
    ::SetConsoleOutputCP(CP_UTF8);
    const std::vector<std::string> args = argv_utf8();
    const std::string prog = args.empty() ? "resource-governor.exe" : args[0];
    const auto parsed = parse_cli(args);
    if (!parsed.has_value()) return usage(prog.c_str());
    const CliOptions& opt = *parsed;

    const fs::path root = discover_root(opt.root);
    const fs::path state_dir = root / "main-system" / "runtime" / "state";
    const fs::path log_dir = root / "main-system" / "runtime" / "logs";
    const fs::path state_file = state_dir / "resource-governor.json";
    const fs::path log_file = log_dir / "resource-governor.jsonl";
    const fs::path lock_file = state_dir / "resource-governor.lock";
    const fs::path default_rules =
        root / "main-system" / "config" / "resource-governor-rules.json";

    if (opt.uninstall_task) return uninstall_task();
    if (opt.uninstall_logon) return uninstall_logon();

    governor::GovernorConfig config = to_config(opt, default_rules);
    const fs::path rules_path = widen_str(config.rules_path);

    if (opt.install_task) return install_task(opt, root, rules_path, default_rules);
    if (opt.install_logon) return install_logon(opt, root, rules_path, default_rules);
    if (opt.stop) return stop_governor(lock_file);
    if (opt.start)
        return start_governor(opt, root, rules_path, default_rules, lock_file,
                              state_file, log_file);

    auto engine = governor::make_windows_engine();
    if (engine == nullptr) {
        std::cout << "process engine unavailable\n";
        return 1;
    }
    governor::CycleContext ctx = make_cycle_ctx(root);

    /* 暖機：與 Python --once/--watch 前置列舉/系統取樣等價。 */
    engine->enumerate();
    engine->system();

    if (opt.status || (!opt.once && !opt.watch)) {
        print_status(lock_file, state_file, log_file);
        return 0;
    }
    if (opt.once)
        return run_once(config, ctx, *engine, rules_path, state_file, log_file);
    return run_watch(config, ctx, *engine, rules_path, state_file, log_file,
                     lock_file);
}

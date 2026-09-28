/* governor_lifecycle.cpp — stop/status/start 與 --once/--watch 執行迴圈實作。
 *
 * 由 main.cpp 依 A185 拆分而來；語義不變。
 */
#include "governor_lifecycle.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <iostream>
#include <iterator>
#include <sstream>
#include <thread>

#include "governor_host.h"
#include "governor_lock.h"
#include "governor_state_io.h"
#include "governor_tasks.h"
#include "jsonlite.h"

namespace governor = gptbridge::governor;
namespace jsonlite = gptbridge::jsonlite;

namespace governor_host {
namespace {

std::atomic<bool> g_running{true};

BOOL WINAPI ctrl_handler(DWORD event) {
    if (event == CTRL_C_EVENT || event == CTRL_BREAK_EVENT ||
        event == CTRL_CLOSE_EVENT) {
        g_running = false;
        return TRUE;
    }
    return FALSE;
}

/* --watch 單一週期：hot-reload rules → govern_once → emit/write_state。
 * 例外記 cycle-error 日誌不中斷迴圈（與 Python 一致）。 */
void watch_cycle(const governor::GovernorConfig& config,
                 governor::CycleContext& ctx, governor::IEngine& engine,
                 governor::RecordMap& records, governor::RegState& regulation,
                 const fs::path& rules_path, const fs::path& state_file,
                 const fs::path& log_file) {
    using namespace governor::detail;
    try {
        ctx.now_mono = monotonic_seconds();
        const governor::RulesDoc rules = load_rules_file(rules_path);
        std::vector<jsonlite::JsonValue> logs;
        const governor::Snapshot snap = governor::govern_once(
            config, rules, engine, records, regulation, ctx, logs);
        emit_logs(log_file, logs);
        write_state(state_file, snap);
    } catch (const std::exception& error) {
        const std::vector<jsonlite::JsonValue> single = {
            jobj({{"action", jstr("cycle-error")},
                  {"error", jstr(std::string("exception: ") + error.what())}})};
        emit_logs(log_file, single);
    } catch (...) {
        const std::vector<jsonlite::JsonValue> single = {
            jobj({{"action", jstr("cycle-error")},
                  {"error", jstr("unknown exception")}})};
        emit_logs(log_file, single);
    }
}

}  // namespace

governor::CycleContext make_cycle_ctx(const fs::path& root) {
    governor::CycleContext ctx;
    ctx.self_username = self_account();
    ctx.self_tree = self_tree();
    ctx.project_root_lower = governor::to_lower(path_u8(root));
    wchar_t sysroot[MAX_PATH]{};
    DWORD sysroot_len = ::GetEnvironmentVariableW(L"SystemRoot", sysroot,
                                                  static_cast<DWORD>(std::size(sysroot)));
    ctx.system_root_lower = governor::to_lower(
        sysroot_len > 0 ? narrow_str(sysroot) : "C:\\Windows");
    ctx.disabled = env_disabled();
    return ctx;
}

void print_status(const fs::path& lock_file, const fs::path& state_file,
                  const fs::path& log_file) {
    const bool running = lock_is_active(lock_file);
    std::cout << jsonlite::json_serialize([&] {
        using namespace governor::detail;
        std::vector<std::pair<std::string, jsonlite::JsonValue>> fields;
        fields.emplace_back("running", jbool(running));
        fields.emplace_back("lock_file", jstr(path_u8(lock_file)));
        bool ok = false;
        const std::string state_text = read_file_text(state_file, ok);
        if (ok) {
            try {
                fields.emplace_back("last_cycle",
                                    jsonlite::JsonParser(state_text).parse());
            } catch (const jsonlite::JsonError&) {
                fields.emplace_back("last_cycle", jnull());
            }
        }
        bool log_ok = false;
        const std::string log_text = read_file_text(log_file, log_ok);
        std::vector<jsonlite::JsonValue> recent;
        if (log_ok) {
            std::vector<std::string> lines;
            std::istringstream stream(log_text);
            std::string line;
            while (std::getline(stream, line)) {
                if (!line.empty() && line.back() == '\r') line.pop_back();
                if (!line.empty()) lines.push_back(line);
            }
            const std::size_t from = lines.size() > 10 ? lines.size() - 10 : 0;
            for (std::size_t i = from; i < lines.size(); ++i) {
                try {
                    recent.push_back(jsonlite::JsonParser(lines[i]).parse());
                } catch (const jsonlite::JsonError&) {
                }
            }
        }
        fields.emplace_back("recent_actions", jarr(std::move(recent)));
        return jobj(std::move(fields));
    }()) << "\n";
}

int stop_governor(const fs::path& lock_file) {
    std::error_code ec;
    if (!fs::exists(lock_file, ec)) {
        std::cout << "no lock file; nothing to stop\n";
        return 0;
    }
    bool ok = false;
    const std::string text = read_file_text(lock_file, ok);
    unsigned long pid = 0;
    if (ok) {
        try {
            jsonlite::JsonValue lock_doc = jsonlite::JsonParser(text).parse();
            if (const jsonlite::JsonValue* pid_value = lock_doc.get("pid");
                pid_value != nullptr &&
                pid_value->type == jsonlite::JsonValue::Type::Number)
                pid = static_cast<unsigned long>(pid_value->number);
        } catch (const jsonlite::JsonError&) {
        }
    }
    if (pid == 0) {
        std::cout << "lock file unreadable; remove it manually if no governor "
                     "is running\n";
        return 1;
    }
    HANDLE proc = ::OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, FALSE, pid);
    if (proc == nullptr) {
        fs::remove(lock_file, ec);
        std::cout << "stale lock removed (process already gone)\n";
        return 0;
    }
    ::TerminateProcess(proc, 0);
    const DWORD waited = ::WaitForSingleObject(proc, 10000);
    ::CloseHandle(proc);
    (void)waited;
    std::cout << "resource-governor stopped (pid " << pid << ")\n";
    return 0;
}

int start_governor(const CliOptions& opt, const fs::path& root,
                   const fs::path& rules_path, const fs::path& default_rules,
                   const fs::path& lock_file, const fs::path& state_file,
                   const fs::path& log_file) {
    if (lock_is_active(lock_file)) {
        std::cout << "resource-governor already running\n";
        return 0;
    }
    const std::vector<std::string> parts =
        spawn_parts(opt, rules_path, default_rules, root);
    const std::wstring cmdline = widen_str(join_args(parts));
    STARTUPINFOW si{};
    PROCESS_INFORMATION pi{};
    si.cb = sizeof(si);
    std::wstring mutable_cmd = cmdline;
    std::wstring workdir = root.wstring();
    if (!::CreateProcessW(nullptr, mutable_cmd.data(), nullptr, nullptr, FALSE,
                          DETACHED_PROCESS | CREATE_NO_WINDOW, nullptr,
                          workdir.c_str(), &si, &pi)) {
        std::cout << "failed to start resource-governor\n";
        return 1;
    }
    ::CloseHandle(pi.hProcess);
    ::CloseHandle(pi.hThread);
    std::this_thread::sleep_for(std::chrono::seconds(2));
    print_status(lock_file, state_file, log_file);
    return 0;
}

int run_once(const governor::GovernorConfig& config,
             governor::CycleContext& ctx, governor::IEngine& engine,
             const fs::path& rules_path, const fs::path& state_file,
             const fs::path& log_file) {
    std::this_thread::sleep_for(std::chrono::duration<double>(
        std::max(1.0, std::min(config.interval, 5.0))));
    ctx.now_mono = monotonic_seconds();
    governor::RecordMap records;
    governor::RegState regulation;
    std::vector<jsonlite::JsonValue> logs;
    const governor::RulesDoc rules = load_rules_file(rules_path);
    const governor::Snapshot snap =
        governor::govern_once(config, rules, engine, records, regulation, ctx, logs);
    emit_logs(log_file, logs);
    write_state(state_file, snap);
    std::cout << jsonlite::json_serialize(governor::snapshot_to_json(snap))
              << "\n";
    return 0;
}

int run_watch(const governor::GovernorConfig& config,
              governor::CycleContext& ctx, governor::IEngine& engine,
              const fs::path& rules_path, const fs::path& state_file,
              const fs::path& log_file, const fs::path& lock_file) {
    /* --watch：唯一常駐行程，由 --start 以鎖檔防重複啟動。 */
    LockGuard lock;
    if (!lock.acquire(lock_file)) {
        std::cout << "governor already running (lock-busy:resource-governor.lock)\n";
        return 0;
    }
    ::SetPriorityClass(::GetCurrentProcess(), IDLE_PRIORITY_CLASS);
    ::SetConsoleCtrlHandler(ctrl_handler, TRUE);
    std::cout << "resource-governor watching (interval=" << config.interval
              << "s); Ctrl+C to stop\n";
    governor::RecordMap records;
    governor::RegState regulation;
    while (g_running) {
        watch_cycle(config, ctx, engine, records, regulation, rules_path,
                    state_file, log_file);
        const double deadline = monotonic_seconds() + config.interval;
        while (g_running && monotonic_seconds() < deadline)
            std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }
    write_state(state_file, governor::Snapshot{}, true, "signal");
    return 0;
}

}  // namespace governor_host

/* governor_tasks.cpp — 排程任務／登入啟動註冊與解除實作。
 *
 * 由 main.cpp 依 A185 拆分而來；語義不變。
 */
#include "governor_tasks.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <fstream>
#include <iostream>
#include <iterator>

#include "governor_host.h"

#pragma comment(lib, "advapi32.lib")

namespace governor_host {
namespace {

constexpr const wchar_t* kTaskName = L"GPTBridge-ResourceGovernor";
constexpr const wchar_t* kRunValue = L"GPTBridge-ResourceGovernor";
constexpr const wchar_t* kRunHive = L"Software\\Microsoft\\Windows\\CurrentVersion\\Run";

/* 字串轉義：schtasks XML / Run 值 / CreateProcess 命令列。 */
std::string quote_arg(const std::string& arg) {
    if (arg.find_first_of(" \t\"") == std::string::npos) return arg;
    std::string out = "\"";
    for (char ch : arg) {
        if (ch == '"') out += "\\\"";
        out += ch;
    }
    out += "\"";
    return out;
}

std::string xml_escape(const std::string& text) {
    std::string out;
    for (char ch : text) {
        switch (ch) {
            case '&': out += "&amp;"; break;
            case '<': out += "&lt;"; break;
            case '>': out += "&gt;"; break;
            case '"': out += "&quot;"; break;
            default: out += ch;
        }
    }
    return out;
}

std::vector<std::string> feature_args(const CliOptions& opt,
                                      const fs::path& rules_path,
                                      const fs::path& default_rules) {
    std::vector<std::string> out;
    auto tri = [&](const char* name, const std::optional<bool>& flag) {
        if (!flag.has_value()) return;
        out.push_back(std::string(*flag ? "--" : "--no-") + name);
    };
    tri("probalance", opt.probalance);
    tri("cpu-limiter", opt.cpu_limiter);
    tri("background-mode", opt.background_mode);
    tri("ecoqos", opt.ecoqos);
    tri("worker-job-cap", opt.worker_job_cap);
    if (opt.limiter_percent.has_value()) {
        out.push_back("--limiter-percent");
        out.push_back(std::to_string(*opt.limiter_percent));
    }
    if (opt.resp_ratio.has_value()) {
        out.push_back("--resp-ratio");
        out.push_back(std::to_string(*opt.resp_ratio));
    }
    if (opt.worker_job_percent.has_value()) {
        out.push_back("--worker-job-percent");
        out.push_back(std::to_string(*opt.worker_job_percent));
    }
    if (rules_path != default_rules) {
        out.push_back("--rules");
        out.push_back(path_u8(rules_path));
    }
    return out;
}

/* schtasks 子行程：launched=false 表示 CreateProcessW 失敗。 */
bool run_schtasks(std::wstring& cmd, DWORD& code) {
    STARTUPINFOW si{};
    PROCESS_INFORMATION pi{};
    si.cb = sizeof(si);
    code = 1;
    if (!::CreateProcessW(nullptr, cmd.data(), nullptr, nullptr, FALSE,
                          CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi))
        return false;
    ::WaitForSingleObject(pi.hProcess, INFINITE);
    ::GetExitCodeProcess(pi.hProcess, &code);
    ::CloseHandle(pi.hProcess);
    ::CloseHandle(pi.hThread);
    return true;
}

/* UTF-16 schtasks XML（與 Python tempfile utf-16 寫法等價：BOM＋LE）。 */
std::string task_xml(const fs::path& root, std::span<const std::string> parts) {
    wchar_t exe[MAX_PATH * 4]{};
    ::GetModuleFileNameW(nullptr, exe, static_cast<DWORD>(std::size(exe)));
    return
        "<?xml version=\"1.0\" encoding=\"UTF-16\"?>\n"
        "<Task version=\"1.4\" "
        "xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        "    <Principal id=\"InteractiveUser\">\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <RestartOnFailure>\n"
        "      <Interval>PT1M</Interval>\n"
        "      <Count>999</Count>\n"
        "    </RestartOnFailure>\n"
        "  </Settings>\n"
        "  <Actions Context=\"InteractiveUser\">\n"
        "    <Exec>\n"
        "      <Command>" +
        xml_escape(narrow_str(exe)) +
        "</Command>\n"
        "      <Arguments>" +
        xml_escape(join_args(parts)) +
        "</Arguments>\n"
        "      <WorkingDirectory>" +
        xml_escape(path_u8(root)) +
        "</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n";
}

}  // namespace

std::string join_args(std::span<const std::string> args) {
    std::string out;
    for (const auto& arg : args) {
        if (!out.empty()) out += " ";
        out += quote_arg(arg);
    }
    return out;
}

std::vector<std::string> launch_parts(const CliOptions& opt,
                                      const fs::path& rules_path,
                                      const fs::path& default_rules,
                                      const fs::path& root) {
    std::vector<std::string> parts;
    parts.push_back("--watch");
    parts.push_back("--interval");
    parts.push_back(std::to_string(opt.interval));
    for (auto& extra : feature_args(opt, rules_path, default_rules))
        parts.push_back(extra);
    if (opt.log_samples) parts.push_back("--log-samples");
    parts.push_back("--root");
    parts.push_back(path_u8(root));
    return parts;
}

std::vector<std::string> spawn_parts(const CliOptions& opt,
                                     const fs::path& rules_path,
                                     const fs::path& default_rules,
                                     const fs::path& root) {
    wchar_t exe[MAX_PATH * 4]{};
    ::GetModuleFileNameW(nullptr, exe, static_cast<DWORD>(std::size(exe)));
    std::vector<std::string> parts;
    parts.push_back(narrow_str(exe));
    for (auto& extra : launch_parts(opt, rules_path, default_rules, root))
        parts.push_back(extra);
    return parts;
}

int uninstall_task() {
    /* 任務不存在亦視為成功——與 Python 版等價（Query 先判斷存在性）。 */
    std::wstring query = L"schtasks /Query /TN ";
    query += kTaskName;
    DWORD query_code = 1;
    run_schtasks(query, query_code);
    DWORD code = 0;
    if (query_code == 0) {
        std::wstring cmd =
            L"schtasks /Delete /TN " + std::wstring(kTaskName) + L" /F";
        if (!run_schtasks(cmd, code)) {
            std::cout << "task removal failed to launch schtasks\n";
            return 1;
        }
    }
    if (code != 0) {
        std::cout << "task removal failed\n";
        return 1;
    }
    std::cout << "task removed: GPTBridge-ResourceGovernor\n";
    return 0;
}

int uninstall_logon() {
    HKEY key = nullptr;
    LONG rc = ::RegOpenKeyExW(HKEY_CURRENT_USER, kRunHive, 0, KEY_SET_VALUE, &key);
    if (rc != ERROR_SUCCESS) {
        std::cout << "logon removal failed: cannot open Run key\n";
        return 1;
    }
    rc = ::RegDeleteValueW(key, kRunValue);
    ::RegCloseKey(key);
    if (rc != ERROR_SUCCESS && rc != ERROR_FILE_NOT_FOUND) {
        std::cout << "logon removal failed\n";
        return 1;
    }
    std::cout << "logon registration removed: GPTBridge-ResourceGovernor\n";
    return 0;
}

int install_task(const CliOptions& opt, const fs::path& root,
                 const fs::path& rules_path, const fs::path& default_rules) {
    wchar_t tmp_dir[MAX_PATH]{};
    ::GetTempPathW(static_cast<DWORD>(std::size(tmp_dir)), tmp_dir);
    wchar_t tmp_file[MAX_PATH]{};
    ::GetTempFileNameW(tmp_dir, L"gov", 0, tmp_file);
    const std::vector<std::string> parts =
        launch_parts(opt, rules_path, default_rules, root);
    const std::string xml = task_xml(root, parts);
    {
        std::ofstream out(tmp_file, std::ios::binary);
        const std::wstring wide = widen_str(xml);
        /* UTF-16LE BOM + 寬字元，與 Python tempfile utf-16 等價。 */
        const unsigned char bom[2] = {0xFF, 0xFE};
        out.write(reinterpret_cast<const char*>(bom), 2);
        out.write(reinterpret_cast<const char*>(wide.c_str()),
                  static_cast<std::streamsize>(wide.size() * sizeof(wchar_t)));
    }
    std::wstring cmd = L"schtasks /Create /TN ";
    cmd += kTaskName;
    cmd += L" /XML \"";
    cmd += tmp_file;
    cmd += L"\" /F";
    DWORD code = 1;
    run_schtasks(cmd, code);
    ::DeleteFileW(tmp_file);
    if (code != 0) {
        std::cout << "task registration failed\n";
        return 1;
    }
    std::cout << "task registered: GPTBridge-ResourceGovernor\n";
    return 0;
}

int install_logon(const CliOptions& opt, const fs::path& root,
                  const fs::path& rules_path, const fs::path& default_rules) {
    const std::vector<std::string> parts =
        spawn_parts(opt, rules_path, default_rules, root);
    const std::string value = join_args(parts);
    HKEY key = nullptr;
    LONG rc = ::RegOpenKeyExW(HKEY_CURRENT_USER, kRunHive, 0, KEY_SET_VALUE, &key);
    if (rc != ERROR_SUCCESS) {
        std::cout << "logon registration failed: cannot open Run key\n";
        return 1;
    }
    const std::wstring wide = widen_str(value);
    rc = ::RegSetValueExW(key, kRunValue, 0, REG_SZ,
                          reinterpret_cast<const BYTE*>(wide.c_str()),
                          static_cast<DWORD>((wide.size() + 1) * sizeof(wchar_t)));
    ::RegCloseKey(key);
    if (rc != ERROR_SUCCESS) {
        std::cout << "logon registration failed\n";
        return 1;
    }
    std::cout << "logon registration active: GPTBridge-ResourceGovernor\n";
    return 0;
}

}  // namespace governor_host

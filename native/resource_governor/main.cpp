/* main.cpp ??resource-governor.exe ?脣暺?C++23嚗? *
 * CLI ??Python ??璅摰對?
 *   --once / --watch / --dry-run / --start / --stop / --status
 *   --install-task / --uninstall-task / --install-logon / --uninstall-logon
 *   --interval / --cpu-busy / --cpu-extreme / --mem-trim-mb / --sustain
 *   --no-affinity / --log-samples / --rules
 *   --probalance[=off] / --cpu-limiter / --background-mode / --ecoqos
 *   --limiter-percent / --resp-ratio / --worker-job-cap / --worker-job-percent
 *   --root嚗++ ?啣?嚗澈?寧??撖恬??身??exe 雿蔭???Ｘ葫嚗? *
 * ????亥?/?楝敺? Python ???函??敺垢 signal 璅∠??⊿?靽格?? */
#include "resource_governor.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <rpc.h>
#include <rpcdce.h>
#include <shellapi.h>
#include <tlhelp32.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <optional>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#pragma comment(lib, "rpcrt4.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "user32.lib")

namespace governor = gptbridge::governor;
namespace jsonlite = gptbridge::jsonlite;
namespace fs = std::filesystem;

namespace {

constexpr const wchar_t* kTaskName = L"GPTBridge-ResourceGovernor";
constexpr const wchar_t* kRunValue = L"GPTBridge-ResourceGovernor";
constexpr const wchar_t* kRunHive = L"Software\\Microsoft\\Windows\\CurrentVersion\\Run";

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
    double interval = governor::kDefaultInterval;
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

std::string narrow_str(const std::wstring& text) {
    if (text.empty()) return {};
    const int needed =
        ::WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<std::size_t>(needed > 0 ? needed - 1 : 0), '\0');
    if (needed > 0)
        ::WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, out.data(), needed, nullptr,
                              nullptr);
    return out;
}

std::string narrow_str(const wchar_t* text) {
    if (text == nullptr || *text == L'\0') return {};
    return narrow_str(std::wstring(text));
}

/* fs::path ??UTF-8嚗++20 韏?u8string() ??char8_t嚗??湔雿輻嚗?*/
std::string path_u8(const fs::path& path) { return narrow_str(path.wstring()); }

std::wstring widen_str(const std::string& text) {
    if (text.empty()) return L"";
    const int needed = ::MultiByteToWideChar(CP_UTF8, 0, text.c_str(), -1, nullptr, 0);
    std::wstring out(static_cast<std::size_t>(needed > 0 ? needed - 1 : 0), L'\0');
    if (needed > 0)
        ::MultiByteToWideChar(CP_UTF8, 0, text.c_str(), -1, out.data(), needed);
    return out;
}

std::vector<std::string> argv_utf8() {
    int argc = 0;
    LPWSTR* raw = ::CommandLineToArgvW(::GetCommandLineW(), &argc);
    std::vector<std::string> out;
    if (raw != nullptr) {
        for (int i = 0; i < argc; ++i) out.push_back(narrow_str(raw[i]));
        ::LocalFree(raw);
    }
    return out;
}

bool parse_number(const std::string& text, double& out) {
    try {
        std::size_t used = 0;
        out = std::stod(text, &used);
        return used == text.size();
    } catch (...) {
        return false;
    }
}

bool parse_int(const std::string& text, int& out) {
    try {
        std::size_t used = 0;
        out = std::stoi(text, &used);
        return used == text.size();
    } catch (...) {
        return false;
    }
}

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

governor::GovernorConfig to_config(const CliOptions& opt, const fs::path& default_rules) {
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

/* ---------------- 頝臬? ---------------- */
fs::path discover_root(const std::string& override_root) {
    if (!override_root.empty()) return fs::path(widen_str(override_root));
    wchar_t exe[MAX_PATH * 4]{};
    ::GetModuleFileNameW(nullptr, exe, static_cast<DWORD>(std::size(exe)));
    fs::path dir = fs::path(exe).parent_path();
    for (int depth = 0; depth < 8; ++depth) {
        if (fs::exists(dir / "main-system" / "config" / "resource-governor-rules.json"))
            return dir;
        if (!dir.has_parent_path()) break;
        dir = dir.parent_path();
    }
    return fs::current_path();
}

/* ---------------- 瑼? IO ---------------- */
std::string read_file_text(const fs::path& path, bool& ok) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        ok = false;
        return {};
    }
    std::ostringstream ss;
    ss << input.rdbuf();
    ok = true;
    return ss.str();
}

bool write_atomic(const fs::path& path, const std::string& text) {
    std::error_code ec;
    fs::create_directories(path.parent_path(), ec);
    const fs::path tmp = path.wstring() + L".tmp";
    {
        std::ofstream output(tmp, std::ios::binary | std::ios::trunc);
        if (!output) return false;
        output << text;
        output.flush();
        if (!output) return false;
    }
    fs::rename(tmp, path, ec);
    if (ec) {
        fs::remove(tmp, ec);
        return false;
    }
    return true;
}

void append_log(const fs::path& path, const std::string& line) {
    std::error_code ec;
    fs::create_directories(path.parent_path(), ec);
    std::ofstream output(path, std::ios::binary | std::ios::app);
    if (output) output << line << "\n";
}

std::string utc_now_iso() {
    SYSTEMTIME st{};
    ::GetSystemTime(&st);
    char buffer[64]{};
    std::snprintf(buffer, sizeof(buffer), "%04u-%02u-%02uT%02u:%02u:%02u.%03u+00:00",
                  st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond,
                  st.wMilliseconds);
    return buffer;
}

double monotonic_seconds() {
    return static_cast<double>(::GetTickCount64()) / 1000.0;
}

/* ---------------- ????process_lock.py 隤儔銝?湛? ---------------- */
bool pid_alive(unsigned long pid) {
    HANDLE proc = ::OpenProcess(0x1000 /*QUERY_LIMITED_INFORMATION*/, FALSE, pid);
    if (proc != nullptr) {
        DWORD code = 0;
        ::GetExitCodeProcess(proc, &code);
        ::CloseHandle(proc);
        return code == 259 /*STILL_ACTIVE*/;
    }
    return ::GetLastError() != 87 /*ERROR_INVALID_PARAMETER*/;
}

bool owner_alive(const fs::path& lock_path) {
    std::error_code ec;
    const auto mtime = fs::last_write_time(lock_path, ec);
    double age = 0.0;
    if (!ec) {
        const auto now = fs::file_time_type::clock::now();
        age = std::chrono::duration<double>(now - mtime).count();
        if (age < 0) age = 0;
    } else {
        return false;
    }
    bool ok = false;
    const std::string text = read_file_text(lock_path, ok);
    if (ok) {
        try {
            jsonlite::JsonValue root =
                jsonlite::JsonParser(text).parse();
            if (const jsonlite::JsonValue* pid_value = root.get("pid");
                pid_value != nullptr &&
                pid_value->type == jsonlite::JsonValue::Type::Number) {
                const auto pid =
                    static_cast<unsigned long>(pid_value->number);
                return pid_alive(pid);
            }
        } catch (const jsonlite::JsonError&) {
        }
    }
    return age < 10.0;
}

bool lock_is_active(const fs::path& lock_path) {
    std::error_code ec;
    return fs::exists(lock_path, ec) && owner_alive(lock_path);
}

struct LockGuard {
    fs::path path;
    HANDLE handle = nullptr;
    std::string token;
    bool owned = false;

    static std::string make_token() {
        UUID uuid{};
        ::UuidCreate(&uuid);
        RPC_WSTR text = nullptr;
        std::string out;
        if (::UuidToStringW(&uuid, &text) == RPC_S_OK && text != nullptr) {
            out = narrow_str(reinterpret_cast<const wchar_t*>(text));
            ::RpcStringFreeW(&text);
        }
        if (out.empty()) {
            char buffer[64]{};
            std::snprintf(buffer, sizeof(buffer), "%lu-%llu-%u",
                          ::GetCurrentProcessId(),
                          static_cast<unsigned long long>(::GetTickCount64()),
                          static_cast<unsigned>(::GetTickCount()));
            out = buffer;
        }
        for (char& ch : out)
            if (ch == '-') ch = 'x';
        return out;
    }

    bool acquire(const fs::path& lock_path) {
        path = lock_path;
        std::error_code ec;
        fs::create_directories(path.parent_path(), ec);
        for (int attempt = 0; attempt < 2; ++attempt) {
            HANDLE created = ::CreateFileW(
                path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                FILE_ATTRIBUTE_NORMAL, nullptr);
            if (created != INVALID_HANDLE_VALUE) {
                handle = created;
                token = make_token();
                char payload[160]{};
                const int len = std::snprintf(
                    payload, sizeof(payload), "{\"pid\": %lu, \"token\": \"%s\"}",
                    ::GetCurrentProcessId(), token.c_str());
                DWORD written = 0;
                ::WriteFile(handle, payload, static_cast<DWORD>(len), &written,
                            nullptr);
                ::FlushFileBuffers(handle);
                owned = true;
                return true;
            }
            if (owner_alive(path) || attempt == 1) return false;
            fs::remove(path, ec);
        }
        return false;
    }

    ~LockGuard() {
        if (handle != nullptr) {
            ::CloseHandle(handle);
            handle = nullptr;
        }
        if (owned) {
            bool ok = false;
            const std::string text = read_file_text(path, ok);
            if (ok) {
                try {
                    jsonlite::JsonValue root =
                        jsonlite::JsonParser(text).parse();
                    if (const jsonlite::JsonValue* token_value = root.get("token");
                        token_value != nullptr &&
                        token_value->type == jsonlite::JsonValue::Type::String &&
                        token_value->string == token) {
                        std::error_code ec;
                        fs::remove(path, ec);
                    }
                } catch (const jsonlite::JsonError&) {
                }
            }
            owned = false;
        }
    }
};

/* ---------------- ?芾澈鞈? ---------------- */
std::string self_account() {
    HANDLE token = nullptr;
    if (!::OpenProcessToken(::GetCurrentProcess(), TOKEN_QUERY, &token))
        return {};
    std::string account;
    DWORD needed = 0;
    ::GetTokenInformation(token, TokenUser, nullptr, 0, &needed);
    std::vector<std::uint8_t> buffer(needed > 0 ? needed : 1);
    if (::GetTokenInformation(token, TokenUser, buffer.data(), needed, &needed)) {
        const TOKEN_USER* user = reinterpret_cast<const TOKEN_USER*>(buffer.data());
        wchar_t name[256]{};
        wchar_t domain[256]{};
        DWORD name_len = static_cast<DWORD>(std::size(name));
        DWORD domain_len = static_cast<DWORD>(std::size(domain));
        SID_NAME_USE use = SidTypeUnknown;
        if (::LookupAccountSidW(nullptr, user->User.Sid, name, &name_len, domain,
                                &domain_len, &use)) {
            account = narrow_str(domain);
            if (!account.empty()) account += "\\";
            account += narrow_str(name);
        }
    }
    ::CloseHandle(token);
    return account;
}

std::set<int> self_tree() {
    std::set<int> tree;
    const DWORD self = ::GetCurrentProcessId();
    tree.insert(static_cast<int>(self));
    HANDLE snap =
        ::CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return tree;
    std::map<DWORD, DWORD> parent_of;
    PROCESSENTRY32W entry{};
    entry.dwSize = sizeof(entry);
    if (::Process32FirstW(snap, &entry)) {
        do {
            parent_of[entry.th32ProcessID] = entry.th32ParentProcessID;
        } while (::Process32NextW(snap, &entry));
    }
    ::CloseHandle(snap);
    DWORD cursor = self;
    for (int depth = 0; depth < 64; ++depth) {
        auto it = parent_of.find(cursor);
        if (it == parent_of.end() || it->second == 0 || it->second == cursor) break;
        tree.insert(static_cast<int>(it->second));
        cursor = it->second;
    }
    return tree;
}

bool env_disabled() {
    wchar_t buffer[16]{};
    DWORD len = ::GetEnvironmentVariableW(L"GPTBRIDGE_GOVERNOR_DISABLE", buffer,
                                          static_cast<DWORD>(std::size(buffer)));
    if (len == 0 || len >= std::size(buffer)) return false;
    std::string value = governor::to_lower(narrow_str(buffer));
    return value == "1" || value == "true" || value == "yes";
}

governor::RulesDoc load_rules_file(const fs::path& rules_path) {
    std::error_code ec;
    if (!fs::exists(rules_path, ec)) return governor::RulesDoc{};
    bool ok = false;
    const std::string text = read_file_text(rules_path, ok);
    if (!ok) {
        governor::RulesDoc doc;
        doc.error = "OSError: cannot read rules file";
        return doc;
    }
    auto parsed = governor::parse_rules(text);
    if (!parsed.has_value()) {
        governor::RulesDoc doc;
        doc.error = parsed.error();
        return doc;
    }
    return std::move(*parsed);
}

void emit_logs(const fs::path& log_path,
               std::span<const jsonlite::JsonValue> entries) {
    const std::string stamp = utc_now_iso();
    jsonlite::JsonValue stamp_value;
    stamp_value.type = jsonlite::JsonValue::Type::String;
    stamp_value.string = stamp;
    for (const auto& entry : entries) {
        jsonlite::JsonValue with_at = entry;
        if (with_at.type == jsonlite::JsonValue::Type::Object)
            with_at.object.insert(with_at.object.begin(), {"at", stamp_value});
        append_log(log_path, jsonlite::json_serialize(with_at));
    }
}

void write_state(const fs::path& state_path, const governor::Snapshot& snap,
                 bool stopped = false, const std::string& reason = {}) {
    using namespace governor::detail;
    jsonlite::JsonValue body = governor::snapshot_to_json(snap);
    if (stopped) {
        body = jobj({{"stopped", jbool(true)}, {"reason", jstr(reason)}});
    }
    if (body.type == jsonlite::JsonValue::Type::Object)
        body.object.insert(body.object.begin(), {"at", jstr(utc_now_iso())});
    write_atomic(state_path, jsonlite::json_serialize(body));
}

/* 撘摮葡嚗chtasks XML / Run ??/ CreateProcess ?賭誘??剁???*/
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

std::string join_args(std::span<const std::string> args) {
    std::string out;
    for (const auto& arg : args) {
        if (!out.empty()) out += " ";
        out += quote_arg(arg);
    }
    return out;
}

std::atomic<bool> g_running{true};

BOOL WINAPI ctrl_handler(DWORD event) {
    if (event == CTRL_C_EVENT || event == CTRL_BREAK_EVENT ||
        event == CTRL_CLOSE_EVENT) {
        g_running = false;
        return TRUE;
    }
    return FALSE;
}

}  // namespace

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

    if (opt.uninstall_task) {
        /* 銝??函?隞餃?閬撌脩宏?歹???Python 銝?湛???Query嚗?摮?單?????*/
        std::wstring query = L"schtasks /Query /TN ";
        query += kTaskName;
        STARTUPINFOW qsi{};
        PROCESS_INFORMATION qpi{};
        qsi.cb = sizeof(qsi);
        std::wstring mutable_query = query;
        DWORD query_code = 1;
        if (::CreateProcessW(nullptr, mutable_query.data(), nullptr, nullptr, FALSE,
                              CREATE_NO_WINDOW, nullptr, nullptr, &qsi, &qpi)) {
            ::WaitForSingleObject(qpi.hProcess, INFINITE);
            ::GetExitCodeProcess(qpi.hProcess, &query_code);
            ::CloseHandle(qpi.hProcess);
            ::CloseHandle(qpi.hThread);
        }
        DWORD code = 0;
        if (query_code == 0) {
            std::wstring cmd =
                L"schtasks /Delete /TN " + std::wstring(kTaskName) + L" /F";
            STARTUPINFOW si{};
            PROCESS_INFORMATION pi{};
            si.cb = sizeof(si);
            std::wstring mutable_cmd = cmd;
            if (!::CreateProcessW(nullptr, mutable_cmd.data(), nullptr, nullptr,
                                  FALSE, CREATE_NO_WINDOW, nullptr, nullptr, &si,
                                  &pi)) {
                std::cout << "task removal failed to launch schtasks\n";
                return 1;
            }
            ::WaitForSingleObject(pi.hProcess, INFINITE);
            ::GetExitCodeProcess(pi.hProcess, &code);
            ::CloseHandle(pi.hProcess);
            ::CloseHandle(pi.hThread);
        }
        if (code != 0) {
            std::cout << "task removal failed\n";
            return 1;
        }
        std::cout << "task removed: GPTBridge-ResourceGovernor\n";
        return 0;
    }
    if (opt.uninstall_logon) {
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

    governor::GovernorConfig config = to_config(opt, default_rules);
    const fs::path rules_path = widen_str(config.rules_path);

    auto launch_parts = [&]() {
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
    };
    auto spawn_parts = [&]() {
        wchar_t exe[MAX_PATH * 4]{};
        ::GetModuleFileNameW(nullptr, exe, static_cast<DWORD>(std::size(exe)));
        std::vector<std::string> parts;
        parts.push_back(narrow_str(exe));
        for (auto& extra : launch_parts()) parts.push_back(extra);
        return parts;
    };

    if (opt.install_task) {
        wchar_t tmp_dir[MAX_PATH]{};
        ::GetTempPathW(static_cast<DWORD>(std::size(tmp_dir)), tmp_dir);
        wchar_t tmp_file[MAX_PATH]{};
        ::GetTempFileNameW(tmp_dir, L"gov", 0, tmp_file);
        const std::vector<std::string> parts = launch_parts();
        wchar_t exe[MAX_PATH * 4]{};
        ::GetModuleFileNameW(nullptr, exe, static_cast<DWORD>(std::size(exe)));
        std::string xml =
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
        {
            std::ofstream out(tmp_file, std::ios::binary);
            const std::wstring wide = widen_str(xml);
            /* UTF-16LE BOM + ?批捆嚗? Python tempfile utf-16 銝?湛???*/
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
        STARTUPINFOW si{};
        PROCESS_INFORMATION pi{};
        si.cb = sizeof(si);
        BOOL launched = ::CreateProcessW(nullptr, cmd.data(), nullptr, nullptr, FALSE,
                                         CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi);
        DWORD code = 1;
        if (launched) {
            ::WaitForSingleObject(pi.hProcess, INFINITE);
            ::GetExitCodeProcess(pi.hProcess, &code);
            ::CloseHandle(pi.hProcess);
            ::CloseHandle(pi.hThread);
        }
        ::DeleteFileW(tmp_file);
        if (code != 0) {
            std::cout << "task registration failed\n";
            return 1;
        }
        std::cout << "task registered: GPTBridge-ResourceGovernor\n";
        return 0;
    }
    if (opt.install_logon) {
        const std::vector<std::string> parts = spawn_parts();
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
    if (opt.stop) {
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
    auto print_status = [&]() {
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
    };

    if (opt.start) {
        if (lock_is_active(lock_file)) {
            std::cout << "resource-governor already running\n";
            return 0;
        }
        const std::vector<std::string> parts = spawn_parts();
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
        print_status();
        return 0;
    }

    auto engine = governor::make_windows_engine();
    if (engine == nullptr) {
        std::cout << "process engine unavailable\n";
        return 1;
    }

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

    /* ?嚗? Python --once/--watch ?璅???銝?湛???*/
    engine->enumerate();
    engine->system();

    if (opt.status || (!opt.once && !opt.watch)) {
        print_status();
        return 0;
    }

    if (opt.once) {
        std::this_thread::sleep_for(std::chrono::duration<double>(
            std::max(1.0, std::min(config.interval, 5.0))));
        ctx.now_mono = monotonic_seconds();
        governor::RecordMap records;
        governor::RegState regulation;
        std::vector<jsonlite::JsonValue> logs;
        const governor::RulesDoc rules = load_rules_file(rules_path);
        const governor::Snapshot snap =
            governor::govern_once(config, rules, *engine, records, regulation, ctx, logs);
        emit_logs(log_file, logs);
        write_state(state_file, snap);
        std::cout << jsonlite::json_serialize(governor::snapshot_to_json(snap))
                  << "\n";
        return 0;
    }

    /* --watch嚗??舐??艘??--start 撌脫銝?銵?銝血??梁?????*/
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
        try {
            ctx.now_mono = monotonic_seconds();
            const governor::RulesDoc rules = load_rules_file(rules_path);
            std::vector<jsonlite::JsonValue> logs;
            const governor::Snapshot snap = governor::govern_once(
                config, rules, *engine, records, regulation, ctx, logs);
            emit_logs(log_file, logs);
            write_state(state_file, snap);
        } catch (const std::exception& error) {
            using namespace governor::detail;
            const std::vector<jsonlite::JsonValue> single = {
                jobj({{"action", jstr("cycle-error")},
                      {"error", jstr(std::string("exception: ") + error.what())}})};
            emit_logs(log_file, single);
        } catch (...) {
            using namespace governor::detail;
            const std::vector<jsonlite::JsonValue> single = {
                jobj({{"action", jstr("cycle-error")},
                      {"error", jstr("unknown exception")}})};
            emit_logs(log_file, single);
        }
        const double deadline = monotonic_seconds() + config.interval;
        while (g_running && monotonic_seconds() < deadline)
            std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }
    write_state(state_file, governor::Snapshot{}, true, "signal");
    return 0;
}

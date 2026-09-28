/* governor_engine_win32.cpp — WindowsEngine 列舉 / 系統 / 探針。
 *
 * 由 resource_governor.cpp 依 A185 拆分而來；語義不變：
 *  Toolhelp32 列舉＋GetModuleFileNameEx/GetProcessImageFileName、NT 命令列、
 *  Token SID 擁有者、GetProcessTimes 增量 CPU、working-set/IO 計數。
 *  RAII：Handle 守衛 OS 句柄；Job 句柄以 map 持有、解構全數釋放。
 */
#include "governor_engine_win32.h"

#include <psapi.h>

#include <algorithm>
#include <chrono>
#include <iterator>
#include <memory>
#include <set>
#include <vector>

#pragma comment(lib, "psapi.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "user32.lib")

namespace gptbridge {
namespace governor {

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

void WindowsEngine::fill_exe(HANDLE proc, const PROCESSENTRY32W& entry,
                             ProcSample& sample) {
    wchar_t path[MAX_PATH * 2]{};
    DWORD path_len = static_cast<DWORD>(std::size(path));
    if (::GetModuleFileNameExW(proc, nullptr, path, path_len) != 0) {
        sample.exe = narrow(path);
    } else {
        wchar_t device[MAX_PATH * 2]{};
        DWORD device_len = static_cast<DWORD>(std::size(device));
        if (::GetProcessImageFileNameW(proc, device, device_len) != 0)
            sample.exe = narrow(device);
    }
    sample.name = sample.exe.empty() ? narrow(entry.szExeFile)
                                     : base_name(sample.exe);
}

/* 命令列（NT 內部資訊類，失敗則留空 —— 分類降級而非中斷）。 */
void WindowsEngine::fill_cmdline(HANDLE proc, ProcSample& sample) {
    auto query = nt_query_info();
    if (query == nullptr) return;
    struct RemoteString {
        USHORT Length = 0;
        USHORT MaximumLength = 0;
        PWSTR Buffer = nullptr;
    };
    RemoteString remote{};
    ULONG returned = 0;
    if (query(proc, 60 /*ProcessCommandLineInformation*/, &remote,
              sizeof(remote), &returned) != 0 ||
        remote.Buffer == nullptr || remote.Length == 0 ||
        remote.Length >= 32768)
        return;
    std::vector<wchar_t> buffer(static_cast<std::size_t>(remote.Length / 2) + 1,
                                L'\0');
    SIZE_T read = 0;
    if (::ReadProcessMemory(proc, remote.Buffer, buffer.data(), remote.Length,
                            &read) &&
        read > 0) {
        sample.cmdline = narrow(std::wstring(buffer.data(), read / 2));
    }
}

/* 擁有者（Token SID → DOMAIN\user；失敗留空＝不過濾）。 */
void WindowsEngine::fill_owner(HANDLE proc, ProcSample& sample) {
    HANDLE token = nullptr;
    if (!::OpenProcessToken(proc, TOKEN_QUERY, &token)) return;
    Handle token_guard(token);
    DWORD needed = 0;
    ::GetTokenInformation(token_guard.get(), TokenUser, nullptr, 0, &needed);
    std::vector<std::uint8_t> buffer(needed > 0 ? needed : 1);
    if (!::GetTokenInformation(token_guard.get(), TokenUser, buffer.data(),
                               needed, &needed))
        return;
    const TOKEN_USER* user = reinterpret_cast<const TOKEN_USER*>(buffer.data());
    wchar_t name[256]{};
    wchar_t domain[256]{};
    DWORD name_len = static_cast<DWORD>(std::size(name));
    DWORD domain_len = static_cast<DWORD>(std::size(domain));
    SID_NAME_USE use = SidTypeUnknown;
    if (::LookupAccountSidW(nullptr, user->User.Sid, name, &name_len, domain,
                            &domain_len, &use)) {
        std::string account = narrow(domain);
        if (!account.empty()) account += "\\";
        account += narrow(name);
        sample.username = std::move(account);
    }
}

/* CPU（GetProcessTimes 增量；首次見到回 0 並播種）。 */
void WindowsEngine::fill_cpu(HANDLE proc, ProcSample& sample,
                             unsigned long long wall) {
    FILETIME created{}, exited{}, kernel{}, user{};
    if (!::GetProcessTimes(proc, &created, &exited, &kernel, &user)) return;
    const unsigned long long cpu =
        filetime_to_u64(kernel) + filetime_to_u64(user);
    sample.create_ms = static_cast<std::int64_t>(filetime_to_u64(created) / 10000ULL);
    auto it = tracks_.find(sample.pid);
    if (it != tracks_.end() && wall > it->second.wall_100ns) {
        const unsigned long long dcpu =
            cpu >= it->second.cpu_100ns ? cpu - it->second.cpu_100ns : 0;
        sample.cpu_percore = 100.0 * static_cast<double>(dcpu) /
                             static_cast<double>(wall - it->second.wall_100ns);
    }
    tracks_[sample.pid] = Track{cpu, wall};
}

void WindowsEngine::fill_mem_io(HANDLE proc, ProcSample& sample) {
    PROCESS_MEMORY_COUNTERS counters{};
    if (::GetProcessMemoryInfo(proc, &counters, sizeof(counters)))
        sample.rss_mb =
            static_cast<double>(counters.WorkingSetSize) / (1024.0 * 1024.0);
    IO_COUNTERS io{};
    if (::GetProcessIoCounters(proc, &io)) {
        sample.io_read_mb =
            static_cast<double>(io.ReadTransferCount) / (1024.0 * 1024.0);
        sample.io_write_mb =
            static_cast<double>(io.WriteTransferCount) / (1024.0 * 1024.0);
    }
}

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
        fill_exe(proc.get(), entry, sample);
        fill_cmdline(proc.get(), sample);
        fill_owner(proc.get(), sample);
        fill_cpu(proc.get(), sample, wall);
        fill_mem_io(proc.get(), sample);
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
        reset_rate(job);
        ::CloseHandle(job);
    }
    jobs_.clear();
    if (shared_job_ != nullptr) {
        reset_rate(shared_job_);
        ::CloseHandle(shared_job_);
        shared_job_ = nullptr;
    }
}

std::unique_ptr<IEngine> make_windows_engine() {
    return std::make_unique<WindowsEngine>();
}

}  // namespace governor
}  // namespace gptbridge

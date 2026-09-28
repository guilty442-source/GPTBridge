/* governor_engine_actions.cpp — WindowsEngine 動作實作。
 *
 * 由 resource_governor.cpp 依 A185 拆分而來；語義不變：
 *  SetPriorityClass / AffinityMask / EmptyWorkingSet / background-equivalent
 *  （CPU＋memory＋IO priority）/ EcoQoS power throttling / Job Object
 *  CpuRate 硬上限＋共享 Job 記憶體與行程數上限。
 */
#include "governor_engine_win32.h"

#include <psapi.h>

#include <vector>

#pragma comment(lib, "psapi.lib")

namespace gptbridge {
namespace governor {

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

/* 負 pid sentinel 皆為共享 Job（-1 worker、-10..-13 各池）；其餘為逐行程 Job。 */
HANDLE WindowsEngine::job_for(const ProcKey& key) {
    auto it = jobs_.find(key);
    return it != jobs_.end() ? it->second : nullptr;
}

/* 共享 Job 建立時的一次性延伸上限（記憶體／活動行程數）。 */
void WindowsEngine::apply_shared_limits(HANDLE job, long long job_memory_bytes,
                                        int job_process_limit) {
    if (job_memory_bytes <= 0 && job_process_limit <= 0) return;
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

bool WindowsEngine::cpu_limit(const ProcKey& key, int pid, double percent,
                              long long job_memory_bytes, int job_process_limit) {
    const bool shared = (key.pid < 0);
    HANDLE job = job_for(key);
    bool created = false;
    if (job == nullptr) {
        job = ::CreateJobObjectW(nullptr, nullptr);
        if (job == nullptr) return false;
        created = true;
        if (shared) apply_shared_limits(job, job_memory_bytes, job_process_limit);
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
    if (created) jobs_.emplace(key, job);
    return true;
}

void WindowsEngine::reset_rate(HANDLE job) {
    CpuRateControl rate{0, 0};
    ::SetInformationJobObject(job, static_cast<JOBOBJECTINFOCLASS>(15), &rate,
                              sizeof(rate));
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
    reset_rate(job);
    ::CloseHandle(job);
    return true;
}

}  // namespace governor
}  // namespace gptbridge

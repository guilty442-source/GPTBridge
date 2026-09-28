/* governor_engine_win32.h — WindowsEngine 類別宣告與 Win32 共享工具。
 *
 * 由 resource_governor.cpp 依 A185（≤500 effective 行）拆分而來；僅供
 * governor_engine_win32.cpp / governor_engine_actions.cpp 使用。
 * 語義不變：RAII Handle 守衛、NT 內部資訊類動態載入、Job Object 控制。
 */
#pragma once

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <tlhelp32.h>

#include <map>
#include <string>
#include <string_view>

#include "resource_governor.h"

namespace gptbridge {
namespace governor {

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

std::wstring widen(std::string_view text);
std::string narrow(const std::wstring& text);
unsigned long long filetime_to_u64(const FILETIME& ft);

using NtSetInformationProcessFn = LONG(WINAPI*)(HANDLE, ULONG, PVOID, ULONG);
using NtQueryInformationProcessFn = LONG(WINAPI*)(HANDLE, ULONG, PVOID, ULONG, PULONG);

NtSetInformationProcessFn nt_set_info();
NtQueryInformationProcessFn nt_query_info();

struct CpuRateControl {
    DWORD ControlFlags = 0;
    DWORD CpuRate = 0;
};

/* ---------------- WindowsEngine ---------------- */
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

    /* enumerate() 逐行程填充助手（governor_engine_win32.cpp）。 */
    void fill_exe(HANDLE proc, const PROCESSENTRY32W& entry, ProcSample& sample);
    void fill_cmdline(HANDLE proc, ProcSample& sample);
    void fill_owner(HANDLE proc, ProcSample& sample);
    void fill_cpu(HANDLE proc, ProcSample& sample, unsigned long long wall);
    static void fill_mem_io(HANDLE proc, ProcSample& sample);

    /* cpu_limit() Job Object 助手（governor_engine_actions.cpp）。 */
    HANDLE job_for(const ProcKey& key);
    void apply_shared_limits(HANDLE job, long long job_memory_bytes,
                             int job_process_limit);
    void reset_rate(HANDLE job);
};

}  // namespace governor
}  // namespace gptbridge

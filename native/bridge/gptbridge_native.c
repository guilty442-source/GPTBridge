/*
 * gptbridge_native.c — sole C ABI thunk (A221/E186).
 *
 * Validates arguments, translates status/handles, delegates immediately.
 * Contains no algorithm/business/workflow/state authority (A220/E185).
 * Resource monitoring is a platform API call, not compute — it lives in
 * the bridge, not in native/core/. Compute functions declared by the public
 * header are implemented by the pure-C cores in native/core/.
 */
#include "gptbridge_native.h"

#include <stdio.h>
#include <string.h>
#include <wchar.h>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <psapi.h>
#include <tlhelp32.h>
#include <iphlpapi.h>
#include <winternl.h>
#pragma comment(lib, "advapi32.lib")
#else
#include <signal.h>
#include <time.h>
#include <unistd.h>
#endif

int gptbridge_native_is_windows(void) {
#ifdef _WIN32
    return 1;
#else
    return 0;
#endif
}

double gptbridge_native_monotonic_seconds(void) {
#ifdef _WIN32
    static LARGE_INTEGER frequency = {0};
    if (frequency.QuadPart == 0) {
        QueryPerformanceFrequency(&frequency);
    }
    LARGE_INTEGER counter;
    QueryPerformanceCounter(&counter);
    if (frequency.QuadPart == 0) {
        return 0.0;
    }
    return (double)counter.QuadPart / (double)frequency.QuadPart;
#else
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        return 0.0;
    }
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
#endif
}

int64_t gptbridge_native_working_set_bytes(void) {
#ifdef _WIN32
    PROCESS_MEMORY_COUNTERS counters;
    ZeroMemory(&counters, sizeof(counters));
    if (GetProcessMemoryInfo(
            GetCurrentProcess(), &counters, sizeof(counters)) == 0) {
        return -1;
    }
    return (int64_t)counters.WorkingSetSize;
#else
    return -1;
#endif
}

int64_t gptbridge_native_private_bytes(void) {
#ifdef _WIN32
    PROCESS_MEMORY_COUNTERS_EX counters;
    ZeroMemory(&counters, sizeof(counters));
    counters.cb = sizeof(counters);
    if (GetProcessMemoryInfo(
            GetCurrentProcess(),
            (PPROCESS_MEMORY_COUNTERS)&counters,
            sizeof(counters)) == 0) {
        return -1;
    }
    return (int64_t)counters.PrivateUsage;
#else
    return -1;
#endif
}

int gptbridge_native_release_working_set(void) {
#ifdef _WIN32
    return EmptyWorkingSet(GetCurrentProcess()) != 0 ? 1 : 0;
#else
    return 0;
#endif
}

int64_t gptbridge_native_system_memory_total_bytes(void) {
#ifdef _WIN32
    MEMORYSTATUSEX status;
    ZeroMemory(&status, sizeof(status));
    status.dwLength = sizeof(status);
    if (GlobalMemoryStatusEx(&status) == 0) {
        return -1;
    }
    return (int64_t)status.ullTotalPhys;
#else
    return -1;
#endif
}

int64_t gptbridge_native_system_memory_available_bytes(void) {
#ifdef _WIN32
    MEMORYSTATUSEX status;
    ZeroMemory(&status, sizeof(status));
    status.dwLength = sizeof(status);
    if (GlobalMemoryStatusEx(&status) == 0) {
        return -1;
    }
    return (int64_t)status.ullAvailPhys;
#else
    return -1;
#endif
}

int gptbridge_native_cpu_count(void) {
#ifdef _WIN32
    SYSTEM_INFO info;
    GetSystemInfo(&info);
    return (int)info.dwNumberOfProcessors;
#else
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    return n > 0 ? (int)n : 0;
#endif
}

int gptbridge_native_process_alive(int64_t pid) {
#ifdef _WIN32
    HANDLE handle;
    DWORD exit_code = 0;
    if (pid <= 0) return 0;
    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD)pid);
    if (handle == NULL) return 0;
    if (GetExitCodeProcess(handle, &exit_code) == 0) {
        CloseHandle(handle);
        return 0;
    }
    CloseHandle(handle);
    return exit_code == STILL_ACTIVE ? 1 : 0;
#else
    return pid > 0 && kill((pid_t)pid, 0) == 0 ? 1 : 0;
#endif
}

static HANDLE _open_process(int64_t pid) {
#ifdef _WIN32
    if (pid <= 0) return NULL;
    return OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ,
        FALSE, (DWORD)pid);
#else
    return NULL;
#endif
}
#ifdef _WIN32
#pragma comment(lib, "iphlpapi.lib")
#pragma comment(lib, "ws2_32.lib")

typedef NTSTATUS(NTAPI* _NtQueryInformationProcess_t)(
    HANDLE ProcessHandle,
    PROCESSINFOCLASS ProcessInformationClass,
    PVOID ProcessInformation,
    ULONG ProcessInformationLength,
    PULONG ReturnLength);

/* Read a NUL-terminated UTF-16LE buffer from another process. */
static int64_t _read_remote_utf16(
    HANDLE proc, const void* remote_base, SIZE_T remote_len,
    char* buf, int64_t buf_len) {
    wchar_t* wide;
    int written;
    if (remote_len == 0 || remote_len > 1 << 20) return -1;
    wide = (wchar_t*)HeapAlloc(
        GetProcessHeap(), 0, (remote_len + 1) * sizeof(wchar_t));
    if (!wide) return -1;
    if (!ReadProcessMemory(
            proc, remote_base, wide, remote_len * sizeof(wchar_t), NULL)) {
        HeapFree(GetProcessHeap(), 0, wide);
        return -1;
    }
    wide[remote_len] = L'\0';
    written = WideCharToMultiByte(
        CP_UTF8, 0, wide, -1, buf, (int)buf_len, NULL, NULL);
    HeapFree(GetProcessHeap(), 0, wide);
    if (written <= 0) return -1;
    return (int64_t)(written - 1);
}

/* x64 offsets: PEB.ProcessParameters = 0x20,
   RTL_USER_PROCESS_PARAMETERS.CommandLine = 0x70. */
#define _PEB_PROCESS_PARAMS_OFF 0x20
#define _RUPP_COMMANDLINE_OFF 0x70
#endif

/* B94 self-decomposition: thunk groups are textually included so
   this file remains the single translation unit (static helpers above
   stay visible).  Fragments are not compiled separately. */
#include "gptbridge_native_proc_a.c"
#include "gptbridge_native_proc_b.c"
#include "gptbridge_native_proc_c.c"

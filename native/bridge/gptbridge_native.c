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

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <psapi.h>
#include <tlhelp32.h>
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

int64_t gptbridge_native_process_name(int64_t pid, char* buf, int64_t buf_len) {
#ifdef _WIN32
    HANDLE handle;
    DWORD size;
    wchar_t wide[MAX_PATH];
    int written;
    if (!buf || buf_len <= 0 || pid <= 0) return -1;
    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD)pid);
    if (handle == NULL) return -1;
    size = (DWORD)(sizeof(wide) / sizeof(wide[0]));
    if (QueryFullProcessImageNameW(handle, 0, wide, &size) == 0) {
        CloseHandle(handle);
        return -1;
    }
    CloseHandle(handle);
    /* base name only — match psutil.Process().name() contract */
    {
        wchar_t* base = wide;
        wchar_t* p;
        for (p = wide; *p; ++p) {
            if (*p == L'\\' || *p == L'/') base = p + 1;
        }
        written = WideCharToMultiByte(
            CP_UTF8, 0, base, -1, buf, (int)buf_len, NULL, NULL);
    }
    if (written <= 0) return -1;
    return (int64_t)(written - 1); /* exclude NUL */
#else
    return -1;
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

int64_t gptbridge_native_process_working_set_bytes_for(int64_t pid) {
#ifdef _WIN32
    HANDLE handle = _open_process(pid);
    PROCESS_MEMORY_COUNTERS counters;
    int64_t result = -1;
    if (handle == NULL) return -1;
    ZeroMemory(&counters, sizeof(counters));
    if (GetProcessMemoryInfo(handle, &counters, sizeof(counters)) != 0) {
        result = (int64_t)counters.WorkingSetSize;
    }
    CloseHandle(handle);
    return result;
#else
    return -1;
#endif
}

int64_t gptbridge_native_process_private_bytes_for(int64_t pid) {
#ifdef _WIN32
    HANDLE handle = _open_process(pid);
    PROCESS_MEMORY_COUNTERS_EX counters;
    int64_t result = -1;
    if (handle == NULL) return -1;
    ZeroMemory(&counters, sizeof(counters));
    counters.cb = sizeof(counters);
    if (GetProcessMemoryInfo(
            handle, (PPROCESS_MEMORY_COUNTERS)&counters,
            sizeof(counters)) != 0) {
        result = (int64_t)counters.PrivateUsage;
    }
    CloseHandle(handle);
    return result;
#else
    return -1;
#endif
}

int gptbridge_native_process_cpu_times_100ns(
    int64_t pid, int64_t* kernel_100ns, int64_t* user_100ns) {
#ifdef _WIN32
    HANDLE handle;
    FILETIME create_t, exit_t, kernel_t, user_t;
    if (!kernel_100ns || !user_100ns || pid <= 0) return 0;
    handle = OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD)pid);
    if (handle == NULL) return 0;
    if (GetProcessTimes(
            handle, &create_t, &exit_t, &kernel_t, &user_t) == 0) {
        CloseHandle(handle);
        return 0;
    }
    CloseHandle(handle);
    *kernel_100ns =
        ((int64_t)kernel_t.dwHighDateTime << 32) | kernel_t.dwLowDateTime;
    *user_100ns =
        ((int64_t)user_t.dwHighDateTime << 32) | user_t.dwLowDateTime;
    return 1;
#else
    return 0;
#endif
}

int gptbridge_native_process_list(int64_t* pids_out, int64_t max_count) {
#ifdef _WIN32
    HANDLE snapshot;
    PROCESSENTRY32W entry;
    int64_t count = 0;
    if (!pids_out || max_count <= 0) return -1;
    snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return -1;
    ZeroMemory(&entry, sizeof(entry));
    entry.dwSize = sizeof(entry);
    if (Process32FirstW(snapshot, &entry)) {
        do {
            if (count >= max_count) break;
            pids_out[count++] = (int64_t)entry.th32ProcessID;
        } while (Process32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    return (int)count;
#else
    return -1;
#endif
}

int gptbridge_native_process_terminate(int64_t pid) {
#ifdef _WIN32
    HANDLE handle;
    int ok;
    if (pid <= 0) return 0;
    handle = OpenProcess(PROCESS_TERMINATE, FALSE, (DWORD)pid);
    if (handle == NULL) return 0;
    ok = TerminateProcess(handle, 1) != 0 ? 1 : 0;
    CloseHandle(handle);
    return ok;
#else
    return pid > 0 && kill((pid_t)pid, SIGKILL) == 0 ? 1 : 0;
#endif
}

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

#include <string.h>

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

int64_t gptbridge_native_process_cmdline(
    int64_t pid, char* buf, int64_t buf_len) {
#ifdef _WIN32
    HANDLE proc;
    PROCESS_BASIC_INFORMATION pbi;
    _NtQueryInformationProcess_t nt_qip;
    ULONG_PTR params_addr = 0;
    USHORT cmd_len = 0;
    ULONG_PTR cmd_buf = 0;
    int64_t result = -1;
    if (!buf || buf_len <= 0 || pid <= 0) return -1;
    nt_qip = (_NtQueryInformationProcess_t)GetProcAddress(
        GetModuleHandleW(L"ntdll.dll"), "NtQueryInformationProcess");
    if (!nt_qip) return -1;
    proc = OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ,
        FALSE, (DWORD)pid);
    if (!proc) return -1;
    ZeroMemory(&pbi, sizeof(pbi));
    if (nt_qip(proc, ProcessBasicInformation, &pbi, sizeof(pbi), NULL) != 0) {
        CloseHandle(proc);
        return -1;
    }
    if (!pbi.PebBaseAddress) {
        CloseHandle(proc);
        return -1;
    }
    if (!ReadProcessMemory(
            proc,
            (const char*)pbi.PebBaseAddress + _PEB_PROCESS_PARAMS_OFF,
            &params_addr, sizeof(params_addr), NULL) ||
        !params_addr) {
        CloseHandle(proc);
        return -1;
    }
    if (!ReadProcessMemory(
            proc, (const char*)params_addr + _RUPP_COMMANDLINE_OFF,
            &cmd_len, sizeof(cmd_len), NULL) ||
        !ReadProcessMemory(
            proc,
            (const char*)params_addr + _RUPP_COMMANDLINE_OFF +
                sizeof(ULONG_PTR),
            &cmd_buf, sizeof(cmd_buf), NULL)) {
        CloseHandle(proc);
        return -1;
    }
    result = _read_remote_utf16(
        proc, (const void*)cmd_buf, (SIZE_T)cmd_len, buf, buf_len);
    CloseHandle(proc);
    return result;
#else
    (void)pid; (void)buf; (void)buf_len;
    return -1;
#endif
}

int64_t gptbridge_native_process_exe(
    int64_t pid, char* buf, int64_t buf_len) {
#ifdef _WIN32
    HANDLE proc;
    DWORD size;
    wchar_t wide[MAX_PATH];
    int written;
    if (!buf || buf_len <= 0 || pid <= 0) return -1;
    proc = OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD)pid);
    if (!proc) return -1;
    size = (DWORD)(sizeof(wide) / sizeof(wide[0]));
    if (QueryFullProcessImageNameW(proc, 0, wide, &size) == 0) {
        CloseHandle(proc);
        return -1;
    }
    CloseHandle(proc);
    written = WideCharToMultiByte(
        CP_UTF8, 0, wide, -1, buf, (int)buf_len, NULL, NULL);
    if (written <= 0) return -1;
    return (int64_t)(written - 1);
#else
    (void)pid; (void)buf; (void)buf_len;
    return -1;
#endif
}

int gptbridge_native_process_children(
    int64_t root_pid, int64_t* pids_out, int64_t max_count) {
#ifdef _WIN32
    HANDLE snapshot;
    PROCESSENTRY32W entry;
    DWORD* pids;
    DWORD* ppids;
    DWORD total = 0, i;
    int64_t out_count = 0;
    int64_t queue_head = 0;
    if (!pids_out || max_count <= 0 || root_pid <= 0) return -1;
    snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return -1;
    ZeroMemory(&entry, sizeof(entry));
    entry.dwSize = sizeof(entry);
    /* first pass: count rows */
    if (Process32FirstW(snapshot, &entry)) {
        do { ++total; } while (Process32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    if (total == 0) return 0;
    pids = (DWORD*)HeapAlloc(GetProcessHeap(), 0, total * sizeof(DWORD));
    ppids = (DWORD*)HeapAlloc(GetProcessHeap(), 0, total * sizeof(DWORD));
    if (!pids || !ppids) {
        if (pids) HeapFree(GetProcessHeap(), 0, pids);
        if (ppids) HeapFree(GetProcessHeap(), 0, ppids);
        return -1;
    }
    snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) {
        HeapFree(GetProcessHeap(), 0, pids);
        HeapFree(GetProcessHeap(), 0, ppids);
        return -1;
    }
    ZeroMemory(&entry, sizeof(entry));
    entry.dwSize = sizeof(entry);
    i = 0;
    if (Process32FirstW(snapshot, &entry)) {
        do {
            if (i < total) {
                pids[i] = entry.th32ProcessID;
                ppids[i] = entry.th32ParentProcessID;
                ++i;
            }
        } while (Process32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    total = i;
    /* BFS: seed the queue with root_pid inside pids_out itself. */
    pids_out[out_count++] = root_pid;
    while (queue_head < out_count) {
        DWORD cur = (DWORD)pids_out[queue_head++];
        for (i = 0; i < total; ++i) {
            if (ppids[i] == cur && pids[i] != cur) {
                if (out_count >= max_count) goto done;
                pids_out[out_count++] = (int64_t)pids[i];
            }
        }
    }
done:
    HeapFree(GetProcessHeap(), 0, pids);
    HeapFree(GetProcessHeap(), 0, ppids);
    /* compact: remove the root seed at position 0 */
    if (out_count > 1) {
        memmove(pids_out, pids_out + 1,
                (size_t)(out_count - 1) * sizeof(int64_t));
    }
    return (int)(out_count - 1);
#else
    (void)root_pid; (void)pids_out; (void)max_count;
    return -1;
#endif
}

int gptbridge_native_process_num_threads(int64_t pid) {
#ifdef _WIN32
    HANDLE snapshot;
    PROCESSENTRY32W entry;
    int result = -1;
    if (pid <= 0) return -1;
    snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return -1;
    ZeroMemory(&entry, sizeof(entry));
    entry.dwSize = sizeof(entry);
    if (Process32FirstW(snapshot, &entry)) {
        do {
            if (entry.th32ProcessID == (DWORD)pid) {
                result = (int)entry.cntThreads;
                break;
            }
        } while (Process32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    return result;
#else
    (void)pid;
    return -1;
#endif
}

int gptbridge_native_process_num_handles(int64_t pid) {
#ifdef _WIN32
    HANDLE proc;
    DWORD count = 0;
    if (pid <= 0) return -1;
    proc = OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD)pid);
    if (!proc) return -1;
    if (GetProcessHandleCount(proc, &count) == 0) {
        CloseHandle(proc);
        return -1;
    }
    CloseHandle(proc);
    return (int)count;
#else
    (void)pid;
    return -1;
#endif
}

int64_t gptbridge_native_tcp_listen_pid(int64_t port) {
#ifdef _WIN32
    PMIB_TCPTABLE_OWNER_PID table = NULL;
    PMIB_TCP6TABLE_OWNER_PID table6 = NULL;
    ULONG size = 0;
    DWORD i;
    int64_t result = -1;
    USHORT want_port;
    if (port <= 0 || port > 65535) return -1;
    want_port = (USHORT)port;
    /* IPv4 */
    if (GetExtendedTcpTable(
            NULL, &size, TRUE, AF_INET,
            TCP_TABLE_OWNER_PID_LISTENER, 0) == ERROR_INSUFFICIENT_BUFFER) {
        table = (PMIB_TCPTABLE_OWNER_PID)HeapAlloc(
            GetProcessHeap(), 0, size);
        if (table &&
            GetExtendedTcpTable(
                table, &size, TRUE, AF_INET,
                TCP_TABLE_OWNER_PID_LISTENER, 0) == NO_ERROR) {
            for (i = 0; i < table->dwNumEntries; ++i) {
                USHORT lp = ntohs(
                    (unsigned short)(table->table[i].dwLocalPort & 0xFFFF));
                if (lp == want_port) {
                    result = (int64_t)table->table[i].dwOwningPid;
                    break;
                }
            }
        }
        if (table) HeapFree(GetProcessHeap(), 0, table);
    }
    if (result >= 0) return result;
    /* IPv6 */
    size = 0;
    if (GetExtendedTcpTable(
            NULL, &size, TRUE, AF_INET6,
            TCP_TABLE_OWNER_PID_LISTENER, 0) == ERROR_INSUFFICIENT_BUFFER) {
        table6 = (PMIB_TCP6TABLE_OWNER_PID)HeapAlloc(
            GetProcessHeap(), 0, size);
        if (table6 &&
            GetExtendedTcpTable(
                table6, &size, TRUE, AF_INET6,
                TCP_TABLE_OWNER_PID_LISTENER, 0) == NO_ERROR) {
            for (i = 0; i < table6->dwNumEntries; ++i) {
                USHORT lp = ntohs(
                    (unsigned short)(table6->table[i].dwLocalPort & 0xFFFF));
                if (lp == want_port) {
                    result = (int64_t)table6->table[i].dwOwningPid;
                    break;
                }
            }
        }
        if (table6) HeapFree(GetProcessHeap(), 0, table6);
    }
    return result;
#else
    (void)port;
    return -1;
#endif
}

int gptbridge_native_system_cpu_times_100ns(
    int64_t* idle_100ns, int64_t* kernel_100ns, int64_t* user_100ns) {
#ifdef _WIN32
    FILETIME idle_t, kernel_t, user_t;
    if (!idle_100ns || !kernel_100ns || !user_100ns) return 0;
    if (GetSystemTimes(&idle_t, &kernel_t, &user_t) == 0) return 0;
    *idle_100ns =
        ((int64_t)idle_t.dwHighDateTime << 32) | idle_t.dwLowDateTime;
    *kernel_100ns =
        ((int64_t)kernel_t.dwHighDateTime << 32) | kernel_t.dwLowDateTime;
    *user_100ns =
        ((int64_t)user_t.dwHighDateTime << 32) | user_t.dwLowDateTime;
    return 1;
#else
    (void)idle_100ns; (void)kernel_100ns; (void)user_100ns;
    return 0;
#endif
}

/* gptbridge_native_proc_a.c - B94 fragment of gptbridge_native.c.
 * Textually included once by gptbridge_native.c (single TU); not a
 * separately compiled unit. */

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

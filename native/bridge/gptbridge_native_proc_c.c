/* gptbridge_native_proc_c.c - B94 fragment of gptbridge_native.c.
 * Textually included once by gptbridge_native.c (single TU); not a
 * separately compiled unit. */

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

int64_t gptbridge_native_process_parent(int64_t pid) {
#ifdef _WIN32
    HANDLE snapshot;
    PROCESSENTRY32W entry;
    int64_t result = -1;
    if (pid <= 0) return -1;
    snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return -1;
    ZeroMemory(&entry, sizeof(entry));
    entry.dwSize = sizeof(entry);
    if (Process32FirstW(snapshot, &entry)) {
        do {
            if (entry.th32ProcessID == (DWORD)pid) {
                result = (int64_t)entry.th32ParentProcessID;
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

int64_t gptbridge_native_process_create_time_ms(int64_t pid) {
#ifdef _WIN32
    HANDLE proc;
    FILETIME create_t, exit_t, kernel_t, user_t;
    int64_t ticks;
    if (pid <= 0) return -1;
    proc = OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD)pid);
    if (!proc) return -1;
    if (GetProcessTimes(proc, &create_t, &exit_t, &kernel_t, &user_t) == 0) {
        CloseHandle(proc);
        return -1;
    }
    CloseHandle(proc);
    ticks =
        ((int64_t)create_t.dwHighDateTime << 32) | create_t.dwLowDateTime;
    /* FILETIME epoch 1601-01-01 → Unix epoch delta = 11644473600 s. */
    return ticks / 10000 - 11644473600000LL;
#else
    (void)pid;
    return -1;
#endif
}

int gptbridge_native_process_io_counters(
    int64_t pid, int64_t* read_bytes, int64_t* write_bytes) {
#ifdef _WIN32
    HANDLE proc;
    IO_COUNTERS counters;
    if (!read_bytes || !write_bytes || pid <= 0) return 0;
    proc = OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD)pid);
    if (!proc) return 0;
    ZeroMemory(&counters, sizeof(counters));
    if (GetProcessIoCounters(proc, &counters) == 0) {
        CloseHandle(proc);
        return 0;
    }
    CloseHandle(proc);
    *read_bytes = (int64_t)counters.ReadTransferCount;
    *write_bytes = (int64_t)counters.WriteTransferCount;
    return 1;
#else
    (void)pid; (void)read_bytes; (void)write_bytes;
    return 0;
#endif
}

int64_t gptbridge_native_process_username(
    int64_t pid, char* buf, int64_t buf_len) {
#ifdef _WIN32
    HANDLE proc = NULL, token = NULL;
    DWORD size = 0;
    TOKEN_USER* user = NULL;
    wchar_t name[256], domain[256];
    DWORD name_len = 256, domain_len = 256;
    SID_NAME_USE sid_use;
    int64_t result = -1;
    if (!buf || buf_len <= 0 || pid <= 0) return -1;
    proc = OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD)pid);
    if (!proc) return -1;
    if (!OpenProcessToken(proc, TOKEN_QUERY, &token)) goto done;
    if (!GetTokenInformation(token, TokenUser, NULL, 0, &size) &&
        GetLastError() != ERROR_INSUFFICIENT_BUFFER)
        goto done;
    user = (TOKEN_USER*)HeapAlloc(GetProcessHeap(), 0, size);
    if (!user) goto done;
    if (!GetTokenInformation(token, TokenUser, user, size, &size))
        goto done;
    if (!LookupAccountSidW(
            NULL, user->User.Sid, name, &name_len,
            domain, &domain_len, &sid_use))
        goto done;
    {
        wchar_t joined[512];
        int written;
        size_t dlen = wcsnlen(domain, 255);
        size_t nlen = wcsnlen(name, 255);
        memcpy(joined, domain, dlen * sizeof(wchar_t));
        joined[dlen] = L'\\';
        memcpy(joined + dlen + 1, name, nlen * sizeof(wchar_t));
        joined[dlen + 1 + nlen] = L'\0';
        written = WideCharToMultiByte(
            CP_UTF8, 0, joined, -1, buf, (int)buf_len, NULL, NULL);
        if (written > 0) result = (int64_t)(written - 1);
    }
done:
    if (user) HeapFree(GetProcessHeap(), 0, user);
    if (token) CloseHandle(token);
    CloseHandle(proc);
    return result;
#else
    (void)pid; (void)buf; (void)buf_len;
    return -1;
#endif
}

int gptbridge_native_process_set_priority(int64_t pid, int32_t win_class) {
#ifdef _WIN32
    HANDLE proc;
    int ok;
    if (pid <= 0) return 0;
    proc = OpenProcess(PROCESS_SET_INFORMATION, FALSE, (DWORD)pid);
    if (!proc) return 0;
    ok = SetPriorityClass(proc, (DWORD)win_class) != 0 ? 1 : 0;
    CloseHandle(proc);
    return ok;
#else
    (void)pid; (void)win_class;
    return 0;
#endif
}

int64_t gptbridge_native_process_get_priority(int64_t pid) {
#ifdef _WIN32
    HANDLE proc;
    DWORD cls;
    if (pid <= 0) return -1;
    proc = OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD)pid);
    if (!proc) return -1;
    cls = GetPriorityClass(proc);
    CloseHandle(proc);
    return cls == 0 ? -1 : (int64_t)cls;
#else
    (void)pid;
    return -1;
#endif
}

int gptbridge_native_process_set_affinity(int64_t pid, uint64_t mask) {
#ifdef _WIN32
    HANDLE proc;
    int ok;
    if (pid <= 0 || mask == 0) return 0;
    proc = OpenProcess(PROCESS_SET_INFORMATION, FALSE, (DWORD)pid);
    if (!proc) return 0;
    ok = SetProcessAffinityMask(proc, (DWORD_PTR)mask) != 0 ? 1 : 0;
    CloseHandle(proc);
    return ok;
#else
    (void)pid; (void)mask;
    return 0;
#endif
}

int64_t gptbridge_native_process_get_affinity(int64_t pid) {
#ifdef _WIN32
    HANDLE proc;
    DWORD_PTR proc_mask = 0, sys_mask = 0;
    if (pid <= 0) return -1;
    proc = OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, FALSE, (DWORD)pid);
    if (!proc) return -1;
    if (GetProcessAffinityMask(proc, &proc_mask, &sys_mask) == 0) {
        CloseHandle(proc);
        return -1;
    }
    CloseHandle(proc);
    return (int64_t)proc_mask;
#else
    (void)pid;
    return -1;
#endif
}

int gptbridge_native_process_wait(int64_t pid, int64_t timeout_ms) {
#ifdef _WIN32
    HANDLE proc;
    DWORD rc;
    if (pid <= 0 || timeout_ms < 0) return 0;
    proc = OpenProcess(SYNCHRONIZE, FALSE, (DWORD)pid);
    if (!proc) return 0;
    rc = WaitForSingleObject(
        proc, timeout_ms > 0xFFFFFFFF ? INFINITE : (DWORD)timeout_ms);
    CloseHandle(proc);
    return rc == WAIT_OBJECT_0 ? 1 : 0;
#else
    (void)pid; (void)timeout_ms;
    return 0;
#endif
}

void* gptbridge_native_dirwatch_open(const wchar_t* path) {
#ifdef _WIN32
    HANDLE handle;
    if (path == NULL || path[0] == L'\0') return NULL;
    handle = FindFirstChangeNotificationW(
        path, TRUE,
        FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME |
        FILE_NOTIFY_CHANGE_SIZE | FILE_NOTIFY_CHANGE_LAST_WRITE);
    if (handle == INVALID_HANDLE_VALUE) return NULL;
    return (void*)handle;
#else
    (void)path;
    return NULL;
#endif
}

int gptbridge_native_dirwatch_wait(void* handle, int64_t timeout_ms) {
#ifdef _WIN32
    DWORD rc;
    if (handle == NULL || timeout_ms < 0) return -1;
    rc = WaitForSingleObject(
        (HANDLE)handle,
        timeout_ms > 0xFFFFFFFFLL ? INFINITE : (DWORD)timeout_ms);
    if (rc == WAIT_OBJECT_0) {
        /* A signaled change handle stays signaled until re-armed. */
        if (FindNextChangeNotification((HANDLE)handle) == 0) return -1;
        return 1;
    }
    if (rc == WAIT_TIMEOUT) return 0;
    return -1;
#else
    (void)handle; (void)timeout_ms;
    return -1;
#endif
}

void gptbridge_native_dirwatch_close(void* handle) {
#ifdef _WIN32
    if (handle != NULL) FindCloseChangeNotification((HANDLE)handle);
#else
    (void)handle;
#endif
}

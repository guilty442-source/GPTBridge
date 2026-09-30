/* gptbridge_native_proc_b.c - B94 fragment of gptbridge_native.c.
 * Textually included once by gptbridge_native.c (single TU); not a
 * separately compiled unit. */

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

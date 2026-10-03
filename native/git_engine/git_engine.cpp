// git-engine-native M0: read-only worktree probe.
// C++23 execution behind the stable C ABI in <git_engine.h>.
// Method: direct CreateProcessW of git.exe (no shell), piped stdout with a
// byte cap and a bounded wait. No write verb exists at M0.

#include "git_engine.h"

#include <windows.h>

#include <string>
#include <vector>

namespace {

constexpr int64_t kTimeoutMinMs = 1000;
constexpr int64_t kTimeoutMaxMs = 120000;

bool IsDirectoryW(const wchar_t* path) {
    if (path == nullptr || *path == L'\0') {
        return false;
    }
    const DWORD attrs = GetFileAttributesW(path);
    return attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_DIRECTORY) != 0;
}

bool ResolveGitExe(std::wstring& out) {
    wchar_t buf[MAX_PATH];
    // SearchPathW avoids hardcoding an install location; fail-closed when absent.
    const DWORD n = SearchPathW(nullptr, L"git.exe", nullptr, MAX_PATH, buf, nullptr);
    if (n == 0 || n >= MAX_PATH) {
        return false;
    }
    out.assign(buf);
    return true;
}

struct ProcCapture {
    std::string bytes;
    bool timed_out = false;
    bool launched = false;
};

// Child environment: the parent's block minus any inherited
// GIT_OPTIONAL_LOCKS, plus GIT_OPTIONAL_LOCKS=0. The status probe must
// never take the index lock (authority guard: the native engine takes no
// lock owned by the live watcher); without this, `git status` refreshes
// the index opportunistically and contends with the committer.
std::vector<wchar_t> BuildChildEnv() {
    std::wstring block;
    if (const wchar_t* env = GetEnvironmentStringsW()) {
        const wchar_t* p = env;
        const std::wstring skip = L"GIT_OPTIONAL_LOCKS=";
        while (*p) {
            const std::wstring entry = p;
            if (entry.compare(0, skip.size(), skip) != 0) {
                block += entry;
                block += L'\0';
            }
            p += entry.size() + 1;
        }
        FreeEnvironmentStringsW(const_cast<LPWCH>(env));
    }
    block += L"GIT_OPTIONAL_LOCKS=0";
    block += L'\0';
    block += L'\0';
    return std::vector<wchar_t>(block.begin(), block.end());
}

// Runs: git -C <dir> status --porcelain=v1 --untracked-files=normal
// Stores at most GE_OUTPUT_CAP_BYTES of stdout. No stdin, no shell.
// Output past the cap is drained and discarded so the child can still
// exit; truncation is reported via ge_probe_t, never as a launch error.
ProcCapture RunGitStatus(const std::wstring& git_exe, const std::wstring& dir, DWORD timeout_ms) {
    ProcCapture cap;
    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;

    HANDLE read_h = nullptr;
    HANDLE write_h = nullptr;
    if (!CreatePipe(&read_h, &write_h, &sa, 0)) {
        return cap;
    }
    // The read end stays ours; only the write end is inherited.
    SetHandleInformation(read_h, HANDLE_FLAG_INHERIT, 0);

    std::wstring cmd = L"\"" + git_exe + L"\" -C \"" + dir +
                       L"\" status --porcelain=v1 --untracked-files=normal";
    std::vector<wchar_t> cmdline(cmd.begin(), cmd.end());
    cmdline.push_back(L'\0');

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    si.hStdOutput = write_h;
    si.hStdError = write_h;
    si.hStdInput = nullptr;

    PROCESS_INFORMATION pi{};
    const std::vector<wchar_t> env = BuildChildEnv();
    const BOOL ok = CreateProcessW(nullptr, cmdline.data(), nullptr, nullptr, TRUE,
                                   CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                                   const_cast<wchar_t*>(env.data()), nullptr, &si, &pi);
    // This end belongs to the child now in every outcome past this point.
    CloseHandle(write_h);
    if (!ok) {
        CloseHandle(read_h);
        return cap;
    }
    cap.launched = true;

    // Drain whatever is available without blocking past the deadline.
    // Bytes beyond GE_OUTPUT_CAP_BYTES are read and discarded so the
    // child is never stranded on a full pipe.
    const auto drain = [&cap, read_h]() {
        DWORD avail = 0;
        while (PeekNamedPipe(read_h, nullptr, 0, nullptr, &avail, nullptr) && avail > 0) {
            char chunk[4096];
            DWORD got = 0;
            DWORD want = sizeof(chunk);
            const size_t room = static_cast<size_t>(GE_OUTPUT_CAP_BYTES) - cap.bytes.size();
            if (room > 0 && static_cast<size_t>(want) > room) {
                want = static_cast<DWORD>(room);
            }
            if (!ReadFile(read_h, chunk, want, &got, nullptr) || got == 0) {
                break;
            }
            if (room > 0) {
                cap.bytes.append(chunk, got);
            }
        }
    };

    constexpr DWORD kSliceMs = 100;
    DWORD waited = 0;
    DWORD exit_code = 0;
    for (;;) {
        drain();
        const DWORD rc = WaitForSingleObject(pi.hProcess, kSliceMs);
        waited += kSliceMs;
        if (rc == WAIT_OBJECT_0) {
            break;
        }
        if (waited >= timeout_ms) {
            cap.timed_out = true;
            TerminateProcess(pi.hProcess, 124);
            break;
        }
    }
    // Final drain after exit (or after kill, which yields nothing further).
    if (!cap.timed_out) {
        drain();
        if (!GetExitCodeProcess(pi.hProcess, &exit_code)) {
            exit_code = 1;
        }
        if (exit_code != 0) {
            cap.launched = false; // Signal git-side failure to the caller.
        }
    }
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    CloseHandle(read_h);
    return cap;
}

void ParsePorcelain(const std::string& bytes, bool hit_cap, ge_probe_t* out) {
    int64_t dirty = 0;
    int64_t staged = 0;
    int64_t untracked = 0;
    size_t pos = 0;
    while (pos < bytes.size()) {
        size_t eol = bytes.find('\n', pos);
        if (eol == std::string::npos) {
            eol = bytes.size();
        }
        size_t len = eol - pos;
        if (len > 0 && bytes[eol - 1] == '\r') {
            --len;
        }
        if (len >= 2) {
            const char x = bytes[pos];
            const char y = bytes[pos + 1];
            if (x == '?' && y == '?') {
                ++untracked;
            } else if (x != ' ' || y != ' ') {
                ++dirty;
                if (x != ' ' && x != '?') {
                    ++staged;
                }
            }
        }
        pos = eol + 1;
    }
    out->dirty = dirty;
    out->staged = staged;
    out->untracked = untracked;
    out->clean = (dirty == 0 && untracked == 0) ? 1 : 0;
    out->truncated = hit_cap ? 1 : 0;
}

} // namespace

int32_t ge_version(void) {
    return GE_VERSION_M0;
}

ge_status_t ge_probe_worktree(const wchar_t* dir_w, ge_probe_t* out, int64_t timeout_ms) {
    if (out == nullptr) {
        return GE_FAILED;
    }
    out->clean = 0;
    out->truncated = 0;
    out->dirty = 0;
    out->staged = 0;
    out->untracked = 0;

    if (!IsDirectoryW(dir_w)) {
        return GE_BAD_PATH;
    }
    if (timeout_ms < kTimeoutMinMs) {
        timeout_ms = kTimeoutMinMs;
    }
    if (timeout_ms > kTimeoutMaxMs) {
        timeout_ms = kTimeoutMaxMs;
    }

    std::wstring git_exe;
    if (!ResolveGitExe(git_exe)) {
        return GE_GIT_MISSING;
    }

    const ProcCapture cap = RunGitStatus(git_exe, dir_w, static_cast<DWORD>(timeout_ms));
    if (cap.timed_out) {
        return GE_TIMEOUT;
    }
    if (!cap.launched) {
        return GE_FAILED; // git missing mid-flight or non-zero exit (e.g. not a repo).
    }
    const bool hit_cap = cap.bytes.size() >= static_cast<size_t>(GE_OUTPUT_CAP_BYTES);
    ParsePorcelain(cap.bytes, hit_cap, out);
    return GE_OK;
}

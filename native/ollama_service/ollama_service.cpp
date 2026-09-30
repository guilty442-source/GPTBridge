// ollama_service.cpp — §10.7 Ollama 按需啟動服務（C++ 原生繼任者）。
//
// Retired lane: main-system/src-core/core_system/ollama_demand.py (B167/B38).
// Same contract, fail-closed:
//   probe    — TCP reachability of 127.0.0.1:11434
//   installed— ollama.exe presence (LOCALAPPDATA\Programs\Ollama or PATH)
//   ensure   — probe → governed headless spawn (`ollama serve`, DETACHED,
//              stdio→NUL) → readiness poll → audit; spawn result is never
//              faked, failure returns ok:false
//   stop     — terminate ONLY a pid this service spawned and recorded
//              (spawned_pid + spawned_image double-check; pid-recycle safe)
//   idle-unload — stop owned instance when last_use exceeds --idle-s
//   status   — combined installed/reachable/state snapshot
//
// Audit: append-only <root>/main-system/runtime/state/ollama-demand.jsonl
// State: <root>/main-system/runtime/state/ollama-demand-state.json
//   {last_use: epoch, spawned_pid, spawned_image}
// Root resolution: --root > GPTBRIDGE_ROOT env > walk-up from exe for
// main-system/config marker > cwd.
//
// Every command emits ONE JSON object on stdout. Exit: 0 = the command
// contract held; 1 = ensure/stop target state not reached; 2 = usage.
// No warm-up calls, no auto-revive — only an explicit ensure() is a demand.

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <psapi.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>

#include "jsonlite.h"

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "psapi.lib")

namespace fs = std::filesystem;
using gptbridge::jsonlite::JsonParser;
using gptbridge::jsonlite::JsonValue;
using gptbridge::jsonlite::json_escape;

namespace {

constexpr const char* kHost = "127.0.0.1";
constexpr uint16_t kPort = 11434;
constexpr double kProbeTimeoutS = 0.75;
constexpr double kEnsureTimeoutS = 15.0;
constexpr double kIdleUnloadS = 900.0;
constexpr double kReadyPollS = 0.5;

// ------------------------------------------------------------- utilities --

std::string now_utc() {
    SYSTEMTIME st;
    GetSystemTime(&st);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02dZ",
                  st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute,
                  st.wSecond);
    return buf;
}

double now_epoch() {
    FILETIME ft;
    GetSystemTimeAsFileTime(&ft);
    uint64_t ticks = (uint64_t(ft.dwHighDateTime) << 32) | ft.dwLowDateTime;
    return double(ticks) / 10000000.0 - 11644473600.0;
}

std::wstring widen(const std::string& s) {
    if (s.empty()) return L"";
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(),
                                nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), w.data(), n);
    return w;
}

std::string narrow(const std::wstring& w) {
    if (w.empty()) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(),
                                nullptr, 0, nullptr, nullptr);
    std::string s(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), s.data(), n,
                        nullptr, nullptr);
    return s;
}

// ------------------------------------------------------------------ args --

struct Args {
    std::string cmd;
    std::string root;
    double timeout_s = kEnsureTimeoutS;
    double idle_s = kIdleUnloadS;
    double probe_s = kProbeTimeoutS;
};

Args parse_args(int argc, char** argv) {
    Args a;
    for (int i = 1; i < argc; ++i) {
        std::string t = argv[i];
        auto need = [&](const char* name) -> std::string {
            if (i + 1 >= argc) {
                std::fprintf(stderr, "missing value for %s\n", name);
                std::exit(2);
            }
            return argv[++i];
        };
        if (t == "--root") a.root = need("--root");
        else if (t == "--timeout") a.timeout_s = std::stod(need("--timeout"));
        else if (t == "--idle-s") a.idle_s = std::stod(need("--idle-s"));
        else if (t == "--probe-timeout") a.probe_s = std::stod(need("--probe-timeout"));
        else if (t.rfind("--", 0) == 0) {
            std::fprintf(stderr, "unknown flag %s\n", t.c_str());
            std::exit(2);
        } else if (a.cmd.empty()) {
            a.cmd = t;
        } else {
            std::fprintf(stderr, "unexpected arg %s\n", t.c_str());
            std::exit(2);
        }
    }
    return a;
}

// ------------------------------------------------------------------ root --

fs::path exe_dir() {
    wchar_t buf[MAX_PATH];
    DWORD n = GetModuleFileNameW(nullptr, buf, MAX_PATH);
    return n ? fs::path(std::wstring(buf, n)).parent_path() : fs::path();
}

bool has_marker(const fs::path& p) {
    std::error_code ec;
    return fs::is_directory(p / "main-system" / "config", ec);
}

fs::path resolve_root(const std::string& arg) {
    if (!arg.empty()) return fs::path(arg);
    const char* env = std::getenv("GPTBRIDGE_ROOT");
    if (env && *env && has_marker(env)) return fs::path(env);
    // walk up from exe dir (native/ollama_service/bin → repo root)
    fs::path p = exe_dir();
    for (int i = 0; i < 8 && !p.empty(); ++i) {
        if (has_marker(p)) return p;
        p = p.parent_path();
    }
    return fs::current_path();
}

fs::path state_dir(const fs::path& root) {
    return root / "main-system" / "runtime" / "state";
}
fs::path audit_path(const fs::path& root) {
    return state_dir(root) / "ollama-demand.jsonl";
}
fs::path state_path(const fs::path& root) {
    return state_dir(root) / "ollama-demand-state.json";
}

// ----------------------------------------------------------------- audit --

void audit(const fs::path& root, const std::string& event,
           const std::string& extra = "") {
    std::error_code ec;
    fs::create_directories(state_dir(root), ec);
    std::ofstream f(audit_path(root), std::ios::app | std::ios::binary);
    if (!f) return;
    f << "{\"ts\":\"" << now_utc() << "\",\"event\":\""
      << json_escape(event) << "\"" << extra << "}\n";
}

// ----------------------------------------------------------------- state --

struct DemandState {
    double last_use = 0.0;
    int64_t spawned_pid = 0;
    std::string spawned_image;
};

DemandState read_state(const fs::path& root) {
    DemandState s;
    std::ifstream f(state_path(root), std::ios::binary);
    if (!f) return s;
    std::ostringstream ss;
    ss << f.rdbuf();
    try {
        JsonValue v = JsonParser(ss.str()).parse();
        if (const JsonValue* u = v.get("last_use"))
            if (u->type == JsonValue::Type::Number) s.last_use = u->number;
        if (const JsonValue* p = v.get("spawned_pid"))
            if (p->type == JsonValue::Type::Number)
                s.spawned_pid = (int64_t)p->number;
        if (const JsonValue* i = v.get("spawned_image"))
            if (i->type == JsonValue::Type::String)
                s.spawned_image = i->string;
    } catch (...) { /* corrupt state → fail-closed empty */ }
    return s;
}

void write_state(const fs::path& root, const DemandState& s) {
    std::error_code ec;
    fs::create_directories(state_dir(root), ec);
    fs::path tmp = state_path(root);
    tmp += ".tmp";
    {
        std::ofstream f(tmp, std::ios::binary | std::ios::trunc);
        if (!f) return;
        f << "{\"last_use\":" << (long long)s.last_use
          << ",\"spawned_pid\":" << s.spawned_pid
          << ",\"spawned_image\":\"" << json_escape(s.spawned_image)
          << "\"}";
    }
    MoveFileExW(widen(tmp.string()).c_str(),
                widen(state_path(root).string()).c_str(),
                MOVEFILE_REPLACE_EXISTING);
}

void touch_use(const fs::path& root, int64_t pid = 0,
               const std::string& image = "") {
    DemandState s = read_state(root);
    s.last_use = now_epoch();
    if (pid > 0) {
        s.spawned_pid = pid;
        s.spawned_image = image;
    }
    write_state(root, s);
}

// ------------------------------------------------------------ ollama exe --

std::string find_in_path(const char* name) {
    char buf[MAX_PATH];
    DWORD n = SearchPathA(nullptr, name, nullptr, MAX_PATH, buf, nullptr);
    return (n > 0 && n < MAX_PATH) ? std::string(buf, n) : "";
}

// Prefer the headless server binary — the GUI app self-issues warm-up
// calls and spawns tray/update behaviour an on-demand daemon must not
// trigger (parity with the retired demand-spawn ordering).
std::string ollama_server_exe() {
    std::string local = narrow(std::wstring(_wgetenv(L"LOCALAPPDATA")));
    if (!local.empty()) {
        fs::path exe = fs::path(local) / "Programs" / "Ollama" / "ollama.exe";
        std::error_code ec;
        if (fs::is_regular_file(exe, ec)) return exe.string();
    }
    return find_in_path("ollama.exe");
}

std::string ollama_any_exe() {
    std::string s = ollama_server_exe();
    if (!s.empty()) return s;
    std::string local = narrow(std::wstring(_wgetenv(L"LOCALAPPDATA")));
    if (!local.empty()) {
        fs::path app =
            fs::path(local) / "Programs" / "Ollama" / "ollama app.exe";
        std::error_code ec;
        if (fs::is_regular_file(app, ec)) return app.string();
    }
    return find_in_path("ollama app.exe");
}

// ----------------------------------------------------------------- probe --

struct WsaInit {
    WsaInit() { WSADATA d; WSAStartup(MAKEWORD(2, 2), &d); }
    ~WsaInit() { WSACleanup(); }
};

bool probe_tcp(double timeout_s) {
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) return false;
    u_long nb = 1;
    ioctlsocket(s, FIONBIO, &nb);

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(kPort);
    inet_pton(AF_INET, kHost, &addr.sin_addr);

    connect(s, (sockaddr*)&addr, sizeof(addr));  // non-blocking
    fd_set wfds;
    FD_ZERO(&wfds);
    FD_SET(s, &wfds);
    fd_set efds;
    FD_ZERO(&efds);
    FD_SET(s, &efds);
    timeval tv;
    tv.tv_sec = long(timeout_s);
    tv.tv_usec = long((timeout_s - tv.tv_sec) * 1e6);
    bool ok = select(0, nullptr, &wfds, &efds, &tv) == 1 &&
              FD_ISSET(s, &wfds);
    if (ok) {
        int err = 0;
        int len = sizeof(err);
        getsockopt(s, SOL_SOCKET, SO_ERROR, (char*)&err, &len);
        ok = (err == 0);
    }
    closesocket(s);
    return ok;
}

// ----------------------------------------------------------------- spawn --

struct SpawnResult {
    int64_t pid = 0;
    std::string cmd;
};

SpawnResult spawn_ollama() {
    SpawnResult r;
    std::string exe = ollama_server_exe();
    if (exe.empty()) return r;
    std::wstring wcmd = L"\"" + widen(exe) + L"\" serve";

    HANDLE nul = CreateFileW(L"NUL", GENERIC_READ | GENERIC_WRITE,
                             FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                             OPEN_EXISTING, 0, nullptr);
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = nul;
    si.hStdOutput = nul;
    si.hStdError = nul;
    PROCESS_INFORMATION pi{};
    DWORD flags = CREATE_NO_WINDOW | DETACHED_PROCESS;
    std::wstring cmdline = wcmd;  // CreateProcessW needs mutable buffer
    if (!CreateProcessW(nullptr, cmdline.data(), nullptr, nullptr, TRUE,
                        flags, nullptr, nullptr, &si, &pi)) {
        if (nul != INVALID_HANDLE_VALUE) CloseHandle(nul);
        return r;
    }
    if (nul != INVALID_HANDLE_VALUE) CloseHandle(nul);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    r.pid = (int64_t)pi.dwProcessId;
    r.cmd = exe + " serve";
    return r;
}

// ------------------------------------------------------------- ownership --

std::string process_image(int64_t pid) {
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE,
                           (DWORD)pid);
    if (!h) return "";
    wchar_t buf[MAX_PATH];
    DWORD n = MAX_PATH;
    std::string out;
    if (QueryFullProcessImageNameW(h, 0, buf, &n))
        out = narrow(std::wstring(buf, n));
    CloseHandle(h);
    return out;
}

std::string lower(std::string s) {
    for (auto& c : s) c = (char)tolower((unsigned char)c);
    return s;
}

// ---------------------------------------------------------------- output --

void emit(const std::string& json) {
    std::fputs(json.c_str(), stdout);
    std::fputc('\n', stdout);
}

std::string jbool(bool b) { return b ? "true" : "false"; }

// ------------------------------------------------------------- commands --

int cmd_installed() {
    std::string exe = ollama_any_exe();
    emit(std::string("{\"ok\":true,\"installed\":") + jbool(!exe.empty()) +
         ",\"exe\":\"" + json_escape(exe) + "\"}");
    return 0;
}

int cmd_probe(double timeout_s) {
    WsaInit wsa;
    bool ok = probe_tcp(timeout_s);
    emit(std::string("{\"ok\":true,\"reachable\":") + jbool(ok) +
         ",\"host\":\"" + kHost + "\",\"port\":" +
         std::to_string(kPort) + "}");
    return 0;
}

int cmd_status(const fs::path& root, double probe_s) {
    WsaInit wsa;
    std::string exe = ollama_any_exe();
    bool reachable = probe_tcp(probe_s);
    DemandState s = read_state(root);
    bool owned_alive = false;
    std::string owned_image;
    if (s.spawned_pid > 0) {
        owned_image = process_image(s.spawned_pid);
        owned_alive = !owned_image.empty() &&
                      lower(owned_image) == lower(s.spawned_image);
    }
    emit(std::string("{\"ok\":true,\"installed\":") + jbool(!exe.empty()) +
         ",\"exe\":\"" + json_escape(exe) + "\"" +
         ",\"reachable\":" + jbool(reachable) +
         ",\"spawned_pid\":" + std::to_string(s.spawned_pid) +
         ",\"owned_alive\":" + jbool(owned_alive) +
         ",\"last_use\":" + std::to_string((long long)s.last_use) + "}");
    return 0;
}

int cmd_ensure(const fs::path& root, double timeout_s, double probe_s) {
    WsaInit wsa;
    touch_use(root);
    if (probe_tcp(probe_s)) {
        emit("{\"ok\":true,\"ready\":true,\"spawned\":false}");
        return 0;
    }
    SpawnResult sp = spawn_ollama();
    if (sp.pid == 0) {
        audit(root, "spawn-unavailable");
        emit("{\"ok\":false,\"error\":\"SPAWN_UNAVAILABLE\"}");
        return 1;
    }
    audit(root, "spawn",
          ",\"pid\":" + std::to_string(sp.pid) + ",\"cmd\":\"" +
              json_escape(sp.cmd) + "\"");
    touch_use(root, sp.pid, ollama_server_exe());
    double deadline = timeout_s;
    LARGE_INTEGER freq, t0;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&t0);
    while (true) {
        if (probe_tcp(kReadyPollS)) {
            LARGE_INTEGER t1;
            QueryPerformanceCounter(&t1);
            int64_t ms = (t1.QuadPart - t0.QuadPart) * 1000 / freq.QuadPart;
            audit(root, "ready",
                  ",\"pid\":" + std::to_string(sp.pid) +
                      ",\"elapsed_ms\":" + std::to_string(ms));
            emit(std::string("{\"ok\":true,\"ready\":true,\"spawned\":true,"
                             "\"pid\":") +
                 std::to_string(sp.pid) +
                 ",\"elapsed_ms\":" + std::to_string(ms) + "}");
            return 0;
        }
        LARGE_INTEGER t1;
        QueryPerformanceCounter(&t1);
        if (double(t1.QuadPart - t0.QuadPart) / freq.QuadPart >= deadline) {
            int64_t ms = (t1.QuadPart - t0.QuadPart) * 1000 / freq.QuadPart;
            audit(root, "timeout",
                  ",\"pid\":" + std::to_string(sp.pid) +
                      ",\"elapsed_ms\":" + std::to_string(ms));
            emit(std::string("{\"ok\":false,\"error\":\"READY_TIMEOUT\","
                             "\"pid\":") +
                 std::to_string(sp.pid) +
                 ",\"elapsed_ms\":" + std::to_string(ms) + "}");
            return 1;
        }
        Sleep(500);
    }
}

// Terminate only a pid this service recorded as spawned — image path must
// equal the recorded spawned_image (pid-recycle safe, fail-closed).
int cmd_stop(const fs::path& root) {
    DemandState s = read_state(root);
    if (s.spawned_pid <= 0) {
        emit("{\"ok\":true,\"stopped\":false,\"reason\":\"no-owned-pid\"}");
        return 0;
    }
    std::string image = process_image(s.spawned_pid);
    if (image.empty()) {
        emit("{\"ok\":true,\"stopped\":false,\"reason\":\"pid-exited\"}");
        return 0;
    }
    if (lower(image) != lower(s.spawned_image)) {
        audit(root, "unload-refused",
              ",\"pid\":" + std::to_string(s.spawned_pid) +
                  ",\"reason\":\"image-mismatch\"");
        emit("{\"ok\":false,\"stopped\":false,"
             "\"error\":\"UNLOAD_REFUSED_IMAGE_MISMATCH\"}");
        return 1;
    }
    HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, (DWORD)s.spawned_pid);
    if (!h || !TerminateProcess(h, 0)) {
        if (h) CloseHandle(h);
        audit(root, "unload-failed",
              ",\"pid\":" + std::to_string(s.spawned_pid));
        emit("{\"ok\":false,\"stopped\":false,\"error\":\"UNLOAD_FAILED\"}");
        return 1;
    }
    CloseHandle(h);
    double idle = now_epoch() - s.last_use;
    audit(root, "unload",
          ",\"pid\":" + std::to_string(s.spawned_pid) +
              ",\"idle_s\":" + std::to_string((long long)idle));
    DemandState cleared = s;
    cleared.spawned_pid = 0;
    cleared.spawned_image.clear();
    write_state(root, cleared);
    emit(std::string("{\"ok\":true,\"stopped\":true,\"pid\":") +
         std::to_string(s.spawned_pid) + "}");
    return 0;
}

int cmd_idle_unload(const fs::path& root, double idle_s) {
    DemandState s = read_state(root);
    if (s.spawned_pid <= 0) {
        emit("{\"ok\":true,\"unloaded\":false,\"reason\":\"no-owned-pid\"}");
        return 0;
    }
    double idle = now_epoch() - s.last_use;
    if (idle < idle_s) {
        emit(std::string("{\"ok\":true,\"unloaded\":false,"
                         "\"reason\":\"not-idle\",\"idle_s\":") +
             std::to_string((long long)idle) + "}");
        return 0;
    }
    return cmd_stop(root);
}

void usage() {
    emit("{\"ok\":false,\"error\":\"USAGE\",\"commands\":["
         "\"installed\",\"probe\",\"status\",\"ensure\",\"stop\","
         "\"idle-unload\"],"
         "\"flags\":[\"--root\",\"--timeout\",\"--idle-s\","
         "\"--probe-timeout\"]}");
}

}  // namespace

int main(int argc, char** argv) {
    Args a = parse_args(argc, argv);
    if (a.cmd.empty()) {
        usage();
        return 2;
    }
    if (a.cmd == "installed") return cmd_installed();
    if (a.cmd == "probe") return cmd_probe(a.probe_s);

    fs::path root = resolve_root(a.root);
    if (a.cmd == "status") return cmd_status(root, a.probe_s);
    if (a.cmd == "ensure") return cmd_ensure(root, a.timeout_s, a.probe_s);
    if (a.cmd == "stop") return cmd_stop(root);
    if (a.cmd == "idle-unload") return cmd_idle_unload(root, a.idle_s);
    usage();
    return 2;
}

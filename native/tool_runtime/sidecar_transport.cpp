/* sidecar_transport.cpp — P2 sidecar 接線層實作（Windows CreateProcess
 * ＋匿名管道）。見 sidecar_transport.h。
 */
#include "sidecar_transport.h"

#ifdef _WIN32

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <chrono>
#include <string>
#include <vector>

namespace gptbridge {
namespace tpx {

namespace {

/* call() 總期限：代理正常為毫秒級；期限僅是 fail-closed 保險。 */
constexpr double kDefaultCallDeadlineSeconds = 120.0;
constexpr DWORD kPollMs = 5;
constexpr DWORD kPipeSize = 1 << 20;

double now_seconds() {
    using clock = std::chrono::steady_clock;
    return std::chrono::duration<double>(clock::now().time_since_epoch())
        .count();
}

} // namespace

struct ProxySidecar::Impl {
    HANDLE child_stdin_r = nullptr;  /* 子端（spawn 後即關） */
    HANDLE child_stdout_w = nullptr;
    HANDLE stdin_w = nullptr;        /* 父端 */
    HANDLE stdout_r = nullptr;
    HANDLE process = nullptr;
    HANDLE thread = nullptr;
    std::string pending;             /* 未滿一行的讀取緩衝 */

    ~Impl() { close_all(); }

    /* 讀滿一行（協定丟棄行不在此層判斷）。deadline_at 超過或斷管回
       false；子行程退出時先排乾殘留位元組。 */
    bool read_line(double deadline_at, std::string* out) {
        for (;;) {
            const auto nl = pending.find('\n');
            if (nl != std::string::npos) {
                *out = pending.substr(0, nl);
                pending.erase(0, nl + 1);
                if (!out->empty() && out->back() == '\r') out->pop_back();
                return true;
            }
            DWORD avail = 0;
            if (!PeekNamedPipe(stdout_r, nullptr, 0, nullptr, &avail,
                               nullptr)) {
                return false; /* 斷管 */
            }
            if (avail == 0) {
                if (WaitForSingleObject(process, 0) != WAIT_TIMEOUT) {
                    DWORD leftover = 0;
                    PeekNamedPipe(stdout_r, nullptr, 0, nullptr,
                                  &leftover, nullptr);
                    if (leftover == 0) return false;
                } else if (now_seconds() >= deadline_at) {
                    return false;
                } else {
                    Sleep(kPollMs);
                    continue;
                }
            }
            char buf[8192];
            DWORD want = avail > 0
                             ? (avail < sizeof(buf) ? avail : sizeof(buf))
                             : sizeof(buf);
            DWORD got = 0;
            if (!ReadFile(stdout_r, buf, want, &got, nullptr)) {
                return false;
            }
            if (got == 0) continue;
            pending.append(buf, got);
        }
    }

    void close_all() {
        for (HANDLE* h :
             {&child_stdin_r, &child_stdout_w, &stdin_w, &stdout_r}) {
            if (*h) {
                CloseHandle(*h);
                *h = nullptr;
            }
        }
        if (process) {
            TerminateProcess(process, 1);
            CloseHandle(process);
            process = nullptr;
        }
        if (thread) {
            CloseHandle(thread);
            thread = nullptr;
        }
        pending.clear();
    }
};

ProxySidecar::~ProxySidecar() { stop(); }

bool ProxySidecar::running() const {
    if (impl_ == nullptr || impl_->process == nullptr) return false;
    return WaitForSingleObject(impl_->process, 0) == WAIT_TIMEOUT;
}

void ProxySidecar::stop() {
    if (impl_ == nullptr) return;
    impl_->close_all();
    delete impl_;
    impl_ = nullptr;
}

bool ProxySidecar::start(const std::string& command_line,
                         SidecarError* err) {
    stop();
    impl_ = new Impl();

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;

    if (!CreatePipe(&impl_->child_stdin_r, &impl_->stdin_w, &sa,
                    kPipeSize) ||
        !CreatePipe(&impl_->stdout_r, &impl_->child_stdout_w, &sa,
                    kPipeSize)) {
        if (err) {
            err->code = "PROXY_SPAWN_FAILED";
            err->message = "CreatePipe failed";
        }
        return false;
    }
    /* 父端不得被子行程繼承 */
    SetHandleInformation(impl_->stdin_w, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(impl_->stdout_r, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOA si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = impl_->child_stdin_r;
    si.hStdOutput = impl_->child_stdout_w;
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);

    PROCESS_INFORMATION pi{};
    std::vector<char> cmd(command_line.begin(), command_line.end());
    cmd.push_back('\0');
    if (!CreateProcessA(nullptr, cmd.data(), nullptr, nullptr,
                        /*bInheritHandles=*/TRUE, 0, nullptr, nullptr, &si,
                        &pi)) {
        if (err) {
            err->code = "PROXY_SPAWN_FAILED";
            err->message = "CreateProcess failed: " +
                           std::to_string(GetLastError());
        }
        return false;
    }
    impl_->process = pi.hProcess;
    impl_->thread = pi.hThread;
    /* 子端在父行程內立即關閉，確保 EOF/斷管語義正確 */
    CloseHandle(impl_->child_stdin_r);
    impl_->child_stdin_r = nullptr;
    CloseHandle(impl_->child_stdout_w);
    impl_->child_stdout_w = nullptr;
    return true;
}

bool ProxySidecar::call(const std::string& op,
                        const std::string& args_json, ProxyResponse* out,
                        SidecarError* err) {
    if (!running()) {
        if (err) {
            err->code = "PROXY_DISCONNECTED";
            err->message = "sidecar not running";
        }
        return false;
    }
    const std::string id =
        "req-" + std::to_string(GetCurrentProcessId()) + "-" +
        std::to_string(next_id_++);
    const std::string line = encode_request(id, op, args_json) + "\n";

    DWORD written = 0;
    if (!WriteFile(impl_->stdin_w, line.data(),
                   static_cast<DWORD>(line.size()), &written, nullptr) ||
        written != line.size()) {
        if (err) {
            err->code = "PROXY_DISCONNECTED";
            err->message = "stdin write failed";
        }
        return false;
    }

    const double deadline_at = now_seconds() + kDefaultCallDeadlineSeconds;
    std::string resp_line;
    for (;;) {
        if (!impl_->read_line(deadline_at, &resp_line)) {
            if (err) {
                err->code = running() ? "PROXY_TIMEOUT"
                                      : "PROXY_DISCONNECTED";
                err->message = "response read failed";
            }
            return false;
        }
        if (resp_line.empty()) continue;
        ProxyResponse r;
        if (!decode_response_line(resp_line, &r)) {
            continue; /* 協定丟棄行 */
        }
        if (r.id != id) continue; /* 非本呼叫回應（單工下不應出現） */
        *out = r;
        return true;
    }
}

} // namespace tpx
} // namespace gptbridge

#else /* !_WIN32 — fail-closed stub */

namespace gptbridge {
namespace tpx {

struct ProxySidecar::Impl {};

ProxySidecar::~ProxySidecar() { delete impl_; }
bool ProxySidecar::running() const { return false; }
void ProxySidecar::stop() {}
bool ProxySidecar::start(const std::string&, SidecarError* err) {
    if (err) {
        err->code = "PROXY_SPAWN_FAILED";
        err->message = "sidecar transport is Windows-only";
    }
    return false;
}
bool ProxySidecar::call(const std::string&, const std::string&,
                        ProxyResponse*, SidecarError* err) {
    if (err) {
        err->code = "PROXY_DISCONNECTED";
        err->message = "sidecar transport is Windows-only";
    }
    return false;
}

} // namespace tpx
} // namespace gptbridge

#endif /* _WIN32 */

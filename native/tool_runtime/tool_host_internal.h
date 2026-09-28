/* tool_host_internal.h — ToolHost 跨 TU 共享內部介面
 * （A50 模組上限拆分：conn/claims/observe 子模組需見 Impl 與線上
 * 助手）。僅供 native/tool_runtime 內 tool_host*.cpp 引用，不裝入
 * native/include；非 Windows 平台本標頭為空（入口 stub 在
 * tool_host.cpp）。
 */
#ifndef GPTBRIDGE_TOOL_HOST_INTERNAL_H
#define GPTBRIDGE_TOOL_HOST_INTERNAL_H

#ifdef _WIN32

#define _CRT_RAND_S  /* rand_s：行程內閘門證明 nonce 的 CRT 安全隨機源 */

#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <ctime>
#include <deque>
#include <fstream>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <thread>

#include "tool_host.h"
#include "governed_tool.h"
#include "governed_tool_ws.h"

namespace gptbridge {
namespace toolhost {

namespace jl = jsonlite;
namespace gtw = gptbridge::gtw;
namespace tpx = gptbridge::tpx;

namespace detail {

/* ---- env-gate 證明登錄區（P6） ----
   load_env() 通過全閘序列後在此鑄造隨機 nonce；start() 只接受登錄區
   記載的 proof。自我聲明的 env_gate_passed 旗標可偽造，登錄區 nonce
   不行——注入 config 想繞過 bootstrap env 閘必須先跑完 load_env 本身。
   inline 變數：全行程唯一實例（跨 TU 共用同一登錄區）。 */
inline std::mutex g_gate_proof_mu;
inline std::set<std::string> g_gate_proofs;

inline std::string mint_gate_proof() {
    unsigned int v[8] = {0};
    for (auto& x : v) {
        if (rand_s(&x) != 0) return std::string();
    }
    static const char hex[] = "0123456789abcdef";
    std::string out;
    out.reserve(64);
    for (unsigned int x : v) {
        for (int s = 28; s >= 0; s -= 4)
            out.push_back(hex[(x >> s) & 0xF]);
    }
    std::lock_guard<std::mutex> lk(g_gate_proof_mu);
    g_gate_proofs.insert(out);
    return out;
}

inline bool gate_proof_registered(const std::string& proof) {
    if (proof.empty()) return false;
    std::lock_guard<std::mutex> lk(g_gate_proof_mu);
    return g_gate_proofs.find(proof) != g_gate_proofs.end();
}

inline jl::JsonValue jstr(const std::string& s) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::String; v.string = s;
    return v;
}
inline jl::JsonValue jnum(double n) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Number; v.number = n;
    return v;
}
inline jl::JsonValue jbool(bool b) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Bool; v.boolean = b;
    return v;
}
inline jl::JsonValue jobj(std::initializer_list<
                   std::pair<std::string, jl::JsonValue>> items) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Object;
    v.object.assign(items.begin(), items.end());
    return v;
}
inline jl::JsonValue jparse(const std::string& s, bool* ok) {
    try {
        jl::JsonValue v = jl::JsonParser(s).parse();
        *ok = true;
        return v;
    } catch (...) {
        *ok = false;
        return jl::JsonValue{};
    }
}
inline const jl::JsonValue* get_str(const jl::JsonValue& v,
                                    const char* k) {
    const jl::JsonValue* p = v.get(k);
    return (p && p->type == jl::JsonValue::Type::String) ? p : nullptr;
}

inline const char* env_or_empty(const char* name) {
    const char* v = std::getenv(name);
    return v ? v : "";
}

inline bool send_all(intptr_t s, const std::string& data) {
    size_t off = 0;
    while (off < data.size()) {
        const int n = send(static_cast<SOCKET>(s), data.data() + off,
                           static_cast<int>(data.size() - off), 0);
        if (n <= 0) return false;
        off += static_cast<size_t>(n);
    }
    return true;
}

inline std::string http_event_frame(const std::string& event,
                                    const jl::JsonValue& payload) {
    return gtw::ws_frame_encode(
        true, gtw::WsOp::Text,
        jl::json_serialize(jobj({{"event", jstr(event)},
                                 {"payload", payload}})));
}

/* concurrency-budget/v1 讀側：governor 於
   <project_root>/main-system/runtime/state/resource-governor.json
   發佈 classes.<work_class>.quota。讀不到/契約不符 → -1
   （fail-open：呼叫方用模組宣告 envelope 上限，絕不因此死鎖）。 */
inline int governor_class_quota(const std::string& project_root,
                                const std::string& work_class) {
    if (project_root.empty()) return -1;
    const std::string path = project_root +
        "\\main-system\\runtime\\state\\resource-governor.json";
    std::ifstream in(path);
    if (!in) return -1;
    std::ostringstream ss;
    ss << in.rdbuf();
    jl::JsonValue state;
    try {
        state = jl::JsonParser(ss.str()).parse();
    } catch (const jl::JsonError&) {
        return -1;
    }
    if (const jl::JsonValue* d = state.get("disabled")) {
        if (d->type == jl::JsonValue::Type::Bool && d->boolean)
            return -1;
    }
    const jl::JsonValue* budget = state.get("concurrency_budget");
    if (!budget) return -1;
    const jl::JsonValue* contract = budget->get("contract");
    if (!contract || contract->type != jl::JsonValue::Type::String ||
        contract->string != "concurrency-budget/v1")
        return -1;
    const jl::JsonValue* classes = budget->get("classes");
    if (!classes) return -1;
    const jl::JsonValue* entry = classes->get(work_class);
    if (!entry) return -1;
    const jl::JsonValue* quota = entry->get("quota");
    if (!quota || quota->type != jl::JsonValue::Type::Number)
        return -1;
    return static_cast<int>(quota->number);
}

/* 503（queue-full reject）：jsonlite 無狀態碼助手，最小明文案
   保持 Connection: close 讓用戶端立即知道被拒絕。 */
inline const char kHttp503[] =
    "HTTP/1.1 503 Service Unavailable\r\n"
    "Content-Type: text/plain\r\n"
    "Content-Length: 15\r\n"
    "Connection: close\r\n"
    "\r\n"
    "capacity-exhaust";

} // namespace detail

struct ToolHost::Impl {
    ToolHostConfig cfg;
    ToolHostHooks hooks;
    std::unique_ptr<tpx::ProxySidecar> sidecar;
    std::unique_ptr<tpx::ProxySidecar> submit_sidecar;
    std::atomic<bool> stop_flag{false};
    std::atomic<int64_t> started_ms{0};

    SOCKET listen_sock = INVALID_SOCKET;
    std::thread accept_thread;
    std::thread claim_thread;
    std::mutex conn_mu;
    std::set<SOCKET> conns;
    /* bounded-concurrency/v1：conn worker 池從有界 pending 佇列
       取連線；佇列滿 → accept 端 503 拒絕（capacity＋backpressure
       ＋drop/reject）。worker 數於 start() 由 governor network
       配額決定（clamp 於 cfg envelope）。 */
    std::mutex pending_mu;
    std::condition_variable pending_cv;
    std::deque<SOCKET> pending_conns;
    std::vector<std::thread> conn_pool;
    int conn_workers = 0;
    std::atomic<int64_t> n_conn_rejected{0};
    /* 活動 conn 執行緒數（detached）：run() 必須等其歸零才釋放
       impl_/WSACleanup，否則 conn 收尾路徑對已釋放 impl_ UAF。 */
    std::atomic<int> active_conns{0};

    /* waiters: request_id → ws socket（{cmd}_result 推送目標） */
    std::mutex waiter_mu;
    std::map<std::string, SOCKET> waiters;
    /* WS 出站序列化：conn 執行緒（COMMAND_RECEIVED/pong/close）與
       claim 執行緒（{cmd}_result 推送）不得交錯位元——Python 側由
       asyncio 單執行緒天然序列化，此處以單一 send 互斥對齊。 */
    std::mutex send_mu;
    /* 執行中 request 的取消旗標 */
    std::mutex exec_mu;
    std::map<std::string, std::shared_ptr<std::atomic<bool>>> exec_flags;

    /* channel_health（§4：連續失敗 ≥3 → degraded；Python
       ChannelHealth.as_dict 形狀：last_ok/last_request_at/
       consecutive_failures/degraded） */
    struct ChHealth {
        int64_t consecutive_failures = 0;
        bool last_ok = true;
        std::string last_request_at; /* ISO8601 UTC */
    };
    std::mutex health_mu;
    std::map<std::string, ChHealth> ch_health;

    std::atomic<int64_t> n_connections{0}, n_upgrades{0}, n_commands{0},
        n_denied{0}, n_claims{0}, n_executed{0}, n_responded{0},
        n_cancelled{0}, n_claim_fail{0};

    int64_t now_ms() const {
        return std::chrono::duration_cast<std::chrono::milliseconds>(
                   std::chrono::steady_clock::now().time_since_epoch())
            .count();
    }

    bool proxy(const std::string& op, const std::string& args,
               tpx::ProxyResponse* out) {
        tpx::SidecarError err;
        if (hooks.proxy_call) {
            return hooks.proxy_call(op, args, out, &err);
        }
        if (sidecar) {
            return sidecar->call(op, args, out, &err);
        }
        if (out) { /* 無 proxy：以協定錯誤形式回報，呼叫方 fail-closed */
            out->valid = true; out->ok = false;
            out->error_code = "PROXY_DISCONNECTED";
            out->error_message = "no proxy transport";
        }
        return true;
    }

    /* submit 側：WS request/cancel——submit 綁定的第二條傳輸。
       未配置 → ok:false PROXY_DISCONNECTED（呼叫方 fail-closed）。 */
    bool proxy_submit(const std::string& op, const std::string& args,
                      tpx::ProxyResponse* out) {
        tpx::SidecarError err;
        if (hooks.proxy_submit_call) {
            return hooks.proxy_submit_call(op, args, out, &err);
        }
        if (submit_sidecar) {
            return submit_sidecar->call(op, args, out, &err);
        }
        if (out) {
            out->valid = true; out->ok = false;
            out->error_code = "PROXY_DISCONNECTED";
            out->error_message = "no submit transport";
        }
        return true;
    }

    void ch_ok(const std::string& ch, bool ok) {
        std::lock_guard<std::mutex> lk(health_mu);
        auto& h = ch_health[ch];
        h.consecutive_failures = ok ? 0 : h.consecutive_failures + 1;
        h.last_ok = ok;
        /* ISO8601 UTC（Python _iso_now） */
        std::time_t t = std::time(nullptr);
        std::tm tm{};
        gmtime_s(&tm, &t);
        char b[32];
        std::strftime(b, sizeof(b), "%Y-%m-%dT%H:%M:%SZ", &tm);
        h.last_request_at = b;
    }

    void push_waiter_result(const std::string& request_id,
                            const std::string& command,
                            const jl::JsonValue& result) {
        SOCKET s = INVALID_SOCKET;
        {
            std::lock_guard<std::mutex> lk(waiter_mu);
            auto it = waiters.find(request_id);
            if (it != waiters.end()) { s = it->second; waiters.erase(it); }
        }
        if (s == INVALID_SOCKET) return;
        jl::JsonValue merged = result;
        if (merged.type != jl::JsonValue::Type::Object) {
            merged = detail::jobj({{"value", merged}});
        }
        /* Python result["request_id"] = …：覆寫而非重複鍵 */
        bool replaced = false;
        for (auto& kv : merged.object) {
            if (kv.first == "request_id") {
                kv.second = detail::jstr(request_id); replaced = true;
            }
        }
        if (!replaced)
            merged.object.emplace_back("request_id",
                                       detail::jstr(request_id));
        std::lock_guard<std::mutex> lk(send_mu);
        detail::send_all(s, detail::http_event_frame(
                      command.empty() ? "error" : command + "_result",
                      merged));
    }
};

} // namespace toolhost
} // namespace gptbridge

#endif /* _WIN32 */
#endif /* GPTBRIDGE_TOOL_HOST_INTERNAL_H */

// Suite: tool_host — M1 受管工具體行程骨架端到端（loopback winsock＋
// 注入假 proxy）。覆蓋 §2 HTTP 閘門、§3 WS 命令受理/拒絕、
// §4 claim→execute→respond→waiter 推送、toolbox_cancel_tool_run、
// /shutdown token 關閉。
#include "harness.hpp"

#ifndef _WIN32
int main() { return 0; } /* windows-only suite */
#else

#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "governed_tool.h"
#include "governed_tool_ws.h"
#include "jsonlite.h"
#include "sidecar_transport.h"
#include "tool_host.h"

namespace {

const char* SUITE = "TOOL_HOST_SUITE";
namespace jl = gptbridge::jsonlite;
namespace gtw = gptbridge::gtw;
namespace th = gptbridge::toolhost;
namespace tpx = gptbridge::tpx;

jl::JsonValue jstr(const std::string& s) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::String; v.string = s;
    return v;
}
jl::JsonValue jbool(bool b) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Bool; v.boolean = b;
    return v;
}
jl::JsonValue jnum(double n) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Number; v.number = n;
    return v;
}
jl::JsonValue jobj(std::initializer_list<
                   std::pair<std::string, jl::JsonValue>> items) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Object;
    v.object.assign(items.begin(), items.end());
    return v;
}
std::string str_val(const jl::JsonValue* v) {
    return (v && v->type == jl::JsonValue::Type::String) ? v->string
                                                       : std::string();
}

/* 假 proxy：記憶體佇列實作 request/claim/respond/cancel 等 op。 */
struct FakeProxy {
    std::mutex mu;
    std::deque<jl::JsonValue> queued;
    std::vector<std::pair<std::string, std::string>> responded;
    std::vector<std::string> cancelled;
    int hellos = 0;
    int claims = 0;
    /* notification_stamp：本地 store 寫入戳替身——bump 即「有寫入」。 */
    std::atomic<int64_t> stamp{0};
    std::atomic<int> stamp_probes{0};

    void enqueue_request(const std::string& request_id,
                         const std::string& command) {
        jl::JsonValue row = jobj({});
        row.object.emplace_back("request_id", jstr(request_id));
        jl::JsonValue pl = jobj({{"request_id", jstr(request_id)}});
        pl.object.emplace_back("_governed_command", jstr(command));
        row.object.emplace_back("payload", pl);
        row.object.emplace_back("requester_actor", jstr("t"));
        std::lock_guard<std::mutex> lk(mu);
        queued.push_back(std::move(row));
    }

    bool call(const std::string& op, const std::string& args_json,
              tpx::ProxyResponse* out, tpx::SidecarError*) {
        jl::JsonValue args;
        try { args = jl::JsonParser(args_json).parse(); }
        catch (...) { args = jl::JsonValue{}; }
        out->valid = true; out->ok = true;
        std::lock_guard<std::mutex> lk(mu);
        if (op == "hello") { ++hellos; out->result = jl::JsonValue{}; }
        else if (op == "request") {
            const jl::JsonValue* rid = args.get("request_id");
            const jl::JsonValue* cmd = args.get("command");
            const jl::JsonValue* pl = args.get("payload");
            jl::JsonValue row = jobj({});
            row.object.emplace_back("request_id",
                                    rid ? *rid : jstr(""));
            jl::JsonValue payload = jobj({});
            if (pl && pl->type == jl::JsonValue::Type::Object)
                payload = *pl;
            payload.object.emplace_back(
                "_governed_command", cmd ? *cmd : jstr(""));
            row.object.emplace_back("payload", payload);
            row.object.emplace_back("requester_actor",
                                    jstr("governance/tool/tester"));
            queued.push_back(std::move(row));
            out->result = jl::JsonValue{};
        }
        else if (op == "claim") {
            ++claims;
            /* 真實線路：result = {"request": row|null}。 */
            jl::JsonValue wrap = jobj({});
            if (queued.empty()) {
                wrap.object.emplace_back("request", jl::JsonValue{});
            } else {
                wrap.object.emplace_back(
                    "request", std::move(queued.front()));
                queued.pop_front();
            }
            out->result = std::move(wrap);
        }
        else if (op == "respond") {
            const jl::JsonValue* rid = args.get("request_id");
            const jl::JsonValue* r = args.get("response");
            responded.emplace_back(
                rid && rid->type == jl::JsonValue::Type::String
                    ? rid->string : "",
                r ? jl::json_serialize(*r) : "");
            out->result = jbool(true); /* 真實線路：純 bool */
        }
        else if (op == "request_cancelled" || op == "cancel") {
            const jl::JsonValue* rid = args.get("request_id");
            const std::string id =
                rid && rid->type == jl::JsonValue::Type::String
                    ? rid->string : "";
            bool hit = false;
            for (const auto& c : cancelled) hit = hit || c == id;
            if (op == "cancel" && !hit && !id.empty())
                cancelled.push_back(id);
            out->result = jbool(hit); /* 真實線路：純 bool */
        }
        else if (op == "notification_stamp") {
            stamp_probes.fetch_add(1);
            /* 真實線路：[mtime_ns, size] 或 null（PG）。 */
            jl::JsonValue arr;
            arr.type = jl::JsonValue::Type::Array;
            arr.array.push_back(
                jnum(static_cast<double>(stamp.load())));
            arr.array.push_back(jnum(7));
            out->result = std::move(arr);
        }
        else { out->result = jl::JsonValue{}; }
        return true;
    }
};

/* ---- loopback 客戶端工具 ---- */

SOCKET connect_loop(int port) {
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = htons(static_cast<u_short>(port));
    inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);
    if (connect(s, reinterpret_cast<sockaddr*>(&a), sizeof(a)) != 0) {
        closesocket(s);
        return INVALID_SOCKET;
    }
    return s;
}

std::string http_get(int port, const std::string& target,
                     const std::string& extra_headers = "") {
    SOCKET s = connect_loop(port);
    if (s == INVALID_SOCKET) return "";
    const std::string req = "GET " + target + " HTTP/1.1\r\n" +
                            extra_headers + "\r\n";
    send(s, req.data(), static_cast<int>(req.size()), 0);
    std::string out;
    char buf[4096];
    for (;;) {
        const int n = recv(s, buf, sizeof(buf), 0);
        if (n <= 0) break;
        out.append(buf, static_cast<size_t>(n));
        if (out.find("\r\n\r\n") != std::string::npos) break;
    }
    closesocket(s);
    return out;
}

int free_port() {
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = 0;
    inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);
    bind(s, reinterpret_cast<sockaddr*>(&a), sizeof(a));
    int len = sizeof(a);
    getsockname(s, reinterpret_cast<sockaddr*>(&a), &len);
    const int port = ntohs(a.sin_port);
    closesocket(s);
    return port;
}

/* masked client frame（server 視角入站必須 mask）。 */
std::string masked_text(const std::string& payload) {
    std::string f;
    f += '\x81';
    const size_t n = payload.size();
    if (n < 126) {
        f += static_cast<char>(0x80 | n);
    } else {
        f += static_cast<char>(0x80 | 126);
        f += static_cast<char>((n >> 8) & 0xFF);
        f += static_cast<char>(n & 0xFF);
    }
    const char mask[4] = {'m', 's', 'k', '!'};
    f.append(mask, 4);
    for (size_t i = 0; i < n; ++i)
        f += static_cast<char>(payload[i] ^ mask[i % 4]);
    return f;
}

/* client 視角解一個 server→client frame（server 端 frame 不 mask，
   故不能用 gtw::ws_frame_decode——它強制入站 masked）。
   回 >0 消耗位元組；0 需更多資料；-1 協定錯。 */
int64_t decode_server_frame(const std::string& buf, gtw::WsFrame* out) {
    const auto* p = reinterpret_cast<const uint8_t*>(buf.data());
    const size_t size = buf.size();
    if (size < 2) return 0;
    const bool fin = (p[0] & 0x80) != 0;
    const uint8_t opcode = p[0] & 0x0F;
    uint64_t len = p[1] & 0x7F;
    size_t pos = 2;
    if (p[1] & 0x80) return -1;              /* server→client 不可 mask */
    if (len == 126) {
        if (size < pos + 2) return 0;
        len = (uint64_t(p[pos]) << 8) | p[pos + 1];
        pos += 2;
    } else if (len == 127) {
        if (size < pos + 8) return 0;
        len = 0;
        for (int i = 0; i < 8; ++i) len = (len << 8) | p[pos + i];
        pos += 8;
    }
    if (size < pos + len) return 0;
    out->fin = fin;
    out->opcode = static_cast<gtw::WsOp>(opcode);
    out->payload.assign(buf.data() + pos, static_cast<size_t>(len));
    return static_cast<int64_t>(pos + len);
}

/* 讀一個 server→client frame。recv 逾時只重試；
   真正斷線/錯誤或 deadline 才回 false。 */
bool read_frame(SOCKET s, gtw::WsFrame* out, int timeout_ms = 5000) {
    /* 一次 recv 可能含多個 frame；殘留位元必須跨呼叫保留，
       否則下一幀被丟棄 → 後續讀取假性逾時。 */
    static std::map<SOCKET, std::string> pending;
    std::string& buf = pending[s];
    char tmp[4096];
    /* Winsock SO_RCVTIMEO 取 DWORD 毫秒——誤傳 timeval{0,...}
       前 4 bytes 為 0 → 逾時=無限 → recv 永久阻塞（本 bug）。 */
    DWORD rcv_ms = 100;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO,
               reinterpret_cast<const char*>(&rcv_ms),
               sizeof(rcv_ms));
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(timeout_ms);
    while (std::chrono::steady_clock::now() < deadline) {
        const int64_t used = decode_server_frame(buf, out);
        if (used > 0) { buf.erase(0, static_cast<size_t>(used)); return true; }
        if (used < 0) { pending.erase(s); return false; }
        const int n = recv(s, tmp, sizeof(tmp), 0);
        if (n > 0) {
            buf.append(tmp, static_cast<size_t>(n));
            continue;
        }
        if (n == 0) { pending.erase(s); return false; } /* peer closed */
        if (WSAGetLastError() == WSAETIMEDOUT) continue;
        pending.erase(s);
        return false;
    }
    return false;
}

const std::string TOKEN(64, 'a');
#include "suite_tool_host_cases_a.cpp"
#include "suite_tool_host_cases_b.cpp"
#include "suite_tool_host_cases_c.cpp"

} // namespace

int main() {
    NT_SUITE(SUITE);

    run_cases_a();
    run_cases_b();
    run_cases_c();

    return native_tests::report("tool_host_suite.json");
}
#endif /* _WIN32 */

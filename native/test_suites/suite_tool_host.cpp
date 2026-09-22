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
#include <cstring>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "governed_tool.h"
#include "governed_tool_ws.h"
#include "jsonlite.h"
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

/* 假 proxy：記憶體佇列實作 request/claim/respond/cancel 等 op。 */
struct FakeProxy {
    std::mutex mu;
    std::deque<jl::JsonValue> queued;
    std::vector<std::pair<std::string, std::string>> responded;
    std::vector<std::string> cancelled;
    int hellos = 0;
    int claims = 0;

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
    std::string buf;
    char tmp[4096];
    timeval tv{0, 100000};
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO,
               reinterpret_cast<const char*>(&tv), sizeof(tv));
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(timeout_ms);
    while (std::chrono::steady_clock::now() < deadline) {
        const int64_t used = decode_server_frame(buf, out);
        if (used > 0) return true;
        if (used < 0) return false;
        const int n = recv(s, tmp, sizeof(tmp), 0);
        if (n > 0) {
            buf.append(tmp, static_cast<size_t>(n));
            continue;
        }
        if (n == 0) return false;               /* peer closed */
        if (WSAGetLastError() == WSAETIMEDOUT) continue;
        return false;
    }
    return false;
}

const std::string TOKEN(64, 'a');

} // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "env_load_and_gate") {
        /* 無 env → load_env fail-closed。 */
        th::ToolHostConfig cfg;
        std::string err;
        NT_CHECK(!th::ToolHost::load_env(&cfg, &err),
                 "missing env -> deny");
        NT_CHECK(err.find("PERMISSION_DENIED") != std::string::npos,
                 "deny code");
    }
    NT_END_TEST(SUITE, "env_load_and_gate");

    NT_TEST(SUITE, "http_ws_claim_end_to_end") {
        WSADATA wsa;
        NT_CHECK(WSAStartup(MAKEWORD(2, 2), &wsa) == 0, "wsa");
        const int port = free_port();
        FakeProxy proxy;
        std::atomic<int> exec_calls{0};

        th::ToolHostConfig cfg;
        cfg.tool_id = "test-tool";
        cfg.port = port;
        cfg.session_token = TOKEN;
        cfg.shutdown_token = "sh-token";
        char wsid[GPTBRIDGE_GT_INSTANCE_ID_LEN + 1] = {0};
        gptbridge_gt_workspace_instance_id("test-tool", port, wsid);
        cfg.workspace_instance_id = wsid;
        cfg.submit_actor = "governance/tool/test-tool";
        cfg.submit_authorizer =
            "governance_rule.permission_directory.registries."
            "permissions.tool_routes:authorize_tool_self_route";

        th::ToolHostHooks hooks;
        hooks.executor = [&](const std::string& command,
                             const jl::JsonValue& payload,
                             const std::string& request_id,
                             const std::atomic<bool>&) {
            exec_calls.fetch_add(1);
            return jobj({{"ok", jbool(true)},
                         {"echo_command", jstr(command)},
                         {"echo_x", payload.get("x")
                                         ? *payload.get("x")
                                         : jl::JsonValue{}}});
        };
        hooks.proxy_call = [&](const std::string& op,
                               const std::string& args,
                               tpx::ProxyResponse* out,
                               tpx::SidecarError* e) {
            return proxy.call(op, args, out, e);
        };
        /* WS request/cancel 走 submit 側（submit 綁定）。 */
        hooks.proxy_submit_call = hooks.proxy_call;

        th::ToolHost host;
        std::string err;
        NT_CHECK(host.start(cfg, std::move(hooks), &err),
                 ("start: " + err).c_str());
        NT_CHECK(proxy.hellos == 2, "hello sent (process+submit)");

        /* /health 200＋tool_id；/metrics 200。 */
        std::string h = http_get(port, "/health");
        NT_CHECK(h.find("200") != std::string::npos, "health 200");
        NT_CHECK(h.find("test-tool") != std::string::npos,
                 "health tool_id");
        NT_CHECK(http_get(port, "/metrics").find("200") !=
                     std::string::npos,
                 "metrics 200");
        NT_CHECK(http_get(port, "/shutdown").find("403") !=
                     std::string::npos,
                 "shutdown no token 403");
        NT_CHECK(http_get(port, "/shutdown",
                          "X-GPTBridge-Shutdown-Token: wrong\r\n")
                         .find("403") != std::string::npos,
                 "shutdown wrong token 403");

        /* WS upgrade：錯 token → 403；正確 → 101。 */
        SOCKET bad = connect_loop(port);
        const std::string badreq =
            "GET /?token=" + std::string(64, 'b') + "&instance=" +
            std::string(wsid) +
            " HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n";
        send(bad, badreq.data(), static_cast<int>(badreq.size()), 0);
        char rbuf[512];
        int rn = recv(bad, rbuf, sizeof(rbuf) - 1, 0);
        rbuf[rn > 0 ? rn : 0] = '\0';
        NT_CHECK(std::string(rbuf).find("403") != std::string::npos,
                 "ws bad token 403");
        closesocket(bad);

        SOCKET ws = connect_loop(port);
        const std::string wsreq =
            "GET /?token=" + TOKEN + "&instance=" + std::string(wsid) +
            " HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n";
        send(ws, wsreq.data(), static_cast<int>(wsreq.size()), 0);
        rn = recv(ws, rbuf, sizeof(rbuf) - 1, 0);
        rbuf[rn > 0 ? rn : 0] = '\0';
        NT_CHECK(std::string(rbuf).find("101") != std::string::npos,
                 "ws upgrade 101");
        NT_CHECK(std::string(rbuf).find("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=") !=
                     std::string::npos,
                 "accept key rfc vector");

        /* 合法命令 → COMMAND_RECEIVED → claim→execute→respond→
           waiter 推送 {cmd}_result。 */
        const std::string cmd = jl::json_serialize(jobj({
            {"command", jstr("echo")},
            {"payload", jobj({{"request_id", jstr("r-1")},
                              {"tool_id", jstr("test-tool")},
                              {"x", jstr("v")}})},
        }));
        send(ws, masked_text(cmd).data(),
             static_cast<int>(masked_text(cmd).size()), 0);
        gtw::WsFrame f;
        NT_CHECK(read_frame(ws, &f), "recv COMMAND_RECEIVED");
        NT_CHECK(f.payload.find("COMMAND_RECEIVED") != std::string::npos,
                 "received event");

        NT_CHECK(read_frame(ws, &f, 8000), "recv echo_result");
        NT_CHECK(f.payload.find("echo_result") != std::string::npos,
                 "result event name");
        NT_CHECK(f.payload.find("r-1") != std::string::npos,
                 "result request_id");
        NT_CHECK(exec_calls.load() == 1, "executor ran once");
        NT_CHECK(proxy.responded.size() == 1, "respond called");

        /* 異形命令 → PERMISSION_DENIED 形式。 */
        const std::string bad2 = jl::json_serialize(jobj({
            {"command", jstr("evil")},
            {"payload", jobj({{"request_id", jstr("r-2")},
                              {"tool_id", jstr("other-tool")}})},
        }));
        send(ws, masked_text(bad2).data(),
             static_cast<int>(masked_text(bad2).size()), 0);
        NT_CHECK(read_frame(ws, &f), "recv denied result");
        NT_CHECK(f.payload.find("PERMISSION_DENIED") !=
                     std::string::npos,
                 "denied code");
        NT_CHECK(f.payload.find("evil_result") != std::string::npos,
                 "denied event name");

        /* Python `or` 語義：truthy 非字串 tool_id → deny；
           falsy（0/null）→ self → COMMAND_RECEIVED。 */
        const std::string truthy = jl::json_serialize(jobj({
            {"command", jstr("echo")},
            {"payload", jobj({{"request_id", jstr("r-t")},
                              {"tool_id", jnum(5)}})},
        }));
        send(ws, masked_text(truthy).data(),
             static_cast<int>(masked_text(truthy).size()), 0);
        NT_CHECK(read_frame(ws, &f), "recv truthy deny");
        NT_CHECK(f.payload.find("PERMISSION_DENIED") !=
                     std::string::npos,
                 "truthy non-self tool_id denied");
        const std::string falsy = jl::json_serialize(jobj({
            {"command", jstr("echo")},
            {"payload", jobj({{"request_id", jstr("r-f")},
                              {"tool_id", jnum(0)}})},
        }));
        send(ws, masked_text(falsy).data(),
             static_cast<int>(masked_text(falsy).size()), 0);
        NT_CHECK(read_frame(ws, &f), "recv falsy received");
        NT_CHECK(f.payload.find("COMMAND_RECEIVED") !=
                     std::string::npos,
                 "falsy tool_id treated as self");
        /* 消掉 r-f 的結果推送，避免干擾後續 read_frame。 */
        NT_CHECK(read_frame(ws, &f, 8000), "recv falsy result");

        /* 空 command → event="error"（非 "error_result"）。 */
        const std::string nocmd = jl::json_serialize(jobj({
            {"command", jstr("")},
            {"payload", jobj({{"request_id", jstr("r-e")}})},
        }));
        send(ws, masked_text(nocmd).data(),
             static_cast<int>(masked_text(nocmd).size()), 0);
        NT_CHECK(read_frame(ws, &f), "recv error event");
        NT_CHECK(f.payload.find("\"event\":\"error\"") !=
                     std::string::npos,
                 "empty command -> error event");
        NT_CHECK(f.payload.find("error_result") == std::string::npos,
                 "no error_result suffix");

        /* toolbox_cancel_tool_run：未知 id → ok false／cancelled false
           （Python：ok==cancelled）。 */
        const std::string cc = jl::json_serialize(jobj({
            {"command", jstr("toolbox_cancel_tool_run")},
            {"payload", jobj({{"request_id", jstr("no-such")}})},
        }));
        send(ws, masked_text(cc).data(),
             static_cast<int>(masked_text(cc).size()), 0);
        NT_CHECK(read_frame(ws, &f), "recv cancel result");
        NT_CHECK(f.payload.find("toolbox_cancel_tool_run_result") !=
                     std::string::npos,
                 "cancel result event");
        NT_CHECK(f.payload.find("\"cancelled\":false") !=
                     std::string::npos,
                 "cancelled false");
        NT_CHECK(f.payload.find("\"ok\":false") != std::string::npos,
                 "ok==cancelled (false)");

        closesocket(ws);

        /* /shutdown 正 token → 200 → run() 返回。 */
        std::thread runner([&] { host.run(); });
        const std::string shut = http_get(
            port, "/shutdown",
            "X-GPTBridge-Shutdown-Token: sh-token\r\n");
        NT_CHECK(shut.find("200") != std::string::npos,
                 "shutdown 200");
        runner.join();
        WSACleanup();
    }
    NT_END_TEST(SUITE, "http_ws_claim_end_to_end");

    NT_TEST(SUITE, "cancel_during_execution") {
        WSADATA wsa;
        NT_CHECK(WSAStartup(MAKEWORD(2, 2), &wsa) == 0, "wsa");
        const int port = free_port();
        FakeProxy proxy;
        proxy.cancelled.push_back("r-x"); /* 預置已取消 */
        std::atomic<bool> flag_seen{false};

        th::ToolHostConfig cfg;
        cfg.tool_id = "test-tool";
        cfg.port = port;
        cfg.session_token = TOKEN;
        cfg.shutdown_token = "";
        char wsid[GPTBRIDGE_GT_INSTANCE_ID_LEN + 1] = {0};
        gptbridge_gt_workspace_instance_id("test-tool", port, wsid);
        cfg.workspace_instance_id = wsid;

        th::ToolHostHooks hooks;
        hooks.executor = [&](const std::string&,
                             const jl::JsonValue&,
                             const std::string&,
                             const std::atomic<bool>& cancelled) {
            /* 等到取消旗標立起（claim 迴圈 100ms 輪詢會立它）。 */
            for (int i = 0; i < 400 && !cancelled.load(); ++i) Sleep(10);
            flag_seen = cancelled.load();
            return jobj({{"ok", jbool(true)}});
        };
        std::atomic<int> cancel_notices{0};
        hooks.cancellation = [&](const std::string&) {
            cancel_notices.fetch_add(1);
        };
        hooks.proxy_call = [&](const std::string& op,
                               const std::string& args,
                               tpx::ProxyResponse* out,
                               tpx::SidecarError* e) {
            return proxy.call(op, args, out, e);
        };

        th::ToolHost host;
        std::string err;
        NT_CHECK(host.start(cfg, std::move(hooks), &err), "start");

        /* 直接經假 proxy 佇列塞入一筆 request（繞過 WS）。 */
        {
            jl::JsonValue row = jobj({});
            row.object.emplace_back("request_id", jstr("r-x"));
            jl::JsonValue pl = jobj({{"request_id", jstr("r-x")}});
            pl.object.emplace_back("_governed_command", jstr("slow"));
            row.object.emplace_back("payload", pl);
            row.object.emplace_back("requester_actor", jstr("t"));
            std::lock_guard<std::mutex> lk(proxy.mu);
            proxy.queued.push_back(std::move(row));
        }
        /* 等 claim 輪詢到達並執行至取消（最多 8s）。 */
        for (int i = 0; i < 800 && !flag_seen.load(); ++i) Sleep(10);
        NT_CHECK(flag_seen.load(), "cancel flag reached executor");
        NT_CHECK(cancel_notices.load() >= 1, "cancellation hook fired");
        for (int i = 0; i < 200 && proxy.responded.empty(); ++i)
            Sleep(10);
        NT_CHECK(proxy.responded.empty(),
                 "cancelled request not responded (§4)");
        host.request_stop();
        host.run();
        WSACleanup();
    }
    NT_END_TEST(SUITE, "cancel_during_execution");

    return native_tests::report("tool_host_suite.json");
}
#endif /* _WIN32 */

/* tool_host.cpp — M1 受管工具體行程骨架（Windows/winsock2）。
 * 見 tool_host.h 的職責邊界：線上機械語義在此，token/路由/傳輸留 Python
 * 代理（模式 B）。非 Windows → 全部入口 fail-closed。 */

#include "tool_host.h"

#ifndef _WIN32

namespace gptbridge {
namespace toolhost {

bool ToolHost::load_env(ToolHostConfig*, std::string* error) {
    if (error) *error = "platform-unsupported";
    return false;
}
bool ToolHost::start(const ToolHostConfig&, ToolHostHooks, std::string* err) {
    if (err) *err = "platform-unsupported";
    return false;
}
void ToolHost::request_stop() {}
bool ToolHost::stopping() const { return true; }
int ToolHost::run() { return 1; }
jsonlite::JsonValue ToolHost::health_snapshot() const { return {}; }
jsonlite::JsonValue ToolHost::metrics_snapshot() const { return {}; }
ToolHost::ToolHost() = default;
ToolHost::~ToolHost() = default;

} // namespace toolhost
} // namespace gptbridge

#else /* _WIN32 */

#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <ctime>
#include <fstream>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <thread>

#include "governed_tool.h"
#include "governed_tool_ws.h"

namespace gptbridge {
namespace toolhost {

namespace {

namespace jl = jsonlite;

void th_trace(const char* tag, const std::string& v) {
    FILE* f = fopen("tool_host_trace.log", "a");
    if (f) { fprintf(f, "%s|%s\n", tag, v.c_str()); fclose(f); }
}
void (th_trace_unused_ok)(const char*, const std::string&); /* 除錯用 */
namespace gtw = gptbridge::gtw;
namespace tpx = gptbridge::tpx;

jl::JsonValue jstr(const std::string& s) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::String; v.string = s;
    return v;
}
jl::JsonValue jnum(double n) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Number; v.number = n;
    return v;
}
jl::JsonValue jbool(bool b) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Bool; v.boolean = b;
    return v;
}
jl::JsonValue jobj(std::initializer_list<
                   std::pair<std::string, jl::JsonValue>> items) {
    jl::JsonValue v; v.type = jl::JsonValue::Type::Object;
    v.object.assign(items.begin(), items.end());
    return v;
}
jl::JsonValue jparse(const std::string& s, bool* ok) {
    try {
        jl::JsonValue v = jl::JsonParser(s).parse();
        *ok = true;
        return v;
    } catch (...) {
        *ok = false;
        return jl::JsonValue{};
    }
}
const jl::JsonValue* get_str(const jl::JsonValue& v, const char* k) {
    const jl::JsonValue* p = v.get(k);
    return (p && p->type == jl::JsonValue::Type::String) ? p : nullptr;
}

const char* env_or_empty(const char* name) {
    const char* v = std::getenv(name);
    return v ? v : "";
}

bool send_all(intptr_t s, const std::string& data) {
    size_t off = 0;
    while (off < data.size()) {
        const int n = send(static_cast<SOCKET>(s), data.data() + off,
                           static_cast<int>(data.size() - off), 0);
        if (n <= 0) return false;
        off += static_cast<size_t>(n);
    }
    return true;
}

std::string http_event_frame(const std::string& event,
                             const jl::JsonValue& payload) {
    return gtw::ws_frame_encode(
        true, gtw::WsOp::Text,
        jl::json_serialize(jobj({{"event", jstr(event)},
                                 {"payload", payload}})));
}

} // namespace

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
            merged = jobj({{"value", merged}});
        }
        /* Python result["request_id"] = …：覆寫而非重複鍵 */
        bool replaced = false;
        for (auto& kv : merged.object) {
            if (kv.first == "request_id") {
                kv.second = jstr(request_id); replaced = true;
            }
        }
        if (!replaced)
            merged.object.emplace_back("request_id", jstr(request_id));
        send_all(s, http_event_frame(
                      command.empty() ? "error" : command + "_result",
                      merged));
    }
};

/* ---- §1 環境載入 ---- */

bool ToolHost::load_env(ToolHostConfig* out, std::string* error) {
    if (out == nullptr) return false;
    ToolHostConfig c;
    c.tool_id = env_or_empty("GPTBRIDGE_TOOL_ID");
    c.project_root = env_or_empty("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT");
    c.tool_dir = env_or_empty("GPTBRIDGE_TOOL_DIR");
    c.session_token = env_or_empty("GPTBRIDGE_IPC_SESSION_TOKEN");
    c.shutdown_token = env_or_empty("GPTBRIDGE_SHUTDOWN_TOKEN");
    const char* port_s = std::getenv("GPTBRIDGE_IPC_PORT");
    const std::string bootstrap =
        env_or_empty("GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP");

    auto fail = [&](const char* msg) {
        if (error) *error = msg;
        return false;
    };
    if (!gptbridge_gt_tool_id_valid(c.tool_id.c_str()))
        return fail("PERMISSION_DENIED:tool-id");
    if (port_s == nullptr || *port_s == '\0')
        return fail("PERMISSION_DENIED:port-missing");
    c.port = std::strtoll(port_s, nullptr, 10);
    if (!gptbridge_gt_port_valid(c.port))
        return fail("PERMISSION_DENIED:port-range");
    if (c.project_root.empty() || c.tool_dir.empty())
        return fail("PERMISSION_DENIED:env-root");
    if (bootstrap.empty())
        return fail("PERMISSION_DENIED:bootstrap-missing");
    /* pop-after-read：bootstrap 不得殘留於行程環境（§1）。 */
    SetEnvironmentVariableA("GPTBRIDGE_TOOL_GOVERNANCE_BOOTSTRAP", nullptr);
    if (!gptbridge_gt_env_gate(1, 1, c.session_token.c_str(), c.port, 1))
        return fail("PERMISSION_DENIED:env-gate");

    /* tool_root 校驗（workspace_root 判定樹）：root 子路徑、相對層級
       ∈{1,2}、manifest.id==tool_id；version 取 manifest（缺→1.0.0）。 */
    {
        auto norm = [](std::string s) {
            for (auto& ch : s) {
                if (ch == '\\') ch = '/';
                else ch = static_cast<char>(
                    std::tolower(static_cast<unsigned char>(ch)));
            }
            while (s.size() > 1 && s.back() == '/') s.pop_back();
            return s;
        };
        const std::string root_n = norm(c.project_root);
        const std::string tool_n = norm(c.tool_dir);
        const std::string prefix = root_n + "/";
        if (tool_n.size() <= prefix.size() ||
            tool_n.compare(0, prefix.size(), prefix) != 0)
            return fail("PERMISSION_DENIED:tool-root");
        const std::string rel = tool_n.substr(prefix.size());
        size_t depth = 1;
        for (char ch : rel) if (ch == '/') ++depth;
        if (depth > 2) return fail("PERMISSION_DENIED:tool-root-depth");

        std::ifstream mf(c.tool_dir + "\\manifest.json",
                         std::ios::binary);
        if (!mf) return fail("PERMISSION_DENIED:manifest");
        std::stringstream ss;
        ss << mf.rdbuf();
        bool mok = false;
        const jl::JsonValue manifest = jparse(ss.str(), &mok);
        const jl::JsonValue* mid =
            mok ? get_str(manifest, "id") : nullptr;
        if (mid == nullptr || mid->string != c.tool_id)
            return fail("PERMISSION_DENIED:manifest-id");
        const jl::JsonValue* mv = get_str(manifest, "version");
        if (mv != nullptr && !mv->string.empty())
            c.version = mv->string;
    }

    char wsid[GPTBRIDGE_GT_INSTANCE_ID_LEN + 1] = {0};
    gptbridge_gt_workspace_instance_id(c.tool_id.c_str(), c.port, wsid);
    c.workspace_instance_id = wsid;
    c.process_channels = {"system"};
    *out = std::move(c);
    return true;
}

/* ---- 啟動 ---- */

bool ToolHost::start(const ToolHostConfig& config, ToolHostHooks hooks,
                     std::string* err) {
    auto fail = [&](const std::string& m) {
        if (err) *err = m;
        return false;
    };
    if (!gptbridge_gt_tool_id_valid(config.tool_id.c_str()))
        return fail("PERMISSION_DENIED:tool-id");
    if (!gptbridge_gt_port_valid(config.port))
        return fail("PERMISSION_DENIED:port");
    if (!gptbridge_gt_session_token_valid(config.session_token.c_str()))
        return fail("PERMISSION_DENIED:session-token");
    if (!hooks.executor)
        return fail("executor-required");

    impl_ = std::unique_ptr<Impl>(new Impl());
    impl_->cfg = config;
    impl_->hooks = std::move(hooks);
    if (impl_->cfg.workspace_instance_id.empty()) {
        char wsid[GPTBRIDGE_GT_INSTANCE_ID_LEN + 1] = {0};
        gptbridge_gt_workspace_instance_id(
            impl_->cfg.tool_id.c_str(), impl_->cfg.port, wsid);
        impl_->cfg.workspace_instance_id = wsid;
    }
    if (impl_->cfg.process_channels.empty())
        impl_->cfg.process_channels = {"system"};

    /* proxy（process）：注入替身優先；否則 command_line → P2 sidecar。 */
    if (!impl_->hooks.proxy_call &&
        !impl_->cfg.proxy_command_line.empty()) {
        impl_->sidecar.reset(new tpx::ProxySidecar());
        tpx::SidecarError serr;
        if (!impl_->sidecar->start(impl_->cfg.proxy_command_line, &serr)) {
            const std::string m = "PROXY_SPAWN_FAILED:" + serr.message;
            impl_.reset();
            return fail(m);
        }
    }

    /* hello 綁定（process 側；無 proxy 的閘門單體模式略過）。 */
    if (impl_->hooks.proxy_call || impl_->sidecar) {
        std::vector<tpx::HelloChannel> chans;
        for (const auto& ch : impl_->cfg.process_channels)
            chans.push_back({ch, "process"});
        tpx::ProxyResponse resp;
        if (!impl_->proxy("hello",
                          tpx::args_hello(impl_->cfg.tool_id,
                                          impl_->cfg.workspace_instance_id,
                                          chans, {}),
                          &resp) ||
            !resp.ok) {
            const std::string m =
                "hello-failed:" +
                (resp.ok ? std::string("transport") : resp.error_code);
            impl_.reset();
            return fail(m);
        }
    }

    /* submit 側第二條綁定：WS request/cancel 需 mode=="submit" 通道。
       設定了 actor/authorizer → 必須有 submit 傳輸（注入或第二支
       sidecar），hello 失敗即啟動失敗（fail-closed）。 */
    const bool want_submit = !impl_->cfg.submit_actor.empty() ||
                             !impl_->cfg.submit_authorizer.empty();
    const bool have_submit_transport =
        static_cast<bool>(impl_->hooks.proxy_submit_call) ||
        !impl_->cfg.proxy_command_line_submit.empty();
    /* 組態完整性：憑證與傳輸必須成對——缺一即啟動失敗（不留半綁定）。 */
    if (want_submit && !have_submit_transport)
        return fail("submit-transport-missing");
    if (!want_submit && have_submit_transport)
        return fail("submit-binding-missing");
    if (want_submit) {
        if (!impl_->hooks.proxy_submit_call &&
            !impl_->cfg.proxy_command_line_submit.empty()) {
            impl_->submit_sidecar.reset(new tpx::ProxySidecar());
            tpx::SidecarError serr;
            if (!impl_->submit_sidecar->start(
                    impl_->cfg.proxy_command_line_submit, &serr)) {
                const std::string m =
                    "PROXY_SPAWN_FAILED:" + serr.message;
                impl_.reset();
                return fail(m);
            }
        }
        if (impl_->hooks.proxy_submit_call || impl_->submit_sidecar) {
            std::vector<tpx::HelloChannel> chans;
            std::vector<tpx::HelloSubmit> subs;
            for (const auto& ch : impl_->cfg.process_channels) {
                chans.push_back({ch, "submit"});
                subs.push_back({ch, impl_->cfg.submit_actor,
                                impl_->cfg.submit_authorizer});
            }
            tpx::ProxyResponse resp;
            if (!impl_->proxy_submit(
                    "hello",
                    tpx::args_hello(impl_->cfg.tool_id,
                                    impl_->cfg.workspace_instance_id,
                                    chans, subs),
                    &resp) ||
                !resp.ok) {
                const std::string m =
                    "submit-hello-failed:" +
                    (resp.ok ? std::string("transport")
                             : resp.error_code);
                impl_.reset();
                return fail(m);
            }
        }
    }

    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        impl_.reset();
        return fail("wsa-startup");
    }
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) {
        impl_.reset();
        return fail("socket");
    }
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<u_short>(impl_->cfg.port));
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr); /* 僅 loopback（§2） */
    if (bind(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 ||
        listen(s, 32) != 0) {
        closesocket(s);
        impl_.reset();
        return fail("bind-listen");
    }
    impl_->listen_sock = s;
    impl_->started_ms = impl_->now_ms();
    impl_->accept_thread =
        std::thread([this] { accept_loop(); });
    if (impl_->cfg.claim_loop)
        impl_->claim_thread = std::thread([this] { claim_loop(); });
    return true;
}

void ToolHost::request_stop() {
    if (!impl_) return;
    impl_->stop_flag.store(true);
    if (impl_->listen_sock != INVALID_SOCKET)
        closesocket(impl_->listen_sock); /* 喚醒 accept */
}

bool ToolHost::stopping() const {
    return impl_ && impl_->stop_flag.load();
}

int ToolHost::run() {
    if (!impl_) return 1;
    while (!impl_->stop_flag.load()) Sleep(20);
    if (impl_->accept_thread.joinable()) impl_->accept_thread.join();
    if (impl_->claim_thread.joinable()) impl_->claim_thread.join();
    {
        std::lock_guard<std::mutex> lk(impl_->conn_mu);
        for (SOCKET s : impl_->conns) closesocket(s);
        impl_->conns.clear();
    }
    if (impl_->sidecar) impl_->sidecar->stop();
    if (impl_->submit_sidecar) impl_->submit_sidecar->stop();
    WSACleanup();
    return 0;
}

ToolHost::ToolHost() = default;

ToolHost::~ToolHost() {
    if (impl_ && !impl_->stop_flag.load()) {
        request_stop();
        if (impl_->accept_thread.joinable()) impl_->accept_thread.join();
        if (impl_->claim_thread.joinable()) impl_->claim_thread.join();
        if (impl_->sidecar) impl_->sidecar->stop();
        if (impl_->submit_sidecar) impl_->submit_sidecar->stop();
        WSACleanup();
    }
}

/* ---- 觀測（GovernedToolRuntime.health_snapshot／_collect_channel_
   metrics parity） ---- */

jsonlite::JsonValue ToolHost::health_snapshot() const {
    /* channel_health：Python ChannelHealth.as_dict 形狀 */
    jl::JsonValue ch;
    ch.type = jl::JsonValue::Type::Object;
    {
        std::lock_guard<std::mutex> lk(impl_->health_mu);
        for (const auto& kv : impl_->ch_health) {
            ch.object.emplace_back(
                kv.first,
                jobj({{"channel_id", jstr(kv.first)},
                      {"last_ok", jbool(kv.second.last_ok)},
                      {"last_request_at",
                       jstr(kv.second.last_request_at)},
                      {"consecutive_failures",
                       jnum(static_cast<double>(
                           kv.second.consecutive_failures))},
                      {"degraded",
                       jbool(gptbridge_gt_health_degraded(
                           static_cast<int32_t>(
                               kv.second.consecutive_failures)) != 0)}}));
        }
    }

    jl::JsonValue channels;
    channels.type = jl::JsonValue::Type::Array;
    jl::JsonValue routes;
    routes.type = jl::JsonValue::Type::Object;
    {
        std::vector<std::string> sorted = impl_->cfg.process_channels;
        std::sort(sorted.begin(), sorted.end());
        for (const auto& c : sorted) {
            jl::JsonValue v;
            v.type = jl::JsonValue::Type::String;
            v.string = c;
            channels.array.push_back(v);
            routes.object.emplace_back(
                c, jstr(c + "-channel/" + impl_->cfg.tool_id));
        }
    }

    jl::JsonValue duty;
    duty.type = jl::JsonValue::Type::Array;
    for (const char* d :
         {"information-delivery-channels", "channel-health",
          "automatic-cleanup", "automatic-repair",
          "automatic-backup-coordination"}) {
        jl::JsonValue v;
        v.type = jl::JsonValue::Type::String;
        v.string = d;
        duty.array.push_back(v);
    }
    jl::JsonValue under;
    under.type = jl::JsonValue::Type::Array;
    for (const char* u : {"system", "maintenance"}) {
        jl::JsonValue v;
        v.type = jl::JsonValue::Type::String;
        v.string = u;
        under.array.push_back(v);
    }

    jl::JsonValue snap = jobj({
        {"ok", jbool(true)},
        {"role", jstr("tool-runtime-sub-sovereign")},
        {"sovereign_id", jstr("system")},
        {"authority",
         jstr("information-management-delivery-channels-and-"
              "channel-health-and-automatic-cleanup-repair-backup")},
        {"scope",
         jstr("all-owned-channel-delivery-health-and-local-"
              "maintenance-duties")},
        {"duty", duty},
        {"subordinate_to", under},
        {"version", jstr(impl_->cfg.version)},
        {"tool_id", jstr(impl_->cfg.tool_id)},
        {"runtime_scope", jstr("independent-tool")},
        {"governance_ready", jbool(true)},
        {"workspace_instance_id",
         jstr(impl_->cfg.workspace_instance_id)},
        {"channels", channels},
        {"channel_routes", routes},
        {"channel_health", ch},
    });
    if (impl_->cfg.self_repair_enabled &&
        impl_->hooks.self_repair_health) {
        snap.object.emplace_back(
            "_self_repair", impl_->hooks.self_repair_health());
    }
    if (impl_->cfg.local_cleanup_enabled) {
        jl::JsonValue lc;
        if (impl_->hooks.cleanup_health) {
            lc = impl_->hooks.cleanup_health();
        } else {
            lc = jobj({{"local_cleanup",
                        jobj({{"enabled", jbool(true)},
                              {"completed", jbool(false)}})}});
        }
        if (lc.type == jl::JsonValue::Type::Object)
            snap.object.emplace_back("_local_cleanup", std::move(lc));
    }
    if (impl_->hooks.health_extras) {
        jl::JsonValue extra = impl_->hooks.health_extras();
        if (extra.type == jl::JsonValue::Type::Object)
            for (auto& kv : extra.object)
                snap.object.push_back(std::move(kv));
    }
    return snap;
}

jsonlite::JsonValue ToolHost::metrics_snapshot() const {
    /* _collect_channel_metrics() parity */
    jl::JsonValue ch;
    ch.type = jl::JsonValue::Type::Object;
    {
        std::lock_guard<std::mutex> lk(impl_->health_mu);
        for (const auto& kv : impl_->ch_health) {
            ch.object.emplace_back(
                kv.first,
                jobj({{"channel_id", jstr(kv.first)},
                      {"last_ok", jbool(kv.second.last_ok)},
                      {"last_request_at",
                       jstr(kv.second.last_request_at)},
                      {"consecutive_failures",
                       jnum(static_cast<double>(
                           kv.second.consecutive_failures))},
                      {"degraded",
                       jbool(gptbridge_gt_health_degraded(
                           static_cast<int32_t>(
                               kv.second.consecutive_failures)) != 0)}}));
        }
    }
    jl::JsonValue processing;
    processing.type = jl::JsonValue::Type::Array;
    for (const auto& c : impl_->cfg.process_channels) {
        jl::JsonValue v;
        v.type = jl::JsonValue::Type::String;
        v.string = c;
        processing.array.push_back(v);
    }
    int64_t waiters = 0;
    {
        std::lock_guard<std::mutex> lk(impl_->waiter_mu);
        waiters = static_cast<int64_t>(impl_->waiters.size());
    }
    return jobj({
        {"channel_health", ch},
        {"worker_queue_size", jnum(static_cast<double>(waiters))},
        {"processing_channels", processing},
        {"notification_queue_size", jnum(0)},
        {"last_notification", jl::JsonValue{}},
        {"uptime_seconds",
         jnum((impl_->now_ms() - impl_->started_ms.load()) / 1000.0)},
    });
}

/* ---- accept / 連線 ---- */

void ToolHost::accept_loop() {
    while (!impl_->stop_flag.load()) {
        SOCKET c = accept(impl_->listen_sock, nullptr, nullptr);
        if (c == INVALID_SOCKET) {
            if (impl_->stop_flag.load()) break;
            continue;
        }
        impl_->n_connections.fetch_add(1);
        {
            std::lock_guard<std::mutex> lk(impl_->conn_mu);
            impl_->conns.insert(c);
        }
        std::thread([this, c] { conn_loop(c); }).detach();
    }
}

void ToolHost::conn_loop(intptr_t sock) {
    const SOCKET c = static_cast<SOCKET>(sock);
    std::string buf;
    buf.reserve(8192);
    char tmp[8192];
    gtw::HttpRequest req;
    size_t consumed = 0;
    bool parsed = false;
    while (!impl_->stop_flag.load()) {
        const int n = recv(c, tmp, sizeof(tmp), 0);
        if (n <= 0) goto done;
        buf.append(tmp, static_cast<size_t>(n));
        if (buf.size() > 16 * 1024) goto done;
        if (buf.find("\r\n\r\n") != std::string::npos) {
            parsed = gtw::http_request_parse(
                reinterpret_cast<const uint8_t*>(buf.data()), buf.size(),
                &req, &consumed);
            break;
        }
    }
    if (!parsed) goto done;

    switch (gtw::route_request(req, impl_->cfg.session_token,
                               impl_->cfg.workspace_instance_id,
                               impl_->cfg.shutdown_token)) {
    case gtw::GateDecision::Health:
        send_all(c, gtw::http_ok_bytes(
                        jl::json_serialize(health_snapshot()),
                        "application/json"));
        break;
    case gtw::GateDecision::Metrics:
        send_all(c, gtw::http_ok_bytes(
                        jl::json_serialize(metrics_snapshot()),
                        "application/json"));
        break;
    case gtw::GateDecision::Shutdown:
        send_all(c, gtw::http_ok_bytes("OK", "text/plain"));
        impl_->stop_flag.store(true);
        break;
    case gtw::GateDecision::Upgrade: {
        std::string accept_key;
        if (!gtw::ws_validate_upgrade(req, &accept_key)) {
            send_all(c, gtw::http_forbidden_bytes());
            break;
        }
        const char* key = req.header("sec-websocket-key");
        if (!send_all(c, gtw::ws_upgrade_response(key ? key : ""))) break;
        impl_->n_upgrades.fetch_add(1);
        ws_loop(c, buf.substr(consumed));
        break;
    }
    case gtw::GateDecision::Forbidden:
    default:
        send_all(c, gtw::http_forbidden_bytes());
        break;
    }
done:
    {
        std::lock_guard<std::mutex> lk(impl_->conn_mu);
        impl_->conns.erase(c);
    }
    closesocket(c);
}

void ToolHost::ws_loop(intptr_t sock, std::string pending) {
    const SOCKET c = static_cast<SOCKET>(sock);
    std::string buf = std::move(pending);
    std::string message; /* text/binary + continuation 重組（≤1MiB） */
    bool in_message = false;
    char tmp[8192];
    while (!impl_->stop_flag.load()) {
        gtw::WsFrame f;
        const int64_t used = gtw::ws_frame_decode(
            reinterpret_cast<const uint8_t*>(buf.data()), buf.size(), &f);
        if (used < 0) { th_trace("decode-err", buf.substr(0,24)); break; }
        if (used > 0) {
            buf.erase(0, static_cast<size_t>(used));
            if (f.opcode == gtw::WsOp::Ping) {
                if (!send_all(c, gtw::ws_pong(f.payload))) break;
            } else if (f.opcode == gtw::WsOp::Close) {
                send_all(c, gtw::ws_close(1000, ""));
                break;
            } else if (f.opcode == gtw::WsOp::Text ||
                       f.opcode == gtw::WsOp::Binary ||
                       f.opcode == gtw::WsOp::Continuation) {
                /* Python websockets 以「訊息」為單位交付：首幀
                   text/binary＋continuation 至 FIN；binary 同樣餵
                   json.loads（bytes 可解析）。孤立 continuation 或
                   訊息途中新 data frame → 協定錯關閉。 */
                if (f.opcode == gtw::WsOp::Continuation) {
                    if (!in_message) break;
                } else {
                    if (in_message) break;
                    in_message = true;
                }
                message.append(f.payload);
                if (message.size() > 1024 * 1024) break;
                if (f.fin) {
                    handle_ws_message(c, message);
                    message.clear();
                    in_message = false;
                }
            }
            continue;
        }
        const int n = recv(c, tmp, sizeof(tmp), 0);
        if (n <= 0) { th_trace("ws-recv", std::to_string(n)); break; }
        buf.append(tmp, static_cast<size_t>(n));
    }
    /* 連線結束：移除指向本 socket 的 waiters（不回推結果）。 */
    std::lock_guard<std::mutex> lk(impl_->waiter_mu);
    for (auto it = impl_->waiters.begin(); it != impl_->waiters.end();) {
        it = (it->second == c) ? impl_->waiters.erase(it) : std::next(it);
    }
}

/* ---- WS 命令（§3） ---- */

void ToolHost::send_event(intptr_t sock, const std::string& event,
                          const jl::JsonValue& payload) {
    std::lock_guard<std::mutex> lk(impl_->send_mu);
    send_all(sock, http_event_frame(event, payload));
}

void ToolHost::send_result_error(intptr_t sock, const std::string& command,
                                 const std::string& request_id,
                                 const std::string& code) {
    /* Python：event = f"{command}_result" if command else "error"。 */
    const std::string event =
        command.empty() ? "error" : command + "_result";
    send_event(sock, event,
               jobj({{"ok", jbool(false)},
                     {"tool_id", jstr(impl_->cfg.tool_id)},
                     {"request_id", jstr(request_id)},
                     {"error_code", jstr(code)},
                     {"message", jstr(code)}}));
}

void ToolHost::handle_ws_message(intptr_t sock, const std::string& text) {
    const SOCKET c = static_cast<SOCKET>(sock);
    impl_->n_commands.fetch_add(1);
    bool ok = false;
    const jl::JsonValue msg = jparse(text, &ok);
    const jl::JsonValue* cmd =
        (ok && msg.type == jl::JsonValue::Type::Object)
            ? get_str(msg, "command")
            : nullptr;
    const std::string command = cmd ? cmd->string : "";
    const jl::JsonValue* payload =
        (ok && msg.type == jl::JsonValue::Type::Object)
            ? msg.get("payload")
            : nullptr;
    const std::string request_id = [&] {
        const jl::JsonValue* r =
            (payload && payload->type == jl::JsonValue::Type::Object)
                ? get_str(*payload, "request_id")
                : nullptr;
        return r ? r->string : std::string();
    }();

    /* Python：非 dict 訊息／空 command／payload 非 dict → except →
       "{command}_result"（或 "error"）PERMISSION_DENIED。 */
    if (!ok || msg.type != jl::JsonValue::Type::Object ||
        command.empty() || payload == nullptr ||
        payload->type != jl::JsonValue::Type::Object) {
        impl_->n_denied.fetch_add(1);
        send_result_error(c, command, request_id, "PERMISSION_DENIED");
        return;
    }

    /* §3.1 toolbox_cancel_tool_run：submit 側 cancel 經代理。 */
    if (command == "toolbox_cancel_tool_run") {
        tpx::ProxyResponse resp;
        bool cancelled = false;
        const std::string& ch = impl_->cfg.process_channels.front();
        if (!request_id.empty() &&
            impl_->proxy_submit("cancel",
                                tpx::args_request_id(ch, request_id),
                                &resp) &&
            resp.ok &&
            resp.result.type == jl::JsonValue::Type::Bool) {
            cancelled = resp.result.boolean;
        }
        /* 本地執行中 request：立取消旗標＋工具自備取消。 */
        std::shared_ptr<std::atomic<bool>> flag;
        {
            std::lock_guard<std::mutex> lk(impl_->exec_mu);
            auto it = impl_->exec_flags.find(request_id);
            if (it != impl_->exec_flags.end()) flag = it->second;
        }
        if (flag) {
            flag->store(true);
            if (impl_->hooks.cancellation)
                impl_->hooks.cancellation(request_id);
            cancelled = true;
        }
        /* Python：{"ok": cancelled, "cancelled": cancelled, …} */
        send_event(c, "toolbox_cancel_tool_run_result",
                   jobj({{"ok", jbool(cancelled)},
                         {"cancelled", jbool(cancelled)},
                         {"tool_id", jstr(impl_->cfg.tool_id)},
                         {"request_id", jstr(request_id)}}));
        return;
    }

    /* §3.1 前置校驗 → PERMISSION_DENIED 形式。
       payload.tool_id 的 Python `or` 語義：falsy（null/false/0/""/
       []/{}）→ self；truthy 非字串或字串 ≠ tool_id → deny。 */
    const jl::JsonValue* tid = payload->get("tool_id");
    bool tid_deny = false;
    if (tid != nullptr) {
        switch (tid->type) {
            case jl::JsonValue::Type::Null: break;
            case jl::JsonValue::Type::Bool:
                tid_deny = tid->boolean; break;
            case jl::JsonValue::Type::Number:
                tid_deny = tid->number != 0; break;
            case jl::JsonValue::Type::String:
                tid_deny =
                    !tid->string.empty() &&
                    tid->string != impl_->cfg.tool_id;
                break;
            case jl::JsonValue::Type::Array:
                tid_deny = !tid->array.empty(); break;
            case jl::JsonValue::Type::Object:
                tid_deny = !tid->object.empty(); break;
        }
    }
    const bool valid =
        !tid_deny &&
        gptbridge_gt_request_valid(
            command.c_str(), 1, request_id.c_str(),
            tid && tid->type == jl::JsonValue::Type::String
                ? tid->string.c_str() : nullptr,
            impl_->cfg.tool_id.c_str()) != 0;
    if (!valid) {
        impl_->n_denied.fetch_add(1);
        send_result_error(c, command, request_id, "PERMISSION_DENIED");
        return;
    }

    /* §3.2 受理：waiter 先登記（Python：waiters[rid]=ws 在 request
       之前）→ submit 入傳輸 → COMMAND_RECEIVED；submit 失敗 → 撤
       waiter＋PERMISSION_DENIED（Python except 統一 DENIED）。 */
    {
        std::lock_guard<std::mutex> lk(impl_->waiter_mu);
        impl_->waiters[request_id] = c;
    }
    tpx::ProxyResponse resp;
    const std::string& ch = impl_->cfg.process_channels.front();
    if (!impl_->proxy_submit(
            "request",
            tpx::args_submit_request(ch, impl_->cfg.tool_id, command,
                                     jl::json_serialize(*payload),
                                     request_id),
            &resp) ||
        !resp.ok) {
        {
            std::lock_guard<std::mutex> lk(impl_->waiter_mu);
            impl_->waiters.erase(request_id);
        }
        impl_->n_denied.fetch_add(1);
        send_result_error(c, command, request_id, "PERMISSION_DENIED");
        return;
    }
    th_trace("received", command + "/" + request_id);
    send_event(c, "COMMAND_RECEIVED",
               jobj({{"command", jstr(command)},
                     {"status", jstr("processing")}}));
}

/* ---- claim / execute / respond（§4） ---- */

void ToolHost::claim_loop() {
    int64_t idle_ms = GPTBRIDGE_GT_IDLE_INITIAL_MS;
    while (!impl_->stop_flag.load()) {
        bool got = false;
        for (const auto& ch : impl_->cfg.process_channels) {
            if (impl_->stop_flag.load()) return;
            tpx::ProxyResponse resp;
            if (!impl_->proxy("claim", tpx::args_channel(ch), &resp)) {
                impl_->n_claim_fail.fetch_add(1);
                impl_->ch_ok(ch, false);
                Sleep(500); /* claim 失敗 0.5s 退避（§4） */
                continue;
            }
            impl_->ch_ok(ch, true);
            if (!resp.ok) {
                impl_->n_claim_fail.fetch_add(1);
                Sleep(500);
                continue;
            }
            /* 線路形狀：result = {"request": row|null}。 */
            const jl::JsonValue* row =
                (resp.result.type == jl::JsonValue::Type::Object)
                    ? resp.result.get("request")
                    : nullptr;
            if (row == nullptr ||
                row->type != jl::JsonValue::Type::Object)
                continue; /* null → 空佇列 */
            got = true;
            impl_->n_claims.fetch_add(1);
            execute_claimed(ch, *row);
        }
        if (got) {
            idle_ms = GPTBRIDGE_GT_IDLE_INITIAL_MS;
            continue;
        }
        idle_ms = gptbridge_gt_idle_next_ms(idle_ms, 0);
        const int64_t wait =
            gptbridge_gt_wait_timeout_ms(idle_ms, 0);
        const int64_t deadline = impl_->now_ms() + wait;
        while (!impl_->stop_flag.load() && impl_->now_ms() < deadline)
            Sleep(10);
    }
}

void ToolHost::execute_claimed(const std::string& channel,
                               const jl::JsonValue& row) {
    const jl::JsonValue* rid = get_str(row, "request_id");
    const jl::JsonValue* payload = row.get("payload");
    const std::string request_id = rid ? rid->string : "";
    jl::JsonValue pl =
        (payload && payload->type == jl::JsonValue::Type::Object)
            ? *payload
            : jl::JsonValue{};
    std::string command;
    std::vector<std::pair<std::string, jl::JsonValue>> kept;
    kept.reserve(pl.object.size());
    for (auto& kv : pl.object) {
        if (kv.first == "_governed_command") {
            if (kv.second.type == jl::JsonValue::Type::String)
                command = kv.second.string;
            continue;
        }
        kept.push_back(std::move(kv));
    }
    pl.object = std::move(kept);
    /* Python：payload["_governed_requester_actor"] = requester_actor。 */
    const jl::JsonValue* ra = row.get("requester_actor");
    const std::string requester =
        (ra && ra->type == jl::JsonValue::Type::String) ? ra->string : "";
    pl.object.emplace_back("_governed_requester_actor",
                           jstr(requester));
    const bool is_local_cleanup =
        command == "toolbox_run_local_cleanup";

    /* 前置失敗／LOCAL_CLEANUP 非 governance actor → DENIED result
       （Python except 路徑：respond＋waiter 推送、不進 executor）。 */
    const bool early_denied =
        rid == nullptr || payload == nullptr ||
        payload->type != jl::JsonValue::Type::Object ||
        command.empty() ||
        (is_local_cleanup &&
         (requester != "governance/main-system" ||
          !impl_->hooks.local_cleanup));
    if (early_denied) {
        jl::JsonValue denied = jobj({
            {"ok", jbool(false)},
            {"tool_id", jstr(impl_->cfg.tool_id)},
            {"request_id", jstr(request_id)},
            {"error_code", jstr("PERMISSION_DENIED")},
            {"message", jstr("PERMISSION_DENIED")}});
        tpx::ProxyResponse resp;
        impl_->proxy("respond",
                     tpx::args_respond(channel, request_id,
                                       jl::json_serialize(denied)),
                     &resp);
        impl_->push_waiter_result(request_id, command, denied);
        return;
    }

    auto flag = std::make_shared<std::atomic<bool>>(false);
    {
        std::lock_guard<std::mutex> lk(impl_->exec_mu);
        impl_->exec_flags[request_id] = flag;
    }

    /* §4：executor 跑於獨立執行緒；本執行緒每 100ms 輪詢
       request_cancelled——命中即立旗標＋呼叫 cancellation、
       **不 respond**（Python 同款）。 */
    std::atomic<bool> done{false};
    jl::JsonValue result;
    std::thread worker([this, &command, &pl, &request_id, flag, &done,
                        &result] {
        if (command == "toolbox_run_local_cleanup" &&
            impl_->hooks.local_cleanup) {
            result = impl_->hooks.local_cleanup();
        } else {
            result = impl_->hooks.executor(command, pl, request_id,
                                           *flag);
        }
        done.store(true);
    });

    bool cancelled = false;
    while (!done.load() && !impl_->stop_flag.load()) {
        if (flag->load()) { cancelled = true; break; }
        tpx::ProxyResponse resp;
        /* 線路形狀：result 為純 bool。 */
        if (impl_->proxy("request_cancelled",
                         tpx::args_request_id(channel, request_id),
                         &resp) &&
            resp.ok &&
            resp.result.type == jl::JsonValue::Type::Bool &&
            resp.result.boolean) {
            flag->store(true);
            if (impl_->hooks.cancellation)
                impl_->hooks.cancellation(request_id);
            cancelled = true;
            break;
        }
        const int64_t until = impl_->now_ms() + 100;
        while (!done.load() && !impl_->stop_flag.load() &&
               impl_->now_ms() < until)
            Sleep(5);
    }
    if (worker.joinable()) worker.join();
    {
        std::lock_guard<std::mutex> lk(impl_->exec_mu);
        impl_->exec_flags.erase(request_id);
    }

    if (cancelled) {
        impl_->n_cancelled.fetch_add(1);
        std::lock_guard<std::mutex> lk(impl_->waiter_mu);
        impl_->waiters.erase(request_id); /* 不回推 _result */
        return;
    }
    impl_->n_executed.fetch_add(1);

    /* respond（僅 claimed 成功——代理/傳輸層保證語義）。 */
    jl::JsonValue response = result;
    if (response.type != jl::JsonValue::Type::Object)
        response = jobj({{"value", response}});
    /* Python result["request_id"] = …：覆寫而非重複鍵。 */
    bool replaced = false;
    for (auto& kv : response.object) {
        if (kv.first == "request_id") {
            kv.second = jstr(request_id); replaced = true;
        }
    }
    if (!replaced)
        response.object.emplace_back("request_id", jstr(request_id));
    tpx::ProxyResponse resp;
    if (impl_->proxy("respond",
                     tpx::args_respond(channel, request_id,
                                       jl::json_serialize(response)),
                     &resp) &&
        resp.ok) {
        impl_->n_responded.fetch_add(1);
        impl_->ch_ok(channel, true);
    } else {
        impl_->n_claim_fail.fetch_add(1);
        impl_->ch_ok(channel, false);
        /* respond 失敗 → PERMISSION_DENIED 形式（Python except 路徑）。 */
        response = jobj({
            {"ok", jbool(false)},
            {"tool_id", jstr(impl_->cfg.tool_id)},
            {"request_id", jstr(request_id)},
            {"error_code", jstr("PERMISSION_DENIED")},
            {"message", jstr("PERMISSION_DENIED")}});
    }
    impl_->push_waiter_result(request_id, command, response);
}

} // namespace toolhost
} // namespace gptbridge

#endif /* _WIN32 */

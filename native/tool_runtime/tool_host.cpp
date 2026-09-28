/* tool_host.cpp — M1 受管工具體行程骨架（Windows/winsock2）：§1 env
 * 載入、啟動閘門、listen/accept 入列、生命週期與排空。協議層
 * （conn_loop/ws_loop/WS 命令）在 tool_host_conn.cpp，claim/
 * execute/respond 在 tool_host_claims.cpp，觀測在
 * tool_host_observe.cpp；Impl 與線上助手共享於
 * tool_host_internal.h。職責邊界見 tool_host.h：線上機械語義在
 * 此，token/路由/傳輸留 Python 代理（模式 B）。
 * 非 Windows → 全部入口 fail-closed。 */

#include "tool_host.h"

#ifndef _WIN32

namespace gptbridge {
namespace toolhost {

bool ToolHost::load_env(ToolHostConfig*, std::string* error) {
    if (error) *error = "platform-unsupported";
    return false;
}
std::string ToolHost::issue_test_gate_proof() { return std::string(); }
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

#include "tool_host_internal.h"

namespace gptbridge {
namespace toolhost {

using namespace detail;

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
    c.env_gate_passed = true;   /* 全閘序列通過 —— start() 只接受此證明 */
    c.env_gate_proof = mint_gate_proof();  /* 登錄區 nonce：不可自構 */
    if (c.env_gate_proof.empty())
        return fail("PERMISSION_DENIED:gate-proof-mint");
    *out = std::move(c);
    return true;
}

std::string ToolHost::issue_test_gate_proof() {
    /* 測試接縫：直構 config 的套件測試用；生產路徑一律走 load_env()。 */
    return mint_gate_proof();
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
    if (!config.env_gate_passed ||
        !gate_proof_registered(config.env_gate_proof))
        return fail("PERMISSION_DENIED:env-gate-bypass");
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
    /* bounded-concurrency/v1：worker 數＝governor network 配額 clamp
       於宣告 envelope；讀不到 → fail-open 用 max。 */
    {
        const int quota = governor_class_quota(
            impl_->cfg.project_root, "network");
        int workers = quota > 0 ? quota : impl_->cfg.conn_workers_max;
        workers = (std::max)(impl_->cfg.conn_workers_min,
                             (std::min)(impl_->cfg.conn_workers_max,
                                        workers));
        impl_->conn_workers = workers;
        impl_->conn_pool.reserve(static_cast<size_t>(workers));
        for (int i = 0; i < workers; ++i)
            impl_->conn_pool.emplace_back([this] { conn_worker(); });
    }
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
    drain_connections();
    if (impl_->sidecar) impl_->sidecar->stop();
    if (impl_->submit_sidecar) impl_->submit_sidecar->stop();
    WSACleanup();
    return 0;
}

/* 排空（bounded-concurrency/v1）：關 pending 佇列的未受理連線＋
   關活動 conns（recv 即返回）→ notify 全池 → join conn workers。
   歸零前不得釋放 impl_／WSACleanup（UAF 防線）。 */
void ToolHost::drain_connections() {
    {
        std::lock_guard<std::mutex> lk(impl_->pending_mu);
        for (SOCKET s : impl_->pending_conns) closesocket(s);
        impl_->pending_conns.clear();
    }
    impl_->pending_cv.notify_all();
    {
        std::lock_guard<std::mutex> lk(impl_->conn_mu);
        for (SOCKET s : impl_->conns) closesocket(s);
        impl_->conns.clear();
    }
    for (std::thread& w : impl_->conn_pool)
        if (w.joinable()) w.join();
    impl_->conn_pool.clear();
    while (impl_->active_conns.load() > 0) Sleep(1);
}

ToolHost::ToolHost() = default;

ToolHost::~ToolHost() {
    if (impl_ && !impl_->stop_flag.load()) {
        request_stop();
        if (impl_->accept_thread.joinable()) impl_->accept_thread.join();
        if (impl_->claim_thread.joinable()) impl_->claim_thread.join();
        drain_connections();
        if (impl_->sidecar) impl_->sidecar->stop();
        if (impl_->submit_sidecar) impl_->submit_sidecar->stop();
        WSACleanup();
    }
    if (impl_) {
        /* 同 run()：conn pool 排空後才可釋放 impl_。 */
        while (impl_->active_conns.load() > 0) Sleep(1);
    }
}

/* ---- accept / 入列（bounded-concurrency/v1） ---- */

void ToolHost::accept_loop() {
    while (!impl_->stop_flag.load()) {
        SOCKET c = accept(impl_->listen_sock, nullptr, nullptr);
        if (c == INVALID_SOCKET) {
            if (impl_->stop_flag.load()) break;
            continue;
        }
        impl_->n_connections.fetch_add(1);
        /* capacity + drop/reject：pending 佇列滿 → 立即 503 關閉，
           絕不為新連線再生產執行緒。 */
        {
            std::lock_guard<std::mutex> lk(impl_->pending_mu);
            const size_t cap = static_cast<size_t>(
                (std::max)(1, impl_->cfg.pending_conn_capacity));
            if (impl_->pending_conns.size() >= cap) {
                impl_->n_conn_rejected.fetch_add(1);
                send_all(c, kHttp503);
                closesocket(c);
                continue;
            }
            impl_->pending_conns.push_back(c);
        }
        impl_->pending_cv.notify_one();
    }
}

/* 固定池工作緒：從有界 pending 佇列取連線，跑完整 conn_loop 生命
   週期。stop_flag＋佇列空 → 收操（run() 排空後 join）。 */
void ToolHost::conn_worker() {
    for (;;) {
        SOCKET c = INVALID_SOCKET;
        {
            std::unique_lock<std::mutex> lk(impl_->pending_mu);
            impl_->pending_cv.wait(lk, [&] {
                return impl_->stop_flag.load() ||
                       !impl_->pending_conns.empty();
            });
            if (impl_->pending_conns.empty()) return;
            c = impl_->pending_conns.front();
            impl_->pending_conns.pop_front();
        }
        {
            /* active_conns 與 conns 同鎖入帳：在 conn_loop 前排程
               就計數，run() 排空才不會在「已出列未啟動」窗口漏數。 */
            std::lock_guard<std::mutex> lk(impl_->conn_mu);
            impl_->conns.insert(c);
            impl_->active_conns.fetch_add(1);
        }
        conn_loop(c);
    }
}

} // namespace toolhost
} // namespace gptbridge

#endif /* _WIN32 */

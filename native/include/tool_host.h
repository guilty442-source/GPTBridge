/* tool_host.h — M1 受管工具體行程骨架（star-governed-tool-runtime-abi/v1
 * ＋ star-governed-transport-proxy/v1 模式 B 接線）
 *
 * 組合既有零 I/O 件成可執行行程：
 *   governed_tool.c    → §1 env/identity/§2 閘門/§3.1 命令前置校驗/§4 backoff
 *   governed_tool_ws   → HTTP 解析、route 階梯、WS 握手/帧編解碼
 *   transport_proxy_client → proxy op 編解碼
 *   sidecar_transport  → P2 Python 代理子行程（預設；可注入替身）
 *
 * 職責邊界（模式 B）：本行程只做線上機械語義——監聽、閘門、命令
 * 受理、claim/execute/respond 迴圈、取消輪詢。token 發行、路由授權、
 * 傳輸庫全留 Python 代理；`claim`/`respond`/`request`/`cancel` 一律
 * 經 proxy call。
 *
 * Windows-only（winsock2）；其他平台 fail-closed。
 */
#ifndef GPTBRIDGE_TOOL_HOST_H
#define GPTBRIDGE_TOOL_HOST_H

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "jsonlite.h"
#include "sidecar_transport.h"

namespace gptbridge {
namespace toolhost {

struct ToolHostConfig {
    std::string tool_id;                 /* §1 正則校驗 */
    int64_t port = 0;                    /* §1 1024–65535 */
    std::string session_token;           /* §1 64-hex */
    std::string shutdown_token;          /* 空 → /shutdown 恆 403 */
    std::string workspace_instance_id;   /* 空 → 依 tool_id:port 計算 */
    std::string project_root;
    std::string tool_dir;
    std::string version = "1.0.0";   /* manifest.json version（health） */
    /* proxy（process 側）：command_line 非空 → spawn P2 sidecar；
       空 → 必須注入 Hooks::proxy_call（測試/P1 常駐代理接線）。 */
    std::string proxy_command_line;
    /* proxy（submit 側）：WS request/cancel 是 submit ops——同一通道
       不能同時 process＋submit（每 hello 單一模式；process 通道收
       request → CHANNEL_NOT_BOUND），故 submit 走**第二條**綁定：
       注入 proxy_submit_call 或 spawn 第二支 sidecar。空則 WS 命令
       受理 fail-closed（PERMISSION_DENIED）。 */
    std::string proxy_command_line_submit;
    /* hello 綁定：process 通道（預設 {"system"}）；submit 綁定供 WS
       命令 submit 路徑——actor/authorizer 空則不綁 submit。 */
    std::vector<std::string> process_channels;
    std::string submit_actor;      /* 例 "governance/tool/<id>" */
    std::string submit_authorizer; /* 例 "module:function" */
    bool claim_loop = true;        /* false → 僅 HTTP/WS 閘門（閘門單體測試） */
    /* 閘門證明：僅 load_env() 跑完 bootstrap/env-gate/tool-root/manifest
       全序列才置位；start() 拒絕未經 load_env 的注入 config（P7 修復——
       此前 start 僅驗 id/port/token，可繞過 bootstrap env 閘）。 */
    bool env_gate_passed = false;
    /* health_snapshot parity 區段（對齊 GovernedToolRuntime）。 */
    bool local_cleanup_enabled = true;
    bool self_repair_enabled = false;
    /* §10.65 dual-track 標記：shadow/parity 觀察期間與 Python 正典
       並行時置 true；primary 切換後應為 false。僅觀測語意，稽核
       可據此拒絕「假 primary」申報。 */
    bool dual_track = false;
};

/* proxy 呼叫抽象：`op`+args_json → ProxyResponse；回 false＝傳輸層失敗。 */
using ProxyCallFn = std::function<bool(const std::string& op,
                                       const std::string& args_json,
                                       tpx::ProxyResponse* out,
                                       tpx::SidecarError* err)>;

struct ToolHostHooks {
    /* 工具業務執行器：收到命令＋payload＋request_id＋可輪詢的取消旗標，
       回傳 result dict（respond 時自動 ∪ {request_id}）。 */
    std::function<jsonlite::JsonValue(const std::string& command,
                                      const jsonlite::JsonValue& payload,
                                      const std::string& request_id,
                                      const std::atomic<bool>& cancelled)>
        executor;
    /* 取消通知（可空）：cancelled 旗標已立後額外呼叫工具自備取消。 */
    std::function<void(const std::string& request_id)> cancellation;
    /* 注入 proxy（可空）：空 → 以 config.proxy_command_line spawn sidecar。 */
    ProxyCallFn proxy_call;
    /* 注入 submit 側 proxy（可空）：WS request/cancel 專用（submit
       綁定）；空 → 以 config.proxy_command_line_submit spawn 第二支
       sidecar；兩者皆空 → WS 命令受理一律 PERMISSION_DENIED。 */
    ProxyCallFn proxy_submit_call;
    /* LOCAL_CLEANUP 委派（可空）：command==toolbox_run_local_cleanup
       且 requester_actor==governance/main-system 時呼叫，回 result
       dict；空 → PERMISSION_DENIED（非 governance actor 恆 DENIED）。 */
    std::function<jsonlite::JsonValue()> local_cleanup;
    /* /health 注入欄位（可空）：回傳 dict，其鍵併入快照。 */
    std::function<jsonlite::JsonValue()> health_extras;
    /* /health 的 _local_cleanup／_self_repair 區段資料（可空）。 */
    std::function<jsonlite::JsonValue()> cleanup_health;
    std::function<jsonlite::JsonValue()> self_repair_health;
};

class ToolHost {
public:
    ToolHost();              /* pimpl：於 cpp（Impl 完整處）定義 */
    ~ToolHost();
    ToolHost(const ToolHost&) = delete;
    ToolHost& operator=(const ToolHost&) = delete;

    /* §1：從環境變數載入啟動契約（含 wsid 計算；bootstrap env 讀後
       自行程環境移除）。任一必要件缺失/不合法 → false + error。 */
    static bool load_env(ToolHostConfig* out, std::string* error);

    /* 啟動：hello 綁定（若有 proxy）→ winsock 監聽 127.0.0.1:port →
       accept/claim 執行緒。失敗回 false + err（fail-closed）。 */
    bool start(const ToolHostConfig& config, ToolHostHooks hooks,
               std::string* err);

    void request_stop();   /* 外部/測試觸發停止（幂等）。 */
    bool stopping() const;
    int run();             /* 阻塞至 shutdown；回 0。 */

    /* 觀測（執行緒安全快照）。 */
    jsonlite::JsonValue health_snapshot() const;
    jsonlite::JsonValue metrics_snapshot() const;

private:
    void accept_loop();
    void conn_loop(intptr_t sock);
    void ws_loop(intptr_t sock, std::string pending);
    void handle_ws_message(intptr_t sock, const std::string& text);
    void send_event(intptr_t sock, const std::string& event,
                    const jsonlite::JsonValue& payload);
    void send_result_error(intptr_t sock, const std::string& command,
                           const std::string& request_id,
                           const std::string& code);
    void claim_loop();
    void execute_claimed(const std::string& channel,
                         const jsonlite::JsonValue& row);

    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace toolhost
} // namespace gptbridge

#endif /* GPTBRIDGE_TOOL_HOST_H */

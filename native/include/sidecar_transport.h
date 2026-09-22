/* sidecar_transport.h — star-governed-transport-proxy/v1 P2 sidecar
 * 接線層（M1 模式 B）：spawn Python 代理子行程＋stdio 管道＋
 * id 對帳的同步呼叫。
 *
 * 職責（僅此）：
 *  - 以受管命令列啟動代理子行程（繼承受管 env）
 *  - 寫請求行、讀回應行，以 `id` 對帳（丟棄行依協定略過）
 *  - 連線中斷 → 所有未完成請求視為失敗（PROXY_DISCONNECTED）
 *
 * 不在本層：token、路由政策、傳輸庫、op 語義（codec 在
 * transport_proxy_client.h；語義在 Python 代理）。
 * Windows-only（CreateProcess＋匿名管道）；其他平台 fail-closed。
 */
#ifndef GPTBRIDGE_SIDECAR_TRANSPORT_H
#define GPTBRIDGE_SIDECAR_TRANSPORT_H

#include <string>

#include "transport_proxy_client.h"

namespace gptbridge {
namespace tpx {

struct SidecarError {
    /* code: PROXY_DISCONNECTED / PROXY_SPAWN_FAILED / PROXY_TIMEOUT */
    std::string code;
    std::string message;
};

class ProxySidecar {
public:
    ProxySidecar() = default;
    ~ProxySidecar();
    ProxySidecar(const ProxySidecar&) = delete;
    ProxySidecar& operator=(const ProxySidecar&) = delete;

    /* 啟動代理子行程。command_line 為完整命令列
       （例："python -m governance_rule.execution.tool_runtime.transport_proxy"）。
       失敗回 false 並填 err。可重複呼叫（先關舊行程）。 */
    bool start(const std::string& command_line, SidecarError* err);
    bool running() const;
    void stop();

    /* 同步呼叫：寫一條請求、讀回應直到 id 對帳命中或連線中斷。
       成功回 true 並填 *out（ok=false 的協定錯誤仍算「呼叫成功」，
       由呼叫方讀 error_code）；傳輸層失敗回 false 並填 err。 */
    bool call(const std::string& op, const std::string& args_json,
              ProxyResponse* out, SidecarError* err);

    /* 子行程 stderr 以繼承方式輸出（呼叫方可見代理日誌）。 */

private:
    struct Impl;
    Impl* impl_ = nullptr;
    unsigned long long next_id_ = 1;
};

} // namespace tpx
} // namespace gptbridge

#endif /* GPTBRIDGE_SIDECAR_TRANSPORT_H */

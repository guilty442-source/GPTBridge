/* transport_proxy_client.h — star-governed-transport-proxy/v1 客戶端編解碼
 * （純 C++17、零 I/O：stdio 讀寫由呼叫方注入；M1 模式 B 原生側）
 *
 * 職責：
 *  - encode：各 op 的請求行（`{"v":1,"id":...,"op":...,"args":{...}}`）
 *  - decode：回應行 → ProxyResponse（id/ok/result/error.code）
 *  - RequestWaiter：submit 側等待狀態機，逐行對齊 Python
 *    `GovernedRequestClient.request_sync`（completed/cancelled/deadline）
 *
 * 不在本層：token、路由政策、傳輸庫（全在代理/Python 治理面）。
 */
#ifndef GPTBRIDGE_TRANSPORT_PROXY_CLIENT_H
#define GPTBRIDGE_TRANSPORT_PROXY_CLIENT_H

#include <string>
#include <utility>
#include <vector>

#include "jsonlite.h"

namespace gptbridge {
namespace tpx {

struct ProxyResponse {
    bool valid = false; /* 行被丟棄（非本協定回應）時 false */
    std::string id;
    bool ok = false;
    jsonlite::JsonValue result;     /* ok==true 時有效 */
    std::string error_code;         /* ok==false 時有效 */
    std::string error_message;
};

/* 解碼一行回應。回 false = 依協定丟棄（壞 JSON／非 dict／v 不符／缺 id）。 */
bool decode_response_line(const std::string& line, ProxyResponse* out);

/* 編碼一條請求行（不含換行；呼叫方負責 +'\n' 與寫入）。 */
std::string encode_request(const std::string& id, const std::string& op,
                           const std::string& args_json);

/* ---- args builders（回傳 args JSON 物件字串） ---- */

struct HelloChannel {
    std::string channel; /* "system" | "ai" */
    std::string mode;    /* "process" | "submit" */
};
struct HelloSubmit {
    std::string channel;    /* submit 綁定的通道 id */
    std::string actor;      /* "governance/tool/<tool>" */
    std::string authorizer; /* "module:function" */
};

std::string args_hello(const std::string& tool_id,
                       const std::string& workspace_instance_id,
                       const std::vector<HelloChannel>& channels,
                       const std::vector<HelloSubmit>& submit);
std::string args_empty();
std::string args_channel(const std::string& channel);
std::string args_respond(const std::string& channel,
                         const std::string& request_id,
                         const std::string& response_json);
std::string args_request_id(const std::string& channel,
                            const std::string& request_id);
std::string args_progress(const std::string& channel,
                          const std::string& request_id,
                          const std::string& payload_json);
std::string args_ack(const std::string& channel,
                     const std::string& push_id,
                     const std::string& response_json /* "" = 省略 */);
std::string args_submit_request(const std::string& channel,
                                const std::string& target_tool_id,
                                const std::string& command,
                                const std::string& payload_json,
                                const std::string& request_id /* "" = 省略 */);
std::string args_submit_response(const std::string& channel,
                                 const std::string& target_tool_id,
                                 const std::string& request_id);
std::string args_push(const std::string& channel,
                      const std::string& target_tool_id,
                      const std::string& command,
                      const std::string& payload_json,
                      const std::string& push_id /* "" = 省略 */);

/* ---- submit 側等待狀態機（request_sync parity） ----
 *
 * 用法：request 後每輪以 response() 的 result 餵 feed()：
 *   result 為 null（無更新）→ Pending
 *   status=="completed" 且 response 為 object → Completed（result() 為
 *     已去 request_id 的 response dict；呼叫方附 queued:false/transport）
 *   status=="cancelled" → Cancelled（GOVERNED_REQUEST_CANCELLED）
 *   status=="completed" 但 response 非 dict → Failed(PERMISSION_DENIED)
 * deadline_exceeded(now) 為真時：呼叫方發 cancel 並報
 *   GOVERNED_REQUEST_TIMEOUT（Python 同款）。
 */
class RequestWaiter {
public:
    enum class State { Pending, Completed, Cancelled, Failed };

    explicit RequestWaiter(double deadline_at) : deadline_at_(deadline_at) {}

    State feed(const jsonlite::JsonValue* state /* nullptr = null */);
    bool deadline_exceeded(double now) const { return now >= deadline_at_; }
    State state() const { return state_; }
    const jsonlite::JsonValue& result() const { return result_; }
    const std::string& error_code() const { return error_code_; }

private:
    double deadline_at_;
    State state_ = State::Pending;
    jsonlite::JsonValue result_;
    std::string error_code_;
};

} // namespace tpx
} // namespace gptbridge

#endif /* GPTBRIDGE_TRANSPORT_PROXY_CLIENT_H */

/* http_gate.h — ABI §3 HTTP 閘門線層（M1 模式 B 工具宿主）。
 *
 * 對齊 `GovernedRuntimeMaintenanceMixin.run` 的 process_request：
 *   /health   → 200 application/json（body 由呼叫方注入）
 *   /metrics  → 200 application/json（body 由呼叫方注入）
 *   /shutdown → X-GPTBridge-Shutdown-Token compare_digest → 403/200
 *   其他      → WS 閘門：?token=<lower>&instance=<wsid>，compare_digest
 *               ＋instance 相等 → 放行（WsUpgrade）否則 403
 *
 * 本層只做「位元組→決策」；token／instance／body 由呼叫方提供。
 * 閘門判定內部重用 governed_tool.c（gt_*）的純 C 語義——不重實作。
 * 零 I/O、零狀態；回應序列化也在此層。
 */
#ifndef GPTBRIDGE_HTTP_GATE_H
#define GPTBRIDGE_HTTP_GATE_H

#include <string>
#include <utility>
#include <vector>

namespace gptbridge {
namespace gate {

struct HttpRequest {
    std::string method;
    std::string path;   /* 不含 query */
    std::string query;  /* '?' 之後原始串 */
    std::vector<std::pair<std::string, std::string>> headers; /* 小寫名 */

    const char* header(const char* name) const;
};

/* 解析 HTTP/1.1 請求頭（request line + headers；不含 body）。
   回 true 並填 *out / *consumed（含結尾 CRLF CRLF）。
   上限：request line ≤ 8 KiB、header ≤ 64 條、每 header line ≤ 8 KiB；
   超限或格式不符 → false（fail-closed）。 */
bool http_parse_request(const std::string& bytes, HttpRequest* out,
                        size_t* consumed);

enum class GateAction {
    RespondHealth,    /* /health → 200 JSON */
    RespondMetrics,   /* /metrics → 200 JSON */
    RespondShutdown,  /* /shutdown 通過 → 200 text/plain（呼叫方再關機） */
    Reject,           /* 403 text/plain "Forbidden" */
    WsUpgrade,        /* 通過 WS 閘門 → 進 WS 握手 */
};

struct GateDecision {
    GateAction action = GateAction::Reject;
};

/* process_request 等值判定。
   shutdown_token / ws_token / instance_id 為權威值（呼叫方持有）；
   supplied 值一律自 request 抽取（header／query）。token 比對走
   compare_digest 語義；WS token 先 lower（Python 同款）。 */
GateDecision gate_decide(const HttpRequest& req,
                         const std::string& shutdown_token,
                         const std::string& ws_token,
                         const std::string& instance_id);

/* 閘門回應序列化（Connection: close）。health/metrics body 由呼叫方
   提供；shutdown 通過與 reject 的 body 固定為 Python 同款字串。 */
std::string http_response(int status, const char* reason,
                          const std::string& body,
                          const char* content_type);

/* query 解析：`a=b&c=d` → 第一個值；百分號解碼 + '+'→空格
   （Python parse_qs 等值之子集：單一 '?' 區段、無分號分隔）。 */
std::string query_first(const std::string& query, const char* key);

} // namespace gate
} // namespace gptbridge

#endif /* GPTBRIDGE_HTTP_GATE_H */

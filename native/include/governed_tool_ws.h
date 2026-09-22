/* governed_tool_ws.h — governed tool runtime HTTP/WS 閘門編解碼
 * （純 C++17、零 I/O；star-governed-tool-runtime-abi/v1 §3 語義）
 *
 * 對齊 Python `governed_runtime_maintenance.py::process_request`：
 *   /health → 200 JSON；/metrics → 200 JSON；
 *   /shutdown → X-GPTBridge-Shutdown-Token 頭經 compare_digest 比對
 *     （shutdown_token 空 → 一律 403）；
 *   其他（WS upgrade）→ query token（lowercase）compare_digest
 *     ＋ instance == workspace_instance_id → upgrade，否則 403。
 *
 * 另含 WS 線框編解碼（server 視角：入站 frame 必須 masked）與
 * RFC 6455 accept key（SHA-1＋base64，自研無第三方）。
 */
#ifndef GPTBRIDGE_GOVERNED_TOOL_WS_H
#define GPTBRIDGE_GOVERNED_TOOL_WS_H

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace gptbridge {
namespace gtw {

/* ---- compare_digest（hmac.compare_digest 語義） ----
 * 長度不同 → 立即 false（同 CPython：型別/長度不同直接 False）。
 * 長度相同 → 常數時間逐位元 XOR。 */
bool compare_digest(const std::string& a, const std::string& b);

/* ---- HTTP request 解析（fail-closed） ---- */
struct HttpRequest {
    std::string method;
    std::string target;                 /* 原始 request-target */
    std::string path;                   /* query 之前 */
    std::vector<std::pair<std::string, std::string>> query; /* 已 url-decode */
    /* headers: (lowercase-name, value-trimmed)，保序 */
    std::vector<std::pair<std::string, std::string>> headers;

    const char* header(const std::string& lower_name) const;
};

/* 解析至第一個 CRLFCRLF；成功回 true 且 consumed=含結尾的位元組數。
 * 失敗一律 false（呼叫方應拒絕連線）。上限 16 KiB 頭部。 */
bool http_request_parse(const uint8_t* buf, size_t size,
                        HttpRequest* out, size_t* consumed);

/* ---- 閘門決策（process_request parity） ---- */
enum class GateDecision {
    Health,      /* 200 application/json 健康快照 */
    Metrics,     /* 200 application/json 通道計量 */
    Shutdown,    /* 200 text/plain + 觸發關閉 */
    Upgrade,     /* WS upgrade 放行（process_request 回 None） */
    Forbidden    /* 403 text/plain "Forbidden" */
};

/* expected_token = runtime token（已 lowercase 的 64-hex）；
 * expected_instance = workspace_instance_id()；
 * shutdown_token 空字串 → /shutdown 一律 Forbidden（Python 同款）。 */
GateDecision route_request(const HttpRequest& req,
                           const std::string& expected_token,
                           const std::string& expected_instance,
                           const std::string& shutdown_token);

/* 403 回應的 HTTP 位元組（固定 body "Forbidden"）。 */
std::string http_forbidden_bytes();
std::string http_ok_bytes(const std::string& body,
                          const std::string& content_type);

/* ---- RFC 6455 ---- */
/* Sec-WebSocket-Accept = base64(sha1(key + GUID))。 */
std::string ws_accept_key(const std::string& sec_websocket_key);
/* upgrade 放行回應（101 Switching Protocols）。 */
std::string ws_upgrade_response(const std::string& sec_websocket_key);

/* 握手驗證（websockets.serve 伺服端 parity）：
 *   Upgrade: websocket（不分大小寫）
 *   Connection 含 upgrade token
 *   Sec-WebSocket-Version: 13
 *   Sec-WebSocket-Key 為合法 base64 且解出 16 bytes
 *   Origin 缺省／"file://"／"null"（origins=(None,"file://","null")）
 * 通過回 true 且 *accept_key 填 Sec-WebSocket-Accept。 */
bool ws_validate_upgrade(const HttpRequest& req, std::string* accept_key);

enum class WsOp : uint8_t {
    Continuation = 0x0, Text = 0x1, Binary = 0x2,
    Close = 0x8, Ping = 0x9, Pong = 0xA
};

struct WsFrame {
    bool fin = false;
    WsOp opcode = WsOp::Continuation;
    std::string payload;   /* unmasked 後 */
};

/* 從 buf 解一個 server 視角入站 frame（client→server 必須 MASK=1）。
 * 回 0=需更多資料；>0=消耗的位元組數（frame 寫入 out）；
 * 回 -1=協定錯（未 mask／保留 opcode／非最小長度編碼／
 * 長度逾 1 MiB（websockets 預設 max_size）／控制帧碎裂或 >125）——
 * 呼叫方應關閉連線。 */
int64_t ws_frame_decode(const uint8_t* buf, size_t size, WsFrame* out);

/* server→client 出站 frame（不 mask）。 */
std::string ws_frame_encode(bool fin, WsOp opcode, const std::string& payload);

/* 便捷封包：pong 回聲 ping payload；close 帶 2-byte code＋reason。 */
std::string ws_pong(const std::string& ping_payload);
std::string ws_close(uint16_t code, const std::string& reason);

} // namespace gtw
} // namespace gptbridge

#endif /* GPTBRIDGE_GOVERNED_TOOL_WS_H */

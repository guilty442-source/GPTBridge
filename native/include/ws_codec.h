/* ws_codec.h — RFC6455 WebSocket 線層（M1 模式 B 工具宿主）。
 *
 * 對齊 `websockets.serve` 伺服端語義（governed runtime 同源）：
 *  - 握手驗證：Upgrade/Connection/Version:13/Key（base64→16 bytes）
 *    ＋Origin 允列（缺 Origin、"file://"、"null"）
 *  - Accept = base64(SHA1(key + RFC6455 GUID))
 *  - 客戶端訊框必須帶 mask；控制訊框 FIN=1 且 ≤125 bytes；
 *    payload 上限 1 MiB（websockets 預設 max_size）
 *  - 伺服端送出訊框不帶 mask
 *
 * 零 I/O：輸入位元組串→訊框／回應字串。
 */
#ifndef GPTBRIDGE_WS_CODEC_H
#define GPTBRIDGE_WS_CODEC_H

#include <cstdint>
#include <string>

#include "http_gate.h"

namespace gptbridge {
namespace ws {

enum class Op : uint8_t {
    Continuation = 0x0,
    Text = 0x1,
    Binary = 0x2,
    Close = 0x8,
    Ping = 0x9,
    Pong = 0xA,
};

constexpr size_t kMaxPayload = 1 << 20; /* websockets 預設 max_size */

struct Frame {
    bool fin = true;
    Op opcode = Op::Text;
    std::string payload; /* 已去 mask */
};

/* 握手驗證：回 true 時 *accept_key 填 Sec-WebSocket-Accept 值。 */
bool ws_validate_upgrade(const gate::HttpRequest& req,
                         std::string* accept_key);

/* 101 握手回應序列化。 */
std::string ws_handshake_response(const std::string& accept_key);

/* 解析一個客戶端訊框。bytes 不足 → 回 false 且 *consumed=0（呼叫方
   保留緩衝續讀）；格式違規 → *consumed=0 且 *protocol_error=true。
   成功 → *consumed=已耗位元組。 */
bool ws_frame_decode(const std::string& bytes, Frame* out,
                     size_t* consumed, bool* protocol_error);

/* 伺服端訊框序列化（不帶 mask）。payload > kMaxPayload → 空串。 */
std::string ws_frame_encode(Op opcode, const std::string& payload,
                            bool fin = true);

std::string ws_pong(const std::string& ping_payload);
std::string ws_close(uint16_t code, const std::string& reason);

/* 對外暴露 SHA-1＋base64（accept 運算可獨立測試）。 */
std::string ws_accept_key(const std::string& client_key);

} // namespace ws
} // namespace gptbridge

#endif /* GPTBRIDGE_WS_CODEC_H */

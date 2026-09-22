/* ws_codec.cpp — RFC6455 線層實作（見 ws_codec.h）。
 * 內含最小 SHA-1 與 base64（accept 運算唯一需求）。
 */
#include "ws_codec.h"

#include <cctype>
#include <cstring>
#include <string>
#include <vector>

namespace gptbridge {
namespace ws {

namespace {

/* ── SHA-1（FIPS-180-1）────────────────────────────────────────── */

struct Sha1 {
    uint32_t h[5] = {0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476,
                     0xC3D2E1F0};
    uint64_t len = 0;
    uint8_t buf[64]{};
    size_t buflen = 0;

    void block(const uint8_t* p) {
        uint32_t w[80];
        for (int i = 0; i < 16; ++i) {
            w[i] = (uint32_t(p[i * 4]) << 24) |
                   (uint32_t(p[i * 4 + 1]) << 16) |
                   (uint32_t(p[i * 4 + 2]) << 8) | uint32_t(p[i * 4 + 3]);
        }
        for (int i = 16; i < 80; ++i) {
            const uint32_t x = w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16];
            w[i] = (x << 1) | (x >> 31);
        }
        uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
        for (int i = 0; i < 80; ++i) {
            uint32_t f, k;
            if (i < 20) {
                f = (b & c) | (~b & d);
                k = 0x5A827999;
            } else if (i < 40) {
                f = b ^ c ^ d;
                k = 0x6ED9EBA1;
            } else if (i < 60) {
                f = (b & c) | (b & d) | (c & d);
                k = 0x8F1BBCDC;
            } else {
                f = b ^ c ^ d;
                k = 0xCA62C1D6;
            }
            const uint32_t t =
                ((a << 5) | (a >> 27)) + f + e + k + w[i];
            e = d;
            d = c;
            c = (b << 30) | (b >> 2);
            b = a;
            a = t;
        }
        h[0] += a;
        h[1] += b;
        h[2] += c;
        h[3] += d;
        h[4] += e;
    }

    void update(const uint8_t* p, size_t n) {
        len += n;
        while (n) {
            const size_t take =
                buflen + n > 64 ? 64 - buflen : n;
            std::memcpy(buf + buflen, p, take);
            buflen += take;
            p += take;
            n -= take;
            if (buflen == 64) {
                block(buf);
                buflen = 0;
            }
        }
    }

    void final(uint8_t out[20]) {
        const uint64_t bits = len * 8;
        uint8_t pad = 0x80;
        update(&pad, 1);
        uint8_t zero = 0;
        while (buflen != 56) update(&zero, 1);
        uint8_t lenbuf[8];
        for (int i = 0; i < 8; ++i)
            lenbuf[i] = static_cast<uint8_t>(bits >> (56 - i * 8));
        update(lenbuf, 8);
        for (int i = 0; i < 5; ++i) {
            out[i * 4] = static_cast<uint8_t>(h[i] >> 24);
            out[i * 4 + 1] = static_cast<uint8_t>(h[i] >> 16);
            out[i * 4 + 2] = static_cast<uint8_t>(h[i] >> 8);
            out[i * 4 + 3] = static_cast<uint8_t>(h[i]);
        }
    }
};

std::string sha1(const std::string& s) {
    Sha1 ctx;
    ctx.update(reinterpret_cast<const uint8_t*>(s.data()), s.size());
    uint8_t out[20];
    ctx.final(out);
    return std::string(reinterpret_cast<const char*>(out), 20);
}

/* ── base64 ────────────────────────────────────────────────────── */

std::string b64(const std::string& raw) {
    static const char* T =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    for (size_t i = 0; i < raw.size(); i += 3) {
        const uint32_t a = static_cast<uint8_t>(raw[i]);
        const uint32_t b =
            i + 1 < raw.size() ? static_cast<uint8_t>(raw[i + 1]) : 0;
        const uint32_t c =
            i + 2 < raw.size() ? static_cast<uint8_t>(raw[i + 2]) : 0;
        const uint32_t v = (a << 16) | (b << 8) | c;
        out += T[(v >> 18) & 63];
        out += T[(v >> 12) & 63];
        out += i + 1 < raw.size() ? T[(v >> 6) & 63] : '=';
        out += i + 2 < raw.size() ? T[v & 63] : '=';
    }
    return out;
}

int b64val(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

/* 嚴格 base64 解碼（長度需 4 倍數、只允結尾 '='）；失敗回空串。 */
std::string b64decode(const std::string& s) {
    if (s.empty() || s.size() % 4) return "";
    std::string out;
    for (size_t i = 0; i < s.size(); i += 4) {
        int v[4];
        for (int j = 0; j < 4; ++j) {
            const char c = s[i + j];
            if (c == '=') {
                if (i + 4 != s.size() || j < 2) return "";
                v[j] = -2;
            } else {
                v[j] = b64val(c);
                if (v[j] < 0) return "";
            }
        }
        const uint32_t n = (uint32_t(v[0]) << 18) |
                           (uint32_t(v[1] < 0 ? 0 : v[1]) << 12) |
                           (uint32_t(v[2] < 0 ? 0 : v[2]) << 6) |
                           uint32_t(v[3] < 0 ? 0 : v[3]);
        out += static_cast<char>((n >> 16) & 0xFF);
        if (v[2] >= 0) out += static_cast<char>((n >> 8) & 0xFF);
        if (v[3] >= 0) out += static_cast<char>(n & 0xFF);
    }
    return out;
}

/* Connection header 以逗號分隔 token，含 "upgrade"（不分大小寫）。 */
bool has_token(const char* header_value, const char* token) {
    if (header_value == nullptr) return false;
    std::string s = header_value;
    for (char& c : s)
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    size_t pos = 0;
    while (pos <= s.size()) {
        const auto comma = s.find(',', pos);
        const auto end = comma == std::string::npos ? s.size() : comma;
        std::string item = s.substr(pos, end - pos);
        const auto a = item.find_first_not_of(" \t");
        const auto b = item.find_last_not_of(" \t");
        if (a != std::string::npos &&
            item.substr(a, b - a + 1) == token) {
            return true;
        }
        if (comma == std::string::npos) break;
        pos = comma + 1;
    }
    return false;
}

std::string lower(std::string s) {
    for (char& c : s)
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

} // namespace

std::string ws_accept_key(const std::string& client_key) {
    static const char* GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    return b64(sha1(client_key + GUID));
}

bool ws_validate_upgrade(const gate::HttpRequest& req,
                         std::string* accept_key) {
    const char* upgrade = req.header("upgrade");
    if (upgrade == nullptr || lower(upgrade) != "websocket") return false;
    if (!has_token(req.header("connection"), "upgrade")) return false;
    const char* version = req.header("sec-websocket-version");
    if (version == nullptr || std::string(version) != "13") return false;
    const char* key = req.header("sec-websocket-key");
    if (key == nullptr || b64decode(key).size() != 16) return false;
    const char* origin = req.header("origin");
    if (origin != nullptr && std::string(origin) != "file://" &&
        std::string(origin) != "null") {
        return false;
    }
    if (accept_key) *accept_key = ws_accept_key(key);
    return true;
}

std::string ws_handshake_response(const std::string& accept_key) {
    return "HTTP/1.1 101 Switching Protocols\r\n"
           "Upgrade: websocket\r\n"
           "Connection: Upgrade\r\n"
           "Sec-WebSocket-Accept: " +
           accept_key + "\r\n\r\n";
}

bool ws_frame_decode(const std::string& bytes, Frame* out,
                     size_t* consumed, bool* protocol_error) {
    *consumed = 0;
    *protocol_error = false;
    if (bytes.size() < 2) return false;
    const uint8_t b0 = static_cast<uint8_t>(bytes[0]);
    const uint8_t b1 = static_cast<uint8_t>(bytes[1]);
    const bool fin = (b0 & 0x80) != 0;
    const uint8_t rsv = b0 & 0x70;
    const uint8_t opc = b0 & 0x0F;
    const bool masked = (b1 & 0x80) != 0;
    uint64_t len = b1 & 0x7F;
    size_t pos = 2;

    if (rsv != 0 || opc == 0x3 || opc == 0x4 || opc == 0x5 ||
        opc == 0x6 || opc == 0x7 || opc > 0xA) {
        *protocol_error = true; /* RSV/未知 opcode */
        return false;
    }
    const bool control = opc >= 0x8;
    if (control && (!fin || len > 125)) {
        *protocol_error = true;
        return false;
    }
    if (!masked) {
        *protocol_error = true; /* 伺服端要求客戶端訊框帶 mask */
        return false;
    }
    if (len == 126) {
        if (bytes.size() < pos + 2) return false;
        len = (uint64_t(static_cast<uint8_t>(bytes[pos])) << 8) |
              uint64_t(static_cast<uint8_t>(bytes[pos + 1]));
        pos += 2;
        if (len < 126) {
            *protocol_error = true;
            return false;
        }
    } else if (len == 127) {
        if (bytes.size() < pos + 8) return false;
        len = 0;
        for (int i = 0; i < 8; ++i) {
            len = (len << 8) |
                  uint64_t(static_cast<uint8_t>(bytes[pos + i]));
        }
        pos += 8;
        if (len < 0x10000 || (len >> 63) != 0) {
            *protocol_error = true;
            return false;
        }
    }
    if (control && len > 125) {
        *protocol_error = true;
        return false;
    }
    if (len > kMaxPayload) {
        *protocol_error = true;
        return false;
    }
    if (bytes.size() < pos + 4 + len) return false;
    const uint8_t* mask =
        reinterpret_cast<const uint8_t*>(bytes.data() + pos);
    pos += 4;
    out->fin = fin;
    out->opcode = static_cast<Op>(opc);
    out->payload.resize(static_cast<size_t>(len));
    for (size_t i = 0; i < len; ++i) {
        out->payload[i] = static_cast<char>(
            static_cast<uint8_t>(bytes[pos + i]) ^ mask[i % 4]);
    }
    *consumed = pos + static_cast<size_t>(len);
    return true;
}

std::string ws_frame_encode(Op opcode, const std::string& payload,
                            bool fin) {
    if (payload.size() > kMaxPayload) return "";
    std::string out;
    out += static_cast<char>((fin ? 0x80 : 0) |
                             static_cast<uint8_t>(opcode));
    const size_t n = payload.size();
    if (n < 126) {
        out += static_cast<char>(n);
    } else if (n <= 0xFFFF) {
        out += static_cast<char>(126);
        out += static_cast<char>((n >> 8) & 0xFF);
        out += static_cast<char>(n & 0xFF);
    } else {
        out += static_cast<char>(127);
        for (int i = 7; i >= 0; --i) {
            out += static_cast<char>((n >> (i * 8)) & 0xFF);
        }
    }
    out += payload;
    return out;
}

std::string ws_pong(const std::string& ping_payload) {
    return ws_frame_encode(Op::Pong, ping_payload);
}

std::string ws_close(uint16_t code, const std::string& reason) {
    std::string payload;
    payload += static_cast<char>((code >> 8) & 0xFF);
    payload += static_cast<char>(code & 0xFF);
    payload += reason;
    return ws_frame_encode(Op::Close, payload);
}

} // namespace ws
} // namespace gptbridge

/* governed_tool_ws.cpp — HTTP/WS 閘門編解碼實作（零 I/O）。
 * parity 目標：governed_runtime_maintenance.py::process_request。 */
#include "governed_tool_ws.h"

#include <cctype>
#include <cstring>

namespace gptbridge {
namespace gtw {

namespace {

const size_t MAX_HEADER_BYTES = 16 * 1024;
const size_t MAX_WS_PAYLOAD = 2 * 1024 * 1024;

/* ---- url percent-decode（query 用；'+'→' ' 同 parse_qs） ---- */
std::string url_decode(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (size_t i = 0; i < s.size(); ++i) {
        char c = s[i];
        if (c == '%' && i + 2 < s.size() &&
            std::isxdigit(static_cast<unsigned char>(s[i + 1])) &&
            std::isxdigit(static_cast<unsigned char>(s[i + 2]))) {
            auto hex = [](char h) -> int {
                if (h >= '0' && h <= '9') return h - '0';
                if (h >= 'a' && h <= 'f') return h - 'a' + 10;
                return h - 'A' + 10;
            };
            out += static_cast<char>((hex(s[i + 1]) << 4) | hex(s[i + 2]));
            i += 2;
        } else if (c == '+') {
            out += ' ';
        } else {
            out += c;
        }
    }
    return out;
}

std::string lower(const std::string& s) {
    std::string out = s;
    for (auto& c : out) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return out;
}

std::string trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t')) ++a;
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t')) --b;
    return s.substr(a, b - a);
}

/* ---- SHA-1（FIPS 180-1）---- */
struct Sha1 {
    uint32_t h[5] = {0x67452301u, 0xEFCDAB89u, 0x98BADCFEu,
                     0x10325476u, 0xC3D2E1F0u};
    uint64_t len = 0;
    uint8_t block[64];
    size_t used = 0;

    static uint32_t rol(uint32_t v, int n) { return (v << n) | (v >> (32 - n)); }

    void chunk(const uint8_t* p) {
        uint32_t w[80];
        for (int i = 0; i < 16; ++i)
            w[i] = (uint32_t(p[i * 4]) << 24) | (uint32_t(p[i * 4 + 1]) << 16) |
                   (uint32_t(p[i * 4 + 2]) << 8) | uint32_t(p[i * 4 + 3]);
        for (int i = 16; i < 80; ++i)
            w[i] = rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
        uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
        for (int i = 0; i < 80; ++i) {
            uint32_t f, k;
            if (i < 20) { f = (b & c) | (~b & d); k = 0x5A827999u; }
            else if (i < 40) { f = b ^ c ^ d; k = 0x6ED9EBA1u; }
            else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8F1BBCDCu; }
            else { f = b ^ c ^ d; k = 0xCA62C1D6u; }
            uint32_t t = rol(a, 5) + f + e + k + w[i];
            e = d; d = c; c = rol(b, 30); b = a; a = t;
        }
        h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e;
    }

    void update(const uint8_t* data, size_t n) {
        len += n;
        while (n) {
            size_t take = 64 - used;
            if (take > n) take = n;
            std::memcpy(block + used, data, take);
            used += take; data += take; n -= take;
            if (used == 64) { chunk(block); used = 0; }
        }
    }

    void finish(uint8_t out[20]) {
        uint64_t bits = len * 8;
        uint8_t pad = 0x80;
        update(&pad, 1);
        uint8_t zero = 0;
        while (used != 56) update(&zero, 1);
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

std::string base64_encode(const uint8_t* data, size_t n) {
    static const char T[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    for (size_t i = 0; i < n; i += 3) {
        uint32_t v = uint32_t(data[i]) << 16;
        if (i + 1 < n) v |= uint32_t(data[i + 1]) << 8;
        if (i + 2 < n) v |= uint32_t(data[i + 2]);
        out += T[(v >> 18) & 63];
        out += T[(v >> 12) & 63];
        out += (i + 1 < n) ? T[(v >> 6) & 63] : '=';
        out += (i + 2 < n) ? T[v & 63] : '=';
    }
    return out;
}

} // namespace

bool compare_digest(const std::string& a, const std::string& b) {
    if (a.size() != b.size()) return false;
    uint8_t diff = 0;
    for (size_t i = 0; i < a.size(); ++i)
        diff |= static_cast<uint8_t>(a[i] ^ b[i]);
    return diff == 0;
}

const char* HttpRequest::header(const std::string& lower_name) const {
    for (const auto& h : headers)
        if (h.first == lower_name) return h.second.c_str();
    return nullptr;
}

bool http_request_parse(const uint8_t* buf, size_t size,
                        HttpRequest* out, size_t* consumed) {
    if (size > MAX_HEADER_BYTES) return false;
    const std::string text(reinterpret_cast<const char*>(buf), size);
    const size_t end = text.find("\r\n\r\n");
    if (end == std::string::npos) return false;
    *consumed = end + 4;
    *out = HttpRequest{};

    const size_t rl_end = text.find("\r\n");
    if (rl_end == std::string::npos || rl_end > end) return false;
    const std::string reqline = text.substr(0, rl_end);
    const size_t sp1 = reqline.find(' ');
    const size_t sp2 = reqline.rfind(' ');
    if (sp1 == std::string::npos || sp2 == std::string::npos || sp1 >= sp2) {
        return false;
    }
    out->method = reqline.substr(0, sp1);
    out->target = reqline.substr(sp1 + 1, sp2 - sp1 - 1);

    const size_t qm = out->target.find('?');
    out->path = (qm == std::string::npos) ? out->target : out->target.substr(0, qm);
    if (qm != std::string::npos) {
        const std::string qs = out->target.substr(qm + 1);
        size_t pos = 0;
        while (pos <= qs.size()) {
            const size_t amp = qs.find('&', pos);
            const std::string pair = qs.substr(
                pos, amp == std::string::npos ? amp : amp - pos);
            if (!pair.empty()) {
                const size_t eq = pair.find('=');
                if (eq == std::string::npos) {
                    out->query.emplace_back(url_decode(pair), "");
                } else {
                    out->query.emplace_back(url_decode(pair.substr(0, eq)),
                                            url_decode(pair.substr(eq + 1)));
                }
            }
            if (amp == std::string::npos) break;
            pos = amp + 1;
        }
    }

    size_t pos = rl_end + 2;
    while (pos < end) {
        const size_t ln = text.find("\r\n", pos);
        if (ln == std::string::npos || ln > end) return false;
        const std::string line = text.substr(pos, ln - pos);
        pos = ln + 2;
        if (line.empty()) continue;
        const size_t colon = line.find(':');
        if (colon == std::string::npos || colon == 0) return false;
        out->headers.emplace_back(lower(line.substr(0, colon)),
                                  trim(line.substr(colon + 1)));
    }
    return !out->method.empty() && !out->path.empty();
}

GateDecision route_request(const HttpRequest& req,
                           const std::string& expected_token,
                           const std::string& expected_instance,
                           const std::string& shutdown_token) {
    if (req.path == "/health") return GateDecision::Health;
    if (req.path == "/metrics") return GateDecision::Metrics;
    if (req.path == "/shutdown") {
        const char* supplied = req.header("x-gptbridge-shutdown-token");
        if (shutdown_token.empty() || supplied == nullptr ||
            !compare_digest(std::string(supplied), shutdown_token)) {
            return GateDecision::Forbidden;
        }
        return GateDecision::Shutdown;
    }
    /* parse_qs parity: blank-valued params are dropped entirely
     * (keep_blank_values=False); first surviving occurrence wins. */
    std::string token;
    std::string instance;
    bool token_seen = false;
    bool instance_seen = false;
    for (const auto& q : req.query) {
        if (q.second.empty()) continue;
        if (q.first == "token" && !token_seen) {
            token = lower(q.second);
            token_seen = true;
        }
        if (q.first == "instance" && !instance_seen) {
            instance = q.second;
            instance_seen = true;
        }
    }
    if (!compare_digest(token, expected_token) ||
        instance != expected_instance) {
        return GateDecision::Forbidden;
    }
    return GateDecision::Upgrade;
}

std::string http_forbidden_bytes() {
    static const char B[] =
        "HTTP/1.1 403 FORBIDDEN\r\n"
        "Content-Type: text/plain\r\n"
        "Content-Length: 9\r\n"
        "\r\n"
        "Forbidden";
    return std::string(B, sizeof(B) - 1);
}

std::string http_ok_bytes(const std::string& body,
                          const std::string& content_type) {
    std::string out = "HTTP/1.1 200 OK\r\nContent-Type: ";
    out += content_type;
    out += "\r\nContent-Length: " + std::to_string(body.size());
    out += "\r\n\r\n";
    out += body;
    return out;
}

std::string ws_accept_key(const std::string& sec_websocket_key) {
    static const char GUID[] = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    Sha1 sha;
    sha.update(reinterpret_cast<const uint8_t*>(sec_websocket_key.data()),
               sec_websocket_key.size());
    sha.update(reinterpret_cast<const uint8_t*>(GUID), sizeof(GUID) - 1);
    uint8_t digest[20];
    sha.finish(digest);
    return base64_encode(digest, sizeof(digest));
}

std::string ws_upgrade_response(const std::string& sec_websocket_key) {
    std::string out =
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Accept: ";
    out += ws_accept_key(sec_websocket_key);
    out += "\r\n\r\n";
    return out;
}

int64_t ws_frame_decode(const uint8_t* buf, size_t size, WsFrame* out) {
    if (size < 2) return 0;
    const bool fin = (buf[0] & 0x80) != 0;
    const uint8_t opcode = buf[0] & 0x0F;
    const bool masked = (buf[1] & 0x80) != 0;
    uint64_t len = buf[1] & 0x7F;
    size_t pos = 2;

    /* rsv bits must be zero (no extensions negotiated) */
    if (buf[0] & 0x70) return -1;
    if (!masked) return -1;                     /* client->server must mask */
    switch (opcode) {
        case 0x0: case 0x1: case 0x2: case 0x8: case 0x9: case 0xA: break;
        default: return -1;                     /* reserved opcode */
    }
    if (opcode >= 0x8 && !fin) return -1;       /* control frames unfragmented */
    if (opcode >= 0x8 && len > 125) return -1;  /* control payload <=125 */

    if (len == 126) {
        if (size < pos + 2) return 0;
        len = (uint64_t(buf[pos]) << 8) | buf[pos + 1];
        pos += 2;
    } else if (len == 127) {
        if (size < pos + 8) return 0;
        len = 0;
        for (int i = 0; i < 8; ++i)
            len = (len << 8) | buf[pos + i];
        pos += 8;
        if (len & (1ull << 63)) return -1;      /* MSB must be 0 */
    }
    if (len > MAX_WS_PAYLOAD) return -1;
    if (size < pos + 4 + len) return 0;
    const uint8_t* mask = buf + pos;
    pos += 4;
    out->fin = fin;
    out->opcode = static_cast<WsOp>(opcode);
    out->payload.assign(len, '\0');
    for (uint64_t i = 0; i < len; ++i)
        out->payload[static_cast<size_t>(i)] =
            static_cast<char>(buf[pos + i] ^ mask[i % 4]);
    return static_cast<int64_t>(pos + len);
}

std::string ws_frame_encode(bool fin, WsOp opcode, const std::string& payload) {
    std::string out;
    out.push_back(static_cast<char>((fin ? 0x80 : 0x00) |
                                    static_cast<uint8_t>(opcode)));
    const size_t n = payload.size();
    if (n <= 125) {
        out.push_back(static_cast<char>(n));
    } else if (n <= 0xFFFF) {
        out.push_back(static_cast<char>(126));
        out.push_back(static_cast<char>((n >> 8) & 0xFF));
        out.push_back(static_cast<char>(n & 0xFF));
    } else {
        out.push_back(static_cast<char>(127));
        for (int i = 7; i >= 0; --i)
            out.push_back(static_cast<char>((uint64_t(n) >> (i * 8)) & 0xFF));
    }
    out += payload;
    return out;
}

} // namespace gtw
} // namespace gptbridge

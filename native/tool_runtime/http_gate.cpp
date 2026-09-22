/* http_gate.cpp — ABI §3 HTTP 閘門線層實作（見 http_gate.h）。
 * 判定重用 governed_tool.c 的 gt_* 純 C 語義。
 */
#include "http_gate.h"

#include <cctype>
#include <cstdio>
#include <string>

extern "C" {
#include "governed_tool.h"
}

namespace gptbridge {
namespace gate {

namespace {

constexpr size_t kMaxLine = 8 * 1024;
constexpr size_t kMaxHeaders = 64;

std::string lower(std::string s) {
    for (char& c : s) {
        c = static_cast<char>(
            std::tolower(static_cast<unsigned char>(c)));
    }
    return s;
}

std::string trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && std::isspace(static_cast<unsigned char>(s[a]))) ++a;
    while (b > a && std::isspace(static_cast<unsigned char>(s[b - 1])))
        --b;
    return s.substr(a, b - a);
}

bool parse_line(const std::string& bytes, size_t* pos, std::string* out) {
    const auto eol = bytes.find("\r\n", *pos);
    if (eol == std::string::npos || eol - *pos > kMaxLine) return false;
    *out = bytes.substr(*pos, eol - *pos);
    *pos = eol + 2;
    return true;
}

int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

} // namespace

const char* HttpRequest::header(const char* name) const {
    const std::string want = lower(name);
    for (const auto& kv : headers) {
        if (kv.first == want) return kv.second.c_str();
    }
    return nullptr;
}

bool http_parse_request(const std::string& bytes, HttpRequest* out,
                        size_t* consumed) {
    *out = HttpRequest{};
    size_t pos = 0;
    std::string line;
    if (!parse_line(bytes, &pos, &line) || line.empty()) return false;

    /* METHOD SP request-target SP HTTP-version */
    const auto sp1 = line.find(' ');
    if (sp1 == std::string::npos || sp1 == 0) return false;
    const auto sp2 = line.find(' ', sp1 + 1);
    if (sp2 == std::string::npos) return false;
    out->method = line.substr(0, sp1);
    const std::string target = line.substr(sp1 + 1, sp2 - sp1 - 1);
    const std::string version = line.substr(sp2 + 1);
    if (target.empty() || version.rfind("HTTP/1.", 0) != 0) return false;
    const auto qmark = target.find('?');
    out->path = qmark == std::string::npos ? target
                                           : target.substr(0, qmark);
    out->query = qmark == std::string::npos ? ""
                                            : target.substr(qmark + 1);

    for (;;) {
        if (!parse_line(bytes, &pos, &line)) return false;
        if (line.empty()) break; /* CRLF CRLF */
        if (out->headers.size() >= kMaxHeaders) return false;
        const auto colon = line.find(':');
        if (colon == std::string::npos || colon == 0) return false;
        for (size_t i = 0; i < colon; ++i) {
            const char c = line[i];
            if (c <= ' ' || c == ':' || c == 0x7f) return false;
        }
        out->headers.emplace_back(lower(line.substr(0, colon)),
                                  trim(line.substr(colon + 1)));
    }
    *consumed = pos;
    return true;
}

std::string query_first(const std::string& query, const char* key) {
    const std::string want = key;
    size_t pos = 0;
    while (pos <= query.size()) {
        const auto amp = query.find('&', pos);
        const auto end = amp == std::string::npos ? query.size() : amp;
        const std::string pair = query.substr(pos, end - pos);
        const auto eq = pair.find('=');
        const std::string k = pair.substr(0, eq);
        if (k == want) {
            const std::string raw =
                eq == std::string::npos ? "" : pair.substr(eq + 1);
            std::string out;
            out.reserve(raw.size());
            for (size_t i = 0; i < raw.size(); ++i) {
                const char c = raw[i];
                if (c == '+') {
                    out += ' ';
                } else if (
                    c == '%' && i + 2 < raw.size() &&
                    hexval(raw[i + 1]) >= 0 && hexval(raw[i + 2]) >= 0) {
                    out += static_cast<char>(
                        hexval(raw[i + 1]) * 16 + hexval(raw[i + 2]));
                    i += 2;
                } else {
                    out += c;
                }
            }
            return out;
        }
        if (amp == std::string::npos) break;
        pos = amp + 1;
    }
    return "";
}

GateDecision gate_decide(const HttpRequest& req,
                         const std::string& shutdown_token,
                         const std::string& ws_token,
                         const std::string& instance_id) {
    if (req.path == "/health") return {GateAction::RespondHealth};
    if (req.path == "/metrics") return {GateAction::RespondMetrics};
    if (req.path == "/shutdown") {
        const char* supplied = req.header("x-gptbridge-shutdown-token");
        if (gptbridge_gt_shutdown_gate(shutdown_token.c_str(), supplied)) {
            return {GateAction::RespondShutdown};
        }
        return {GateAction::Reject};
    }
    const std::string supplied_token = query_first(req.query, "token");
    const std::string supplied_inst = query_first(req.query, "instance");
    if (gptbridge_gt_ws_gate(ws_token.c_str(), supplied_token.c_str(),
                             instance_id.c_str(),
                             supplied_inst.c_str())) {
        return {GateAction::WsUpgrade};
    }
    return {GateAction::Reject};
}

std::string http_response(int status, const char* reason,
                          const std::string& body,
                          const char* content_type) {
    std::string out = "HTTP/1.1 ";
    out += std::to_string(status);
    out += ' ';
    out += reason;
    out += "\r\nContent-Type: ";
    out += content_type;
    out += "\r\nContent-Length: ";
    out += std::to_string(body.size());
    out += "\r\nConnection: close\r\n\r\n";
    out += body;
    return out;
}

} // namespace gate
} // namespace gptbridge

// Suite: M1 tool-host wire layer — HTTP gate (process_request parity)
// + RFC6455 codec (websockets.serve parity). Zero-I/O decision surface.
#include "harness.hpp"

#include <stdexcept>
#include <string>

#include "http_gate.h"
#include "ws_codec.h"

namespace {
const char* SUITE = "WS_HTTP_GATE_SUITE";
const char* TOKEN64 =
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

gate::HttpRequest parse(const std::string& raw, size_t* consumed) {
    gate::HttpRequest req;
    if (!gate::http_parse_request(raw, &req, consumed)) {
        throw std::runtime_error("parse failed");
    }
    return req;
}

std::string masked_frame(uint8_t b0, const std::string& payload,
                         const char* mask) {
    std::string out;
    out += static_cast<char>(b0);
    const size_t n = payload.size();
    if (n < 126) {
        out += static_cast<char>(0x80 | n);
    } else {
        out += static_cast<char>(0x80 | 126);
        out += static_cast<char>((n >> 8) & 0xFF);
        out += static_cast<char>(n & 0xFF);
    }
    out.append(mask, 4);
    for (size_t i = 0; i < n; ++i) {
        out += static_cast<char>(payload[i] ^ mask[i % 4]);
    }
    return out;
}

std::string upgrade_request(const char* origin = nullptr,
                            const char* conn = "Upgrade",
                            const char* ver = "13",
                            const char* key = "dGhlIHNhbXBsZSBub25jZQ==") {
    std::string r = "GET /ws?token=t&instance=i HTTP/1.1\r\n"
                    "Host: 127.0.0.1\r\n"
                    "Upgrade: websocket\r\n";
    r += std::string("Connection: ") + conn + "\r\n";
    r += std::string("Sec-WebSocket-Key: ") + key + "\r\n";
    r += std::string("Sec-WebSocket-Version: ") + ver + "\r\n";
    if (origin) r += std::string("Origin: ") + origin + "\r\n";
    return r + "\r\n";
}
} // namespace

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "http_parse_basics") {
        size_t used = 0;
        const auto req = parse(
            "GET /ws?token=abc&instance=x HTTP/1.1\r\n"
            "Host: 127.0.0.1:8787\r\n"
            "X-Test:  spaced  \r\n"
            "\r\nBODY",
            &used);
        NT_CHECK(req.method == "GET", "method");
        NT_CHECK(req.path == "/ws", "path without query");
        NT_CHECK(req.query == "token=abc&instance=x", "query raw");
        NT_CHECK(req.header("host") != nullptr, "host present");
        NT_CHECK(std::string(req.header("x-test")) == "spaced",
                 "value trimmed");
        NT_CHECK(used + 4 == std::string("GET /ws?token=abc&instance=x "
                                         "HTTP/1.1\r\nHost: "
                                         "127.0.0.1:8787\r\nX-Test:  "
                                         "spaced  \r\n\r\nBODY").size(),
                 "consumed ends before body");
    }
    NT_END_TEST(SUITE, "http_parse_basics");

    NT_TEST(SUITE, "http_parse_rejects") {
        gate::HttpRequest req;
        size_t used = 0;
        NT_CHECK(!gate::http_parse_request("GARBAGE\r\n\r\n", &req, &used),
                 "no version");
        NT_CHECK(!gate::http_parse_request("GET /x HTTP/1.1\r\nBad\r\n\r\n",
                                           &req, &used),
                 "header without colon");
        NT_CHECK(!gate::http_parse_request("GET /x HTTP/1.1\r\n: v\r\n\r\n",
                                           &req, &used),
                 "empty header name");
        std::string big = "GET /x HTTP/1.1\r\n";
        for (int i = 0; i < 65; ++i) big += "H: v\r\n";
        big += "\r\n";
        NT_CHECK(!gate::http_parse_request(big, &req, &used),
                 ">64 headers rejected");
        std::string longline = "GET /x HTTP/1.1\r\nH: ";
        longline.append(9000, 'a');
        longline += "\r\n\r\n";
        NT_CHECK(!gate::http_parse_request(longline, &req, &used),
                 "oversized header line rejected");
    }
    NT_END_TEST(SUITE, "http_parse_rejects");

    NT_TEST(SUITE, "query_first_semantics") {
        NT_CHECK(gate::query_first("token=ab+c%20d&x=1", "token") ==
                     "ab c d",
                 "plus and percent decode");
        NT_CHECK(gate::query_first("token=A&token=B", "token") == "A",
                 "first occurrence wins (parse_qs [0])");
        NT_CHECK(gate::query_first("a=1&b=2", "instance") == "",
                 "missing key empty");
        NT_CHECK(gate::query_first("flag", "flag") == "",
                 "bare flag value empty");
        NT_CHECK(gate::query_first("tok%ZZen=v", "token") == "",
                 "malformed key not matched");
    }
    NT_END_TEST(SUITE, "query_first_semantics");

    NT_TEST(SUITE, "gate_decide_routes") {
        size_t used = 0;
        const std::string inst = "inst-xyz";

        auto d = [&](const std::string& raw) {
            const auto req = parse(raw, &used);
            return gate::gate_decide(req, "shut-1", TOKEN64, inst);
        };

        NT_CHECK(d("GET /health HTTP/1.1\r\n\r\n").action ==
                     gate::GateAction::RespondHealth,
                 "/health");
        NT_CHECK(d("GET /metrics HTTP/1.1\r\n\r\n").action ==
                     gate::GateAction::RespondMetrics,
                 "/metrics");

        NT_CHECK(
            d("GET /shutdown HTTP/1.1\r\n"
              "X-GPTBridge-Shutdown-Token: shut-1\r\n\r\n").action ==
                gate::GateAction::RespondShutdown,
            "/shutdown valid token");
        NT_CHECK(
            d("GET /shutdown HTTP/1.1\r\n"
              "X-GPTBridge-Shutdown-Token: wrong\r\n\r\n").action ==
                gate::GateAction::Reject,
            "/shutdown wrong token");
        NT_CHECK(d("GET /shutdown HTTP/1.1\r\n\r\n").action ==
                     gate::GateAction::Reject,
                 "/shutdown missing token");

        const std::string ws_ok = "GET /ws?token=" + std::string(TOKEN64) +
                                  "&instance=" + inst + " HTTP/1.1\r\n\r\n";
        NT_CHECK(d(ws_ok).action == gate::GateAction::WsUpgrade,
                 "ws gate pass");
        const std::string ws_upper =
            "GET /ws?token="
            "0123456789ABCDEF0123456789abcdef"
            "0123456789abcdef0123456789abcdef&instance=" +
            inst + " HTTP/1.1\r\n\r\n";
        NT_CHECK(d(ws_upper).action == gate::GateAction::WsUpgrade,
                 "uppercase token lowered (Python .lower() parity)");
        NT_CHECK(d("GET /ws?token=bad&instance=inst-xyz HTTP/1.1\r\n\r\n")
                     .action == gate::GateAction::Reject,
                 "ws bad token");
        NT_CHECK(d("GET /ws?token=0123456789abcdef0123456789abcdef"
                   "0123456789abcdef0123456789abcdef&instance=other "
                   "HTTP/1.1\r\n\r\n")
                     .action == gate::GateAction::Reject,
                 "ws wrong instance");
        NT_CHECK(d("GET /other HTTP/1.1\r\n\r\n").action ==
                     gate::GateAction::Reject,
                 "unknown path no creds");
        NT_CHECK(d("GET /health?token=x HTTP/1.1\r\n\r\n").action ==
                     gate::GateAction::RespondHealth,
                 "/health ignores creds (Python order)");
    }
    NT_END_TEST(SUITE, "gate_decide_routes");

    NT_TEST(SUITE, "http_response_shape") {
        const std::string r = gate::http_response(
            403, "FORBIDDEN", "Forbidden", "text/plain");
        NT_CHECK(r.rfind("HTTP/1.1 403 FORBIDDEN\r\n", 0) == 0,
                 "status line");
        NT_CHECK(r.find("Content-Type: text/plain\r\n") !=
                     std::string::npos,
                 "content type");
        NT_CHECK(r.find("Content-Length: 9\r\n") != std::string::npos,
                 "length");
        NT_CHECK(r.substr(r.size() - 9) == "Forbidden", "body tail");
    }
    NT_END_TEST(SUITE, "http_response_shape");

    NT_TEST(SUITE, "ws_accept_rfc_vector") {
        /* RFC6455 §1.3 canonical example */
        NT_CHECK(ws::ws_accept_key("dGhlIHNhbXBsZSBub25jZQ==") ==
                     "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
                 "RFC6455 accept vector");
    }
    NT_END_TEST(SUITE, "ws_accept_rfc_vector");

    NT_TEST(SUITE, "ws_validate_upgrade") {
        size_t used = 0;
        std::string accept;

        auto req = parse(upgrade_request(), &used);
        NT_CHECK(ws::ws_validate_upgrade(req, &accept),
                 "canonical upgrade");
        NT_CHECK(accept == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
                 "accept matches RFC vector");

        req = parse(upgrade_request("file://"), &used);
        NT_CHECK(ws::ws_validate_upgrade(req, nullptr),
                 "origin file:// allowed");
        req = parse(upgrade_request("null"), &used);
        NT_CHECK(ws::ws_validate_upgrade(req, nullptr),
                 "origin null allowed");
        req = parse(upgrade_request("https://evil.example"), &used);
        NT_CHECK(!ws::ws_validate_upgrade(req, nullptr),
                 "foreign origin denied");

        req = parse(upgrade_request(nullptr, "keep-alive, Upgrade"), &used);
        NT_CHECK(ws::ws_validate_upgrade(req, nullptr),
                 "connection token list");
        req = parse(upgrade_request(nullptr, "keep-alive"), &used);
        NT_CHECK(!ws::ws_validate_upgrade(req, nullptr),
                 "connection without upgrade denied");
        req = parse(upgrade_request(nullptr, "Upgrade", "12"), &used);
        NT_CHECK(!ws::ws_validate_upgrade(req, nullptr),
                 "version 12 denied");
        req = parse(upgrade_request(nullptr, "Upgrade", "13",
                                    "not-base64!!!"), &used);
        NT_CHECK(!ws::ws_validate_upgrade(req, nullptr),
                 "bad key denied");
        req = parse(upgrade_request(nullptr, "Upgrade", "13",
                                    "AA=="), &used);
        NT_CHECK(!ws::ws_validate_upgrade(req, nullptr),
                 "short key denied (not 16 bytes)");
    }
    NT_END_TEST(SUITE, "ws_validate_upgrade");

    NT_TEST(SUITE, "ws_frame_decode_masked") {
        const std::string bytes =
            masked_frame(0x81, "hello", "\x01\x02\x03\x04");
        ws::Frame f;
        size_t used = 0;
        bool perr = true;
        NT_CHECK(ws::ws_frame_decode(bytes, &f, &used, &perr),
                 "masked text decoded");
        NT_CHECK(!perr, "no protocol error");
        NT_CHECK(f.fin && f.opcode == ws::Op::Text, "fin+text");
        NT_CHECK(f.payload == "hello", "payload unmasked");
        NT_CHECK(used == bytes.size(), "consumed all");

        /* incomplete → retry, not error */
        NT_CHECK(!ws::ws_frame_decode(bytes.substr(0, 3), &f, &used,
                                      &perr),
                 "short buffer retry");
        NT_CHECK(!perr && used == 0, "retry not error");

        /* unmasked client frame → protocol error */
        NT_CHECK(!ws::ws_frame_decode("\x81\x05hello", &f, &used, &perr),
                 "unmasked rejected");
        NT_CHECK(perr, "protocol error flagged");

        /* ping decodes */
        const std::string ping = masked_frame(0x89, "pp", "\x0A\x0B\x0C\x0D");
        NT_CHECK(ws::ws_frame_decode(ping, &f, &used, &perr) &&
                     f.opcode == ws::Op::Ping && f.payload == "pp",
                 "masked ping");
    }
    NT_END_TEST(SUITE, "ws_frame_decode_masked");

    NT_TEST(SUITE, "ws_frame_decode_bounds") {
        ws::Frame f;
        size_t used = 0;
        bool perr = false;

        /* 126-extended length round trip */
        std::string big(200, 'x');
        const std::string enc = masked_frame(0x82, big, "\x05\x06\x07\x08");
        NT_CHECK(ws::ws_frame_decode(enc, &f, &used, &perr) &&
                     f.payload == big,
                 "16-bit extended length");

        /* non-minimal length encoding → protocol error */
        const char bad_len[] = "\x81\xFE\x00\x05";
        NT_CHECK(!ws::ws_frame_decode(
                     std::string(bad_len, 4) + "\x00\x00\x00\x00aaaaa",
                     &f, &used, &perr),
                 "non-minimal ext len rejected");
        NT_CHECK(perr, "flagged");

        /* fragmented control → protocol error */
        NT_CHECK(!ws::ws_frame_decode(masked_frame(0x09, "x", "aaaa"), &f,
                                      &used, &perr),
                 "fragmented ping rejected");
        NT_CHECK(perr, "control fin enforced");

        /* oversize declared payload → protocol error */
        std::string hdr;
        hdr += '\x81';
        hdr += static_cast<char>(0x80 | 126);
        hdr += static_cast<char>(0xFF);
        hdr += static_cast<char>(0xFF);
        hdr += "abcd";
        NT_CHECK(!ws::ws_frame_decode(hdr, &f, &used, &perr),
                 "declared >1MiB rejected");
        NT_CHECK(perr, "size bound enforced");
    }
    NT_END_TEST(SUITE, "ws_frame_decode_bounds");

    NT_TEST(SUITE, "ws_frame_encode_and_helpers") {
        const std::string e = ws::ws_frame_encode(ws::Op::Text, "hi");
        NT_CHECK(e.size() == 4 &&
                     (static_cast<uint8_t>(e[0]) == 0x81) &&
                     (static_cast<uint8_t>(e[1]) == 0x02) &&
                     e.substr(2) == "hi",
                 "unmasked server frame");
        std::string mid(130, 'y');
        const std::string e2 = ws::ws_frame_encode(ws::Op::Text, mid);
        NT_CHECK(static_cast<uint8_t>(e2[1]) == 126,
                 "16-bit length marker");
        NT_CHECK(ws::ws_frame_encode(
                     ws::Op::Text, std::string((1 << 20) + 1, 'z'))
                     .empty(),
                 ">1MiB encode refused");
        const std::string pong = ws::ws_pong("pp");
        NT_CHECK(static_cast<uint8_t>(pong[0]) == 0x8A &&
                     pong.substr(2) == "pp",
                 "pong echoes payload");
        const std::string close = ws::ws_close(1000, "");
        NT_CHECK(static_cast<uint8_t>(close[0]) == 0x88 &&
                 static_cast<uint8_t>(close[2]) == 0x03 &&
                 static_cast<uint8_t>(close[3]) == 0xE8,
                 "close code 1000");
    }
    NT_END_TEST(SUITE, "ws_frame_encode_and_helpers");

    return native_tests::report("ws_http_gate_suite.json");
}

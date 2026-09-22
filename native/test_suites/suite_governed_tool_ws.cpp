// Suite: governed_tool_ws — HTTP/WS gate codec (star-governed-tool-runtime-
// abi/v1 §3). Covers http_request_parse, route_request ladder parity,
// compare_digest, ws_accept_key RFC 6455 vector, and frame encode/decode.
#include "harness.hpp"

#include <cstring>
#include <string>

#include "governed_tool_ws.h"

namespace {
const char* SUITE = "GOVERNED_TOOL_WS_SUITE";
namespace gtw = gptbridge::gtw;

gtw::HttpRequest parse_req(const std::string& raw, size_t* consumed) {
    gtw::HttpRequest r;
    if (!gtw::http_request_parse(
            reinterpret_cast<const uint8_t*>(raw.data()), raw.size(), &r,
            consumed)) {
        throw std::runtime_error("parse failed");
    }
    return r;
}
}

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "compare_digest_semantics") {
        NT_CHECK(gtw::compare_digest("abc", "abc"), "equal");
        NT_CHECK(!gtw::compare_digest("abc", "abd"), "diff");
        NT_CHECK(!gtw::compare_digest("abc", "ab"), "length");
        NT_CHECK(!gtw::compare_digest("", "x"), "empty vs x");
        NT_CHECK(gtw::compare_digest("", ""), "both empty");
    }
    NT_END_TEST(SUITE, "compare_digest_semantics");

    NT_TEST(SUITE, "http_request_parse") {
        size_t consumed = 0;
        const std::string raw =
            "GET /ws?token=ABC&instance=i1&token=DEF HTTP/1.1\r\n"
            "Host: 127.0.0.1:9100\r\n"
            "X-GPTBridge-Shutdown-Token:  s3cr3t \r\n"
            "\r\nBODY";
        const gtw::HttpRequest r = parse_req(raw, &consumed);
        NT_CHECK(r.method == "GET", "method");
        NT_CHECK(r.path == "/ws", "path");
        NT_CHECK(r.query.size() == 3, "query count");
        NT_CHECK(r.query[0].first == "token" && r.query[0].second == "ABC",
                 "first token");
        NT_CHECK(r.query[1].second == "i1", "instance");
        NT_CHECK(r.query[2].second == "DEF", "dup token kept");
        const char* st = r.header("x-gptbridge-shutdown-token");
        NT_CHECK(st != nullptr && std::strcmp(st, "s3cr3t") == 0,
                 "header trim+lowercase");
        NT_CHECK(consumed == raw.size() - 4, "consumed excludes body");
    }
    NT_END_TEST(SUITE, "http_request_parse");

    NT_TEST(SUITE, "route_request_ladder") {
        size_t c = 0;
        const std::string tok(64, 'a');
        const std::string inst = "0123456789abcdef";

        NT_CHECK(gtw::route_request(
                     parse_req("GET /health HTTP/1.1\r\n\r\n", &c),
                     tok, inst, "sh") == gtw::GateDecision::Health,
                 "health");
        NT_CHECK(gtw::route_request(
                     parse_req("GET /metrics HTTP/1.1\r\n\r\n", &c),
                     tok, inst, "sh") == gtw::GateDecision::Metrics,
                 "metrics");

        /* /shutdown: header token via compare_digest; empty runtime
           shutdown_token -> always Forbidden */
        NT_CHECK(gtw::route_request(
                     parse_req("GET /shutdown HTTP/1.1\r\n"
                               "X-GPTBridge-Shutdown-Token: sh\r\n\r\n", &c),
                     tok, inst, "sh") == gtw::GateDecision::Shutdown,
                 "shutdown ok");
        NT_CHECK(gtw::route_request(
                     parse_req("GET /shutdown HTTP/1.1\r\n"
                               "X-GPTBridge-Shutdown-Token: bad\r\n\r\n", &c),
                     tok, inst, "sh") == gtw::GateDecision::Forbidden,
                 "shutdown bad");
        NT_CHECK(gtw::route_request(
                     parse_req("GET /shutdown HTTP/1.1\r\n\r\n", &c),
                     tok, inst, "sh") == gtw::GateDecision::Forbidden,
                 "shutdown missing hdr");
        NT_CHECK(gtw::route_request(
                     parse_req("GET /shutdown HTTP/1.1\r\n"
                               "X-GPTBridge-Shutdown-Token: sh\r\n\r\n", &c),
                     tok, inst, "") == gtw::GateDecision::Forbidden,
                 "empty runtime token -> forbidden");

        /* WS gate: token (lowercased) + instance */
        const std::string good =
            "GET /ws?token=" + tok + "&instance=" + inst +
            " HTTP/1.1\r\n\r\n";
        NT_CHECK(gtw::route_request(parse_req(good, &c), tok, inst, "sh")
                     == gtw::GateDecision::Upgrade, "upgrade ok");
        const std::string upper_tok =
            "GET /ws?token=" + std::string(64, 'A') + "&instance=" + inst +
            " HTTP/1.1\r\n\r\n";
        NT_CHECK(gtw::route_request(parse_req(upper_tok, &c), tok, inst, "sh")
                     == gtw::GateDecision::Upgrade,
                 "token query lowercased");
        const std::string bad_inst =
            "GET /ws?token=" + tok + "&instance=WRONG HTTP/1.1\r\n\r\n";
        NT_CHECK(gtw::route_request(parse_req(bad_inst, &c), tok, inst, "sh")
                     == gtw::GateDecision::Forbidden, "bad instance");
        const std::string no_q = "GET /ws HTTP/1.1\r\n\r\n";
        NT_CHECK(gtw::route_request(parse_req(no_q, &c), tok, inst, "sh")
                     == gtw::GateDecision::Forbidden, "no query");
    }
    NT_END_TEST(SUITE, "route_request_ladder");

    NT_TEST(SUITE, "ws_accept_key_rfc6455") {
        /* RFC 6455 §1.3 known vector */
        NT_CHECK(gtw::ws_accept_key("dGhlIHNhbXBsZSBub25jZQ==") ==
                     "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
                 "rfc vector");
        const std::string resp =
            gtw::ws_upgrade_response("dGhlIHNhbXBsZSBub25jZQ==");
        NT_CHECK(resp.find("101 Switching Protocols") != std::string::npos,
                 "101");
        NT_CHECK(resp.find("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=") !=
                     std::string::npos, "accept hdr");
    }
    NT_END_TEST(SUITE, "ws_accept_key_rfc6455");

    NT_TEST(SUITE, "ws_frame_roundtrip") {
        /* server->client encode (unmasked) then decode must fail
           (inbound requires mask); craft masked client frame manually */
        const std::string enc =
            gtw::ws_frame_encode(true, gtw::WsOp::Text, "hello");
        NT_CHECK(enc.size() == 2 + 5 && (enc[0] & 0x7F) == 0x01,
                 "encode text");
        gtw::WsFrame f;
        NT_CHECK(gtw::ws_frame_decode(
                     reinterpret_cast<const uint8_t*>(enc.data()),
                     enc.size(), &f) == -1, "unmasked inbound rejected");

        /* masked client text frame: fin|text, mask|len5, mask4, xored */
        const char* p = "hello";
        uint8_t wire[11];
        wire[0] = 0x81; wire[1] = 0x85;
        wire[2] = 0x12; wire[3] = 0x34; wire[4] = 0x56; wire[5] = 0x78;
        const uint8_t mk[4] = {0x12, 0x34, 0x56, 0x78};
        for (int i = 0; i < 5; ++i) wire[6 + i] = p[i] ^ mk[i % 4];
        const int64_t n = gtw::ws_frame_decode(wire, sizeof(wire), &f);
        NT_CHECK(n == 11, "consumed");
        NT_CHECK(f.fin && f.opcode == gtw::WsOp::Text && f.payload == "hello",
                 "payload unmasked");

        /* incomplete -> 0 */
        NT_CHECK(gtw::ws_frame_decode(wire, 6, &f) == 0, "partial header");
        NT_CHECK(gtw::ws_frame_decode(wire, 10, &f) == 0, "partial payload");

        /* 126 extended length */
        std::string big(200, 'x');
        uint8_t hdr[8] = {0x82, 0xFE, 0, 200, 1, 2, 3, 4};
        std::string frame(reinterpret_cast<char*>(hdr), 8);
        for (size_t i = 0; i < big.size(); ++i)
            frame += static_cast<char>(big[i] ^ hdr[4 + i % 4]);
        NT_CHECK(gtw::ws_frame_decode(
                     reinterpret_cast<const uint8_t*>(frame.data()),
                     frame.size(), &f) == static_cast<int64_t>(frame.size()),
                 "ext len");
        NT_CHECK(f.payload == big, "ext payload");

        /* control frame rules: unfragmented, <=125 */
        uint8_t frag_ping[7] = {0x09, 0x81, 1, 2, 3, 4, 0};
        NT_CHECK(gtw::ws_frame_decode(frag_ping, sizeof(frag_ping), &f) == -1,
                 "fragmented ping");
        uint8_t rsv[7] = {0xC1, 0x80, 1, 2, 3, 4};
        NT_CHECK(gtw::ws_frame_decode(rsv, sizeof(rsv), &f) == -1, "rsv bit");
    }
    NT_END_TEST(SUITE, "ws_frame_roundtrip");

    return native_tests::report("governed_tool_ws_suite.json");
}

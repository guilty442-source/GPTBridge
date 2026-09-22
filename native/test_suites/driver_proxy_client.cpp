/* driver_proxy_client.cpp — star-governed-transport-proxy/v1 線協定
 * interop driver（M1 模式 B）。
 *
 * 非測試套件：本程式把 tpx::（零 I/O codec）暴露成 CLI，供 Python
 * 測試在「C++ encode → 真實 Python TransportProxyAgent → C++ decode」
 * 的管線中逐 op 驗證線協定一致性。所有 argv 皆純字串；JSON 片段由
 * 呼叫方提供（無 shell 介入）。
 *
 * 用法：
 *   driver_proxy_client.exe args-empty
 *   driver_proxy_client.exe args-channel <channel>
 *   driver_proxy_client.exe args-hello <tool_id> <instance_id>
 *       <channels_csv> [submit_csv]
 *       channels_csv: system=process,ai=submit
 *       submit_csv:   ai|actor|authorizer,...（'-' = 無 submit 綁定）
 *   driver_proxy_client.exe args-respond <channel> <request_id> <resp_json>
 *   driver_proxy_client.exe args-request-id <channel> <request_id>
 *   driver_proxy_client.exe args-progress <channel> <request_id> <payload>
 *   driver_proxy_client.exe args-ack <channel> <push_id> [resp_json|'-']
 *   driver_proxy_client.exe args-request <channel> <target> <command>
 *       <payload_json> [request_id|'-']
 *   driver_proxy_client.exe args-submit-response <channel> <target> <rid>
 *   driver_proxy_client.exe args-push <channel> <target> <command>
 *       <payload_json> [push_id|'-']
 *   driver_proxy_client.exe encode <id> <op> <args_json>
 *   driver_proxy_client.exe decode          （stdin 逐行回應 → decoded JSONL）
 *   driver_proxy_client.exe waiter <deadline_at> <state_json|'null'>
 */

#include <cstdio>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "transport_proxy_client.h"

using gptbridge::jsonlite::JsonError;
using gptbridge::jsonlite::JsonParser;
using gptbridge::jsonlite::JsonValue;
using gptbridge::jsonlite::json_serialize;
namespace tpx = gptbridge::tpx;

namespace {

int usage() {
    std::fprintf(stderr, "driver_proxy_client: unknown/missing args\n");
    return 2;
}

std::string opt(int argc, char** argv, int i) {
    return i < argc ? std::string(argv[i]) : std::string("-");
}

/* chan=mode 清單 → HelloChannel */
std::vector<tpx::HelloChannel> parse_channels(const std::string& csv) {
    std::vector<tpx::HelloChannel> out;
    std::stringstream ss(csv);
    std::string item;
    while (std::getline(ss, item, ',')) {
        const auto eq = item.find('=');
        if (eq == std::string::npos) continue;
        out.push_back({item.substr(0, eq), item.substr(eq + 1)});
    }
    return out;
}

/* chan|actor|authorizer 清單 → HelloSubmit */
std::vector<tpx::HelloSubmit> parse_submit(const std::string& csv) {
    std::vector<tpx::HelloSubmit> out;
    if (csv.empty() || csv == "-") return out;
    std::stringstream ss(csv);
    std::string item;
    while (std::getline(ss, item, ',')) {
        const auto p1 = item.find('|');
        const auto p2 = p1 == std::string::npos
                            ? std::string::npos
                            : item.find('|', p1 + 1);
        if (p1 == std::string::npos || p2 == std::string::npos) continue;
        out.push_back({item.substr(0, p1), item.substr(p1 + 1, p2 - p1 - 1),
                       item.substr(p2 + 1)});
    }
    return out;
}

std::string dash(const std::string& s) { return s == "-" ? "" : s; }

int do_decode() {
    std::string line;
    while (std::getline(std::cin, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty()) continue;
        tpx::ProxyResponse r;
        if (!tpx::decode_response_line(line, &r)) {
            std::cout << "{\"valid\":false}\n";
            continue;
        }
        std::cout << "{\"valid\":true,\"id\":";
        std::cout << json_serialize(JsonValue{JsonValue::Type::String,
                                              false, 0.0, r.id, {}, {}});
        if (r.ok) {
            std::cout << ",\"ok\":true,\"result\":"
                      << (r.result.type == JsonValue::Type::Null
                              ? "null"
                              : json_serialize(r.result));
        } else {
            JsonValue code{JsonValue::Type::String, false, 0.0,
                           r.error_code, {}, {}};
            JsonValue msg{JsonValue::Type::String, false, 0.0,
                          r.error_message, {}, {}};
            std::cout << ",\"ok\":false,\"error_code\":"
                      << json_serialize(code)
                      << ",\"error_message\":" << json_serialize(msg);
        }
        std::cout << "}\n";
    }
    std::cout.flush();
    return 0;
}

int do_waiter(int argc, char** argv) {
    if (argc < 4) return usage();
    const double deadline = std::strtod(argv[2], nullptr);
    const std::string raw = argv[3];
    tpx::RequestWaiter w(deadline);
    tpx::RequestWaiter::State st;
    JsonValue state;
    if (raw == "null") {
        st = w.feed(nullptr);
    } else {
        try {
            state = JsonParser(raw).parse();
        } catch (const JsonError&) {
            std::fprintf(stderr, "driver_proxy_client: bad state json\n");
            return 2;
        }
        st = w.feed(&state);
    }
    const char* name = "Pending";
    if (st == tpx::RequestWaiter::State::Completed) name = "Completed";
    if (st == tpx::RequestWaiter::State::Cancelled) name = "Cancelled";
    if (st == tpx::RequestWaiter::State::Failed) name = "Failed";
    std::cout << "{\"state\":\"" << name << "\"";
    if (st == tpx::RequestWaiter::State::Completed) {
        std::cout << ",\"result\":" << json_serialize(w.result());
    }
    if (!w.error_code().empty()) {
        JsonValue code{JsonValue::Type::String, false, 0.0,
                       w.error_code(), {}, {}};
        std::cout << ",\"error_code\":" << json_serialize(code);
    }
    std::cout << ",\"deadline_exceeded\":"
              << (w.deadline_exceeded(deadline + 60.0) ? "true" : "false")
              << "}\n";
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 2) return usage();
    const std::string mode = argv[1];

    if (mode == "encode" && argc >= 5) {
        std::cout << tpx::encode_request(argv[2], argv[3], argv[4]) << "\n";
        return 0;
    }
    if (mode == "decode") return do_decode();
    if (mode == "waiter") return do_waiter(argc, argv);

    /* args builders — 輸出 args JSON 物件字串 */
    if (mode == "args-empty") {
        std::cout << tpx::args_empty() << "\n";
        return 0;
    }
    if (mode == "args-channel" && argc >= 3) {
        std::cout << tpx::args_channel(argv[2]) << "\n";
        return 0;
    }
    if (mode == "args-hello" && argc >= 5) {
        std::cout << tpx::args_hello(argv[2], argv[3],
                                     parse_channels(argv[4]),
                                     parse_submit(opt(argc, argv, 5)))
                  << "\n";
        return 0;
    }
    if (mode == "args-respond" && argc >= 5) {
        std::cout << tpx::args_respond(argv[2], argv[3], argv[4]) << "\n";
        return 0;
    }
    if (mode == "args-request-id" && argc >= 4) {
        std::cout << tpx::args_request_id(argv[2], argv[3]) << "\n";
        return 0;
    }
    if (mode == "args-progress" && argc >= 5) {
        std::cout << tpx::args_progress(argv[2], argv[3], argv[4]) << "\n";
        return 0;
    }
    if (mode == "args-ack" && argc >= 4) {
        std::cout << tpx::args_ack(argv[2], argv[3],
                                   dash(opt(argc, argv, 4)))
                  << "\n";
        return 0;
    }
    if (mode == "args-request" && argc >= 6) {
        std::cout << tpx::args_submit_request(argv[2], argv[3], argv[4],
                                              argv[5],
                                              dash(opt(argc, argv, 6)))
                  << "\n";
        return 0;
    }
    if (mode == "args-submit-response" && argc >= 5) {
        std::cout << tpx::args_submit_response(argv[2], argv[3], argv[4])
                  << "\n";
        return 0;
    }
    if (mode == "args-push" && argc >= 6) {
        std::cout << tpx::args_push(argv[2], argv[3], argv[4], argv[5],
                                    dash(opt(argc, argv, 6)))
                  << "\n";
        return 0;
    }
    return usage();
}

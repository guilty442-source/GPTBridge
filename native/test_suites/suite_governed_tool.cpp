// Suite: M1 governed_tool C prototype (star-governed-tool-runtime-abi/v1).
// Covers the parity-checklist decision subset: env/tool_id/token/port
// validation, workspace_instance_id, shutdown + WS gates, request
// pre-validation, idle backoff sequence, and channel_health degrade.
#include "harness.hpp"

#include <cstring>
#include <string>

extern "C" {
#include "governed_tool.h"
}

namespace {
const char* SUITE = "GOVERNED_TOOL_SUITE";
const char* TOKEN64 =
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
}

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "tool_id_regex") {
        NT_CHECK(gptbridge_gt_tool_id_valid("system-rescue") == 1,
                 "canonical tool id");
        NT_CHECK(gptbridge_gt_tool_id_valid("a1") == 1, "2-char ok");
        NT_CHECK(gptbridge_gt_tool_id_valid("x") == 0, "1-char rejected");
        NT_CHECK(gptbridge_gt_tool_id_valid("_lead") == 0,
                 "leading underscore rejected");
        NT_CHECK(gptbridge_gt_tool_id_valid("UPPER") == 0,
                 "uppercase rejected");
        NT_CHECK(gptbridge_gt_tool_id_valid("has space") == 0,
                 "space rejected");
        NT_CHECK(gptbridge_gt_tool_id_valid(nullptr) == 0, "null rejected");
        const std::string long_id(64, 'a');
        NT_CHECK(gptbridge_gt_tool_id_valid(long_id.c_str()) == 0,
                 "64-char rejected");
        const std::string max_id(63, 'a');
        NT_CHECK(gptbridge_gt_tool_id_valid(max_id.c_str()) == 1,
                 "63-char ok");
    }
    NT_END_TEST(SUITE, "tool_id_regex");

    NT_TEST(SUITE, "session_token_and_port") {
        NT_CHECK(gptbridge_gt_session_token_valid(TOKEN64) == 1,
                 "64 lowercase hex");
        NT_CHECK(gptbridge_gt_session_token_valid("abc") == 0, "short");
        // Python: str(env).strip().lower() then fullmatch — uppercase
        // and surrounding whitespace still pass.
        NT_CHECK(gptbridge_gt_session_token_valid(
                     "0123456789ABCDEF0123456789abcdef"
                     "0123456789abcdef0123456789abcdef") == 1,
                 "uppercase hex accepted (Python .lower() parity)");
        NT_CHECK(gptbridge_gt_session_token_valid(
                     "  0123456789abcdef0123456789abcdef"
                     "0123456789abcdef0123456789abcdef\n") == 1,
                 "surrounding whitespace stripped (Python .strip())");
        NT_CHECK(gptbridge_gt_session_token_valid(
                     "0123456789abcgef0123456789abcdef"
                     "0123456789abcdef0123456789abcdef") == 0,
                 "non-hex rejected");
        NT_CHECK(gptbridge_gt_session_token_valid(nullptr) == 0, "null");

        NT_CHECK(gptbridge_gt_port_valid(1024) == 1, "min ok");
        NT_CHECK(gptbridge_gt_port_valid(65535) == 1, "max ok");
        NT_CHECK(gptbridge_gt_port_valid(1023) == 0, "below min");
        NT_CHECK(gptbridge_gt_port_valid(65536) == 0, "above max");
    }
    NT_END_TEST(SUITE, "session_token_and_port");

    NT_TEST(SUITE, "env_gate_conjunction") {
        NT_CHECK(gptbridge_gt_env_gate(1, 1, TOKEN64, 8765, 1) == 1,
                 "all facts pass");
        NT_CHECK(gptbridge_gt_env_gate(0, 1, TOKEN64, 8765, 1) == 0,
                 "bad root");
        NT_CHECK(gptbridge_gt_env_gate(1, 0, TOKEN64, 8765, 1) == 0,
                 "bad tool dir");
        NT_CHECK(gptbridge_gt_env_gate(1, 1, "bad", 8765, 1) == 0,
                 "bad token");
        NT_CHECK(gptbridge_gt_env_gate(1, 1, TOKEN64, 80, 1) == 0,
                 "bad port");
        NT_CHECK(gptbridge_gt_env_gate(1, 1, TOKEN64, 8765, 0) == 0,
                 "bad bootstrap");
    }
    NT_END_TEST(SUITE, "env_gate_conjunction");

    NT_TEST(SUITE, "workspace_instance_id") {
        char out[17];
        NT_CHECK(gptbridge_gt_workspace_instance_id(
                     "system-rescue", 8765, out) == 1, "computed");
        // sha256("system-rescue:8765")[:16] (Python hashlib parity)
        NT_CHECK(std::strcmp(out, "4585146487962aa9") == 0,
                 "matches hashlib[:16]");
        NT_CHECK(gptbridge_gt_workspace_instance_id(
                     "investment-mobile", 9000, out) == 1, "computed 2");
        NT_CHECK(std::strcmp(out, "25cd5b9152e8e334") == 0,
                 "matches hashlib[:16] second");
    }
    NT_END_TEST(SUITE, "workspace_instance_id");

    NT_TEST(SUITE, "shutdown_gate") {
        NT_CHECK(gptbridge_gt_shutdown_gate("", "x") == 0,
                 "empty env token always 403");
        NT_CHECK(gptbridge_gt_shutdown_gate(nullptr, "x") == 0,
                 "null env token always 403");
        NT_CHECK(gptbridge_gt_shutdown_gate("secret", "secret") == 1,
                 "match");
        NT_CHECK(gptbridge_gt_shutdown_gate("secret", "secreX") == 0,
                 "mismatch");
        NT_CHECK(gptbridge_gt_shutdown_gate("secret", "secret-longer") == 0,
                 "length mismatch");
        NT_CHECK(gptbridge_gt_shutdown_gate("secret", nullptr) == 0,
                 "missing header");
    }
    NT_END_TEST(SUITE, "shutdown_gate");

    NT_TEST(SUITE, "ws_gate") {
        NT_CHECK(gptbridge_gt_ws_gate(TOKEN64, TOKEN64, "inst1", "inst1")
                     == 1, "both match");
        NT_CHECK(gptbridge_gt_ws_gate(
                     TOKEN64,
                     "0123456789ABCDEF0123456789abcdef"
                     "0123456789abcdef0123456789abcdef",
                     "inst1", "inst1") == 1,
                 "uppercase token lowercased before compare");
        NT_CHECK(gptbridge_gt_ws_gate(TOKEN64, "deadbeef", "inst1", "inst1")
                     == 0, "wrong token");
        NT_CHECK(gptbridge_gt_ws_gate(TOKEN64, TOKEN64, "inst1", "inst2")
                     == 0, "wrong instance");
        NT_CHECK(gptbridge_gt_ws_gate(TOKEN64, nullptr, "inst1", "inst1")
                     == 0, "missing token");
    }
    NT_END_TEST(SUITE, "ws_gate");

    NT_TEST(SUITE, "request_prevalidation") {
        NT_CHECK(gptbridge_gt_request_valid("cmd", 1, "req-1", nullptr,
                                            "tool-x") == 1,
                 "valid, tool_id defaulted");
        NT_CHECK(gptbridge_gt_request_valid("cmd", 1, "req-1", "tool-x",
                                            "tool-x") == 1,
                 "valid, tool_id match");
        NT_CHECK(gptbridge_gt_request_valid("", 1, "req-1", nullptr,
                                            "tool-x") == 0,
                 "empty command");
        NT_CHECK(gptbridge_gt_request_valid("cmd", 0, "req-1", nullptr,
                                            "tool-x") == 0,
                 "payload not dict");
        NT_CHECK(gptbridge_gt_request_valid("cmd", 1, "", nullptr,
                                            "tool-x") == 0,
                 "empty request_id");
        const std::string big_id(257, 'r');
        NT_CHECK(gptbridge_gt_request_valid("cmd", 1, big_id.c_str(),
                                            nullptr, "tool-x") == 0,
                 "request_id > 256");
        const std::string max_id(256, 'r');
        NT_CHECK(gptbridge_gt_request_valid("cmd", 1, max_id.c_str(),
                                            nullptr, "tool-x") == 1,
                 "request_id == 256 ok");
        NT_CHECK(gptbridge_gt_request_valid("cmd", 1, "req-1", "other",
                                            "tool-x") == 0,
                 "foreign tool_id denied");
    }
    NT_END_TEST(SUITE, "request_prevalidation");

    NT_TEST(SUITE, "idle_backoff_sequence") {
        // Python _worker: idle_poll starts 0.25s; timeout ->
        // min(x1.5, 0.5); notification/request -> reset 0.25s.
        int64_t v = gptbridge_gt_idle_next_ms(0, 0);
        NT_CHECK(v == 375, "0.25 -> 0.375");
        v = gptbridge_gt_idle_next_ms(v, 0);
        NT_CHECK(v == 500, "0.375 -> 0.5 cap");
        v = gptbridge_gt_idle_next_ms(v, 0);
        NT_CHECK(v == 500, "capped");
        NT_CHECK(gptbridge_gt_idle_next_ms(v, 1) == 250,
                 "notified -> reset to 0.25s");
    }
    NT_END_TEST(SUITE, "idle_backoff_sequence");

    NT_TEST(SUITE, "wait_timeout_selection") {
        // Python: 0.05 if notify_queue not empty else
        // max(idle_poll, 0.05).
        NT_CHECK(gptbridge_gt_wait_timeout_ms(375, 1) == 50,
                 "notify pending -> 50ms");
        NT_CHECK(gptbridge_gt_wait_timeout_ms(375, 0) == 375,
                 "no notify -> idle_poll");
        NT_CHECK(gptbridge_gt_wait_timeout_ms(10, 0) == 50,
                 "floor 50ms");
    }
    NT_END_TEST(SUITE, "wait_timeout_selection");

    NT_TEST(SUITE, "channel_health_degrade") {
        NT_CHECK(gptbridge_gt_health_degraded(0) == 0, "healthy");
        NT_CHECK(gptbridge_gt_health_degraded(1) == 0, "1 fail ok");
        NT_CHECK(gptbridge_gt_health_degraded(2) == 0, "2 fail ok");
        NT_CHECK(gptbridge_gt_health_degraded(3) == 1,
                 "3 consecutive -> degraded");
        NT_CHECK(gptbridge_gt_health_degraded(9) == 1, "stays degraded");
    }
    NT_END_TEST(SUITE, "channel_health_degrade");

    return native_tests::report("governed_tool_suite.json");
}

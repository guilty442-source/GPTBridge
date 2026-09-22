// Suite: M1 transport-proxy client codec (star-governed-transport-proxy/v1).
// Covers envelope encode (id/op/args), args builders, response decode
// (ok/error/dropped), and the RequestWaiter request_sync parity ladder.
#include "harness.hpp"

#include <string>

#include "transport_proxy_client.h"

namespace {
const char* SUITE = "TRANSPORT_PROXY_CLIENT_SUITE";
using gptbridge::jsonlite::JsonParser;
using gptbridge::jsonlite::JsonValue;
namespace tpx = gptbridge::tpx;

JsonValue parse(const std::string& s) { return JsonParser(s).parse(); }
}

int main() {
    NT_SUITE(SUITE);

    NT_TEST(SUITE, "encode_request_envelope") {
        const std::string line = tpx::encode_request(
            "id-1", "ping", tpx::args_empty());
        const JsonValue v = parse(line);
        NT_CHECK(v.get("v")->number == 1.0, "v=1");
        NT_CHECK(v.get("id")->string == "id-1", "id");
        NT_CHECK(v.get("op")->string == "ping", "op");
        NT_CHECK(v.get("args")->type == JsonValue::Type::Object, "args obj");
    }
    NT_END_TEST(SUITE, "encode_request_envelope");

    NT_TEST(SUITE, "args_builders") {
        const JsonValue hello = parse(tpx::args_hello(
            "system-rescue", "inst-9",
            {{"system", "process"}, {"ai", "submit"}},
            {{"ai", "governance/tool/system-rescue", "mod:auth"}}));
        NT_CHECK(hello.get("tool_id")->string == "system-rescue", "tool_id");
        NT_CHECK(hello.get("workspace_instance_id")->string == "inst-9",
                 "instance");
        const JsonValue* ch = hello.get("channels");
        NT_CHECK(ch->get("system")->string == "process", "sys mode");
        NT_CHECK(ch->get("ai")->string == "submit", "ai mode");
        const JsonValue* sb = hello.get("submit")->get("ai");
        NT_CHECK(sb->get("actor")->string == "governance/tool/system-rescue",
                 "actor");
        NT_CHECK(sb->get("authorizer")->string == "mod:auth", "authorizer");

        const JsonValue req = parse(tpx::args_submit_request(
            "ai", "xingcheng", "do_thing", "{\"a\":1}", "rid-1"));
        NT_CHECK(req.get("channel")->string == "ai", "channel");
        NT_CHECK(req.get("target_tool_id")->string == "xingcheng", "target");
        NT_CHECK(req.get("command")->string == "do_thing", "command");
        NT_CHECK(req.get("payload")->get("a")->number == 1.0, "payload passthru");
        NT_CHECK(req.get("request_id")->string == "rid-1", "request_id");

        const JsonValue req2 = parse(tpx::args_submit_request(
            "ai", "xingcheng", "c", "", ""));
        NT_CHECK(req2.get("payload")->type == JsonValue::Type::Object,
                 "empty payload -> {}");
        NT_CHECK(req2.get("request_id") == nullptr, "request_id omitted");

        const JsonValue resp = parse(tpx::args_respond(
            "system", "r9", "{\"ok\":true}"));
        NT_CHECK(resp.get("response")->get("ok")->boolean, "respond payload");

        const JsonValue esc = parse(tpx::args_request_id(
            "system", "r\"\\\n"));
        NT_CHECK(esc.get("request_id")->string == "r\"\\\n", "escaping");
    }
    NT_END_TEST(SUITE, "args_builders");

    NT_TEST(SUITE, "decode_response_ok_and_error") {
        tpx::ProxyResponse r;
        NT_CHECK(tpx::decode_response_line(
                     "{\"v\":1,\"id\":\"a\",\"ok\":true,"
                     "\"result\":{\"request\":{\"request_id\":\"r1\"}}}", &r),
                 "decode ok");
        NT_CHECK(r.valid && r.ok && r.id == "a", "fields");
        NT_CHECK(r.result.get("request")->get("request_id")->string == "r1",
                 "result passthru");

        NT_CHECK(tpx::decode_response_line(
                     "{\"v\":1,\"id\":\"b\",\"ok\":false,\"error\":{"
                     "\"code\":\"PERMISSION_DENIED\",\"message\":\"no\"}}", &r),
                 "decode err");
        NT_CHECK(!r.ok && r.error_code == "PERMISSION_DENIED" &&
                     r.error_message == "no",
                 "error fields");

        /* dropped lines: bad json, non-dict, wrong v, missing id, bad err */
        NT_CHECK(!tpx::decode_response_line("not json", &r), "garbage");
        NT_CHECK(!tpx::decode_response_line("[1,2]", &r), "array");
        NT_CHECK(!tpx::decode_response_line(
                     "{\"v\":2,\"id\":\"x\",\"ok\":true}", &r), "v2");
        NT_CHECK(!tpx::decode_response_line(
                     "{\"v\":1,\"ok\":true}", &r), "no id");
        NT_CHECK(!tpx::decode_response_line(
                     "{\"v\":1,\"id\":\"x\",\"ok\":false}", &r), "no err obj");
        NT_CHECK(!tpx::decode_response_line(
                     "{\"v\":1,\"id\":\"x\",\"ok\":false,"
                     "\"error\":{\"message\":\"m\"}}", &r), "no code");
    }
    NT_END_TEST(SUITE, "decode_response_ok_and_error");

    NT_TEST(SUITE, "request_waiter_parity") {
        /* null -> Pending; pending -> Pending; cancelled -> Cancelled;
           completed+dict -> Completed (request_id stripped);
           completed+non-dict -> Failed(PERMISSION_DENIED);
           deadline flag independent. */
        tpx::RequestWaiter w(100.0);
        JsonValue nullv;
        NT_CHECK(w.feed(&nullv) == tpx::RequestWaiter::State::Pending,
                 "null pending");
        const JsonValue queued = parse("{\"status\":\"queued\"}");
        NT_CHECK(w.feed(&queued) == tpx::RequestWaiter::State::Pending,
                 "queued pending");
        NT_CHECK(!w.deadline_exceeded(50.0) && w.deadline_exceeded(100.0),
                 "deadline");
        const JsonValue done = parse(
            "{\"status\":\"completed\",\"response\":{\"ok\":true,"
            "\"request_id\":\"r1\",\"data\":2}}");
        NT_CHECK(w.feed(&done) == tpx::RequestWaiter::State::Completed,
                 "completed");
        NT_CHECK(w.result().get("request_id") == nullptr,
                 "request_id stripped");
        NT_CHECK(w.result().get("data")->number == 2.0, "result kept");

        tpx::RequestWaiter w2(10.0);
        const JsonValue cancelled = parse("{\"status\":\"cancelled\"}");
        NT_CHECK(w2.feed(&cancelled) == tpx::RequestWaiter::State::Cancelled,
                 "cancelled");
        NT_CHECK(w2.error_code() == "GOVERNED_REQUEST_CANCELLED",
                 "cancelled code");

        tpx::RequestWaiter w3(10.0);
        const JsonValue bad = parse(
            "{\"status\":\"completed\",\"response\":[1]}");
        NT_CHECK(w3.feed(&bad) == tpx::RequestWaiter::State::Failed,
                 "non-dict response");
        NT_CHECK(w3.error_code() == "PERMISSION_DENIED", "fail code");

        tpx::RequestWaiter w4(10.0);
        NT_CHECK(w4.feed(nullptr) == tpx::RequestWaiter::State::Pending,
                 "nullptr feed");
    }
    NT_END_TEST(SUITE, "request_waiter_parity");

    return native_tests::report("transport_proxy_client_suite.json");
}

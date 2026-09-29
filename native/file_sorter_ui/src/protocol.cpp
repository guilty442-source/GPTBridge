/* protocol.cpp — governed command frame codec. */
#include "protocol.h"

#include <process.h>

namespace fsui {
namespace proto {

namespace {
unsigned long long counter_ = 0;
} // namespace

std::string next_request_id() {
    ++counter_;
    return "file-sorter-" + std::to_string(counter_) + "-" +
           std::to_string(_getpid());
}

std::string jescape(const std::string& s) { return gptbridge::jsonlite::json_escape(s); }

std::string encode_run_tool(const std::string& request_id,
                            const std::string& tool_id,
                            const std::vector<std::string>& args,
                            unsigned timeout_s) {
    std::string out =
        "{\"command\":\"toolbox_run_tool\",\"payload\":{\"request_id\":\"" +
        jescape(request_id) + "\",\"tool_id\":\"" + jescape(tool_id) +
        "\",\"source\":\"tool_window\",\"timeout_seconds\":" +
        std::to_string(timeout_s) + ",\"args\":[";
    for (size_t i = 0; i < args.size(); ++i) {
        if (i) out += ',';
        out += "\"" + jescape(args[i]) + "\"";
    }
    out += "]}}";
    return out;
}

std::string encode_cancel_run(const std::string& tool_id,
                              const std::string& request_id) {
    return "{\"command\":\"toolbox_cancel_tool_run\",\"payload\":{\"request_id\":\"" +
           jescape(request_id) + "\",\"tool_id\":\"" + jescape(tool_id) +
           "\",\"source\":\"tool_window\"}}";
}

std::string decode_event(const std::string& frame, JsonValue* payload) {
    JsonValue v;
    try {
        v = gptbridge::jsonlite::JsonParser(frame).parse();
    } catch (...) {
        return "";
    }
    const JsonValue* ev = jget(v, "event");
    if (!ev || ev->type != JsonValue::Type::String) return "";
    const JsonValue* pl = jget(v, "payload");
    *payload = pl ? *pl : JsonValue{};
    return ev->string;
}

const JsonValue* jget(const JsonValue& v, const char* key) {
    return v.get(key);
}

std::string jstr(const JsonValue& v, const char* key) {
    const JsonValue* e = jget(v, key);
    return (e && e->type == JsonValue::Type::String) ? e->string : "";
}

double jnum(const JsonValue& v, const char* key) {
    const JsonValue* e = jget(v, key);
    return (e && e->type == JsonValue::Type::Number) ? e->number : 0.0;
}

bool jbool(const JsonValue& v, const char* key, bool fallback) {
    const JsonValue* e = jget(v, key);
    return (e && e->type == JsonValue::Type::Bool) ? e->boolean : fallback;
}

} // namespace proto
} // namespace fsui

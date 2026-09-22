/* transport_proxy_client.cpp — star-governed-transport-proxy/v1 客戶端編解碼
 * 實作（零 I/O；spec: convergence/governed-transport-proxy-v1.md）。
 */
#include "transport_proxy_client.h"

namespace gptbridge {
namespace tpx {

using jsonlite::JsonError;
using jsonlite::JsonParser;
using jsonlite::JsonValue;
using jsonlite::json_escape;
using jsonlite::json_serialize;

namespace {

std::string q(const std::string& s) { return "\"" + json_escape(s) + "\""; }

void kv(std::vector<std::string>* out, const std::string& k,
        const std::string& raw_json) {
    out->push_back(q(k) + ":" + raw_json);
}

void kv_str(std::vector<std::string>* out, const std::string& k,
            const std::string& v) {
    kv(out, k, q(v));
}

std::string join_obj(const std::vector<std::string>& fields) {
    std::string out = "{";
    for (size_t i = 0; i < fields.size(); ++i) {
        if (i) out += ',';
        out += fields[i];
    }
    return out + '}';
}

const JsonValue* field(const JsonValue& v, const char* key) {
    return v.get(key);
}

std::string field_str(const JsonValue& v, const char* key, bool* found) {
    const JsonValue* f = field(v, key);
    if (f != nullptr && f->type == JsonValue::Type::String) {
        *found = true;
        return f->string;
    }
    *found = false;
    return std::string();
}

} // namespace

bool decode_response_line(const std::string& line, ProxyResponse* out) {
    *out = ProxyResponse{};
    JsonValue v;
    try {
        v = JsonParser(line).parse();
    } catch (const JsonError&) {
        return false;
    }
    if (v.type != JsonValue::Type::Object) return false;
    const JsonValue* ver = field(v, "v");
    if (ver == nullptr || ver->type != JsonValue::Type::Number ||
        ver->number != 1.0) {
        return false;
    }
    bool found = false;
    out->id = field_str(v, "id", &found);
    if (!found || out->id.empty()) return false;

    const JsonValue* ok = field(v, "ok");
    if (ok == nullptr || ok->type != JsonValue::Type::Bool) return false;
    out->ok = ok->boolean;

    if (out->ok) {
        const JsonValue* result = field(v, "result");
        if (result != nullptr) out->result = *result;
    } else {
        const JsonValue* err = field(v, "error");
        if (err == nullptr || err->type != JsonValue::Type::Object) {
            return false;
        }
        bool f2 = false;
        out->error_code = field_str(*err, "code", &f2);
        if (!f2 || out->error_code.empty()) return false;
        out->error_message = field_str(*err, "message", &f2);
    }
    out->valid = true;
    return true;
}

std::string encode_request(const std::string& id, const std::string& op,
                           const std::string& args_json) {
    std::vector<std::string> fields;
    kv(&fields, "v", "1");
    kv_str(&fields, "id", id);
    kv_str(&fields, "op", op);
    kv(&fields, "args", args_json.empty() ? "{}" : args_json);
    return join_obj(fields);
}

std::string args_empty() { return "{}"; }

std::string args_channel(const std::string& channel) {
    std::vector<std::string> f;
    kv_str(&f, "channel", channel);
    return join_obj(f);
}

std::string args_hello(const std::string& tool_id,
                       const std::string& workspace_instance_id,
                       const std::vector<HelloChannel>& channels,
                       const std::vector<HelloSubmit>& submit) {
    std::vector<std::string> f;
    kv_str(&f, "tool_id", tool_id);
    kv_str(&f, "workspace_instance_id", workspace_instance_id);

    std::string channels_obj = "{";
    for (size_t i = 0; i < channels.size(); ++i) {
        if (i) channels_obj += ',';
        channels_obj += q(channels[i].channel) + ":" + q(channels[i].mode);
    }
    channels_obj += '}';
    kv(&f, "channels", channels_obj);

    if (!submit.empty()) {
        std::string submit_obj = "{";
        for (size_t i = 0; i < submit.size(); ++i) {
            if (i) submit_obj += ',';
            std::vector<std::string> b;
            kv_str(&b, "actor", submit[i].actor);
            kv_str(&b, "authorizer", submit[i].authorizer);
            submit_obj += q(submit[i].channel) + ":" + join_obj(b);
        }
        submit_obj += '}';
        kv(&f, "submit", submit_obj);
    }
    return join_obj(f);
}

std::string args_respond(const std::string& channel,
                         const std::string& request_id,
                         const std::string& response_json) {
    std::vector<std::string> f;
    kv_str(&f, "channel", channel);
    kv_str(&f, "request_id", request_id);
    kv(&f, "response", response_json.empty() ? "null" : response_json);
    return join_obj(f);
}

std::string args_request_id(const std::string& channel,
                            const std::string& request_id) {
    std::vector<std::string> f;
    kv_str(&f, "channel", channel);
    kv_str(&f, "request_id", request_id);
    return join_obj(f);
}

std::string args_progress(const std::string& channel,
                          const std::string& request_id,
                          const std::string& payload_json) {
    std::vector<std::string> f;
    kv_str(&f, "channel", channel);
    kv_str(&f, "request_id", request_id);
    kv(&f, "payload", payload_json.empty() ? "null" : payload_json);
    return join_obj(f);
}

std::string args_ack(const std::string& channel,
                     const std::string& push_id,
                     const std::string& response_json) {
    std::vector<std::string> f;
    kv_str(&f, "channel", channel);
    kv_str(&f, "push_id", push_id);
    if (!response_json.empty()) kv(&f, "response", response_json);
    return join_obj(f);
}

std::string args_submit_request(const std::string& channel,
                                const std::string& target_tool_id,
                                const std::string& command,
                                const std::string& payload_json,
                                const std::string& request_id) {
    std::vector<std::string> f;
    kv_str(&f, "channel", channel);
    kv_str(&f, "target_tool_id", target_tool_id);
    kv_str(&f, "command", command);
    kv(&f, "payload", payload_json.empty() ? "{}" : payload_json);
    if (!request_id.empty()) kv_str(&f, "request_id", request_id);
    return join_obj(f);
}

std::string args_submit_response(const std::string& channel,
                                 const std::string& target_tool_id,
                                 const std::string& request_id) {
    std::vector<std::string> f;
    kv_str(&f, "channel", channel);
    kv_str(&f, "target_tool_id", target_tool_id);
    kv_str(&f, "request_id", request_id);
    return join_obj(f);
}

std::string args_submit_cancel(const std::string& channel,
                               const std::string& target_tool_id,
                               const std::string& request_id) {
    std::vector<std::string> f;
    kv_str(&f, "channel", channel);
    kv_str(&f, "target_tool_id", target_tool_id);
    kv_str(&f, "request_id", request_id);
    return join_obj(f);
}

std::string args_push(const std::string& channel,
                      const std::string& target_tool_id,
                      const std::string& command,
                      const std::string& payload_json,
                      const std::string& push_id) {
    std::vector<std::string> f;
    kv_str(&f, "channel", channel);
    kv_str(&f, "target_tool_id", target_tool_id);
    kv_str(&f, "command", command);
    kv(&f, "payload", payload_json.empty() ? "{}" : payload_json);
    if (!push_id.empty()) kv_str(&f, "push_id", push_id);
    return join_obj(f);
}

RequestWaiter::State RequestWaiter::feed(const JsonValue* state) {
    if (state_ != State::Pending) return state_;
    if (state == nullptr || state->type == JsonValue::Type::Null) {
        return state_;
    }
    bool found = false;
    const std::string status = field_str(*state, "status", &found);
    if (status == "completed") {
        const JsonValue* response = field(*state, "response");
        if (response != nullptr && response->type == JsonValue::Type::Object) {
            result_ = *response;
            /* request_sync parity: response.pop("request_id", None) */
            for (auto it = result_.object.begin(); it != result_.object.end(); ++it) {
                if (it->first == "request_id") {
                    result_.object.erase(it);
                    break;
                }
            }
            state_ = State::Completed;
        } else {
            /* request_sync raises permission_denied on non-dict response */
            error_code_ = "PERMISSION_DENIED";
            state_ = State::Failed;
        }
        return state_;
    }
    if (status == "cancelled") {
        error_code_ = "GOVERNED_REQUEST_CANCELLED";
        state_ = State::Cancelled;
        return state_;
    }
    return state_;
}

} // namespace tpx
} // namespace gptbridge

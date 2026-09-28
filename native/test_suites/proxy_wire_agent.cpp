/* star-governed-transport-proxy/v1 線協定測試 fixture（原生實作）。
 *
 * 取代已退役的 proxy_wire_agent.py：實作 TransportProxyAgent 的 stdio
 * JSONL dispatch（封包驗證、hello 綁定、channel-mode 強制、submit 授權、
 * 封閉錯誤碼映射），通道為 echo-recording 假通道，語義與原 fixture 相同，
 * 讓 C++ codec／tool_host 可以不通過 transport store 端到端驗證。
 *
 * 協定（與 governance_rule/execution/tool_runtime/transport_proxy.py 一致）：
 *   {"v":1,"id":"<id>","op":"<op>","args":{...}}
 *   -> {"v":1,"id":"<id>","ok":true,"result":{...}}
 *   -> {"v":1,"id":"<id>","ok":false,"error":{"code":"...","message":"..."}}
 *
 * 檔案佇列模式：GPTBRIDGE_WIRE_QUEUE 指向目錄時，request/claim/respond/
 * cancel 以 req-/claimed-/done-/cancelled-<id>.json 共享跨行程狀態（claim
 * 以 atomic rename 取列）。GPTBRIDGE_WIRE_REQUESTER_ACTOR 覆寫入列列的
 * requester_actor。
 */
#include "jsonlite.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <io.h>
#include <iostream>
#include <map>
#include <regex>
#include <sstream>
#include <string>
#include <windows.h>

namespace jl = gptbridge::jsonlite;
namespace fs = std::filesystem;

namespace {

const char* kAgentId = "star-governed-transport-proxy";
const size_t kMaxLineBytes = 2 * 1024 * 1024;
const std::regex kToolIdRe("[a-z0-9][a-z0-9_-]{1,63}");

jl::JsonValue jstr(const std::string& s) {
    jl::JsonValue v;
    v.type = jl::JsonValue::Type::String;
    v.string = s;
    return v;
}
jl::JsonValue jnum(double n) {
    jl::JsonValue v;
    v.type = jl::JsonValue::Type::Number;
    v.number = n;
    return v;
}
jl::JsonValue jbool(bool b) {
    jl::JsonValue v;
    v.type = jl::JsonValue::Type::Bool;
    v.boolean = b;
    return v;
}
jl::JsonValue jobj(
    std::initializer_list<std::pair<std::string, jl::JsonValue>> items) {
    jl::JsonValue v;
    v.type = jl::JsonValue::Type::Object;
    for (auto& kv : items) v.object.push_back(kv);
    return v;
}
jl::JsonValue jarr(std::initializer_list<jl::JsonValue> items) {
    jl::JsonValue v;
    v.type = jl::JsonValue::Type::Array;
    for (auto& it : items) v.array.push_back(it);
    return v;
}

std::string str_arg(const jl::JsonValue& args, const char* key) {
    const jl::JsonValue* v = args.get(key);
    if (v && v->type == jl::JsonValue::Type::String) return v->string;
    return "";
}

struct ProxyError {
    std::string code;
    std::string message;
};

/* 每次通道呼叫以一行 JSON 記錄輸出到 stderr（與原 fixture 相同）。 */
void emit_call(const std::string& name,
               std::initializer_list<jl::JsonValue> args) {
    jl::JsonValue rec = jobj({{"call", jobj({{"name", jstr(name)},
                                             {"args", jarr(args)}})}});
    std::cerr << jl::json_serialize(rec) << "\n";
    std::cerr.flush();
}

std::string env_str(const char* name) {
    const char* v = std::getenv(name);
    return v ? std::string(v) : std::string();
}

/* ---- 檔案佇列 ---------------------------------------------------- */

std::string sanitize_id(const std::string& id) {
    std::string out;
    out.reserve(id.size());
    for (char c : id) {
        const bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                        (c >= '0' && c <= '9') || c == '_' || c == '.' ||
                        c == '-';
        out += ok ? c : '_';
    }
    return out;
}

fs::path queue_path(const fs::path& dir, const std::string& rid,
                    const std::string& state) {
    return dir / (state + "-" + sanitize_id(rid) + ".json");
}

bool queue_find(const fs::path& dir, const std::string& rid,
                fs::path* path, std::string* state) {
    for (const char* s : {"req", "claimed", "done", "cancelled"}) {
        fs::path p = queue_path(dir, rid, s);
        std::error_code ec;
        if (fs::exists(p, ec)) {
            *path = p;
            *state = s;
            return true;
        }
    }
    return false;
}

bool queue_write(const fs::path& path, const jl::JsonValue& row) {
    fs::path tmp = path;
    tmp += ".tmp";
    {
        std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
        if (!out) return false;
        out << jl::json_serialize(row);
    }
    std::error_code ec;
    fs::rename(tmp, path, ec);
    if (!ec) return true;
    /* Windows rename 不覆蓋既有檔——退到原子替換。 */
    return MoveFileExA(tmp.string().c_str(), path.string().c_str(),
                       MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) != 0;
}

bool queue_read(const fs::path& path, jl::JsonValue* row) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return false;
    std::ostringstream ss;
    ss << in.rdbuf();
    try {
        *row = jl::JsonParser(ss.str()).parse();
        return row->type == jl::JsonValue::Type::Object;
    } catch (...) {
        return false;
    }
}

/* ---- 代理 --------------------------------------------------------- */

class Agent {
  public:
    Agent() : queue_dir_(env_str("GPTBRIDGE_WIRE_QUEUE")),
              requester_actor_(env_str("GPTBRIDGE_WIRE_REQUESTER_ACTOR")) {}

    /* 回傳空字串 = 丟棄不回應（連線保持）。 */
    std::string handle_line(const std::string& line) {
        if (line.size() > kMaxLineBytes) return "";
        jl::JsonValue msg;
        try {
            msg = jl::JsonParser(line).parse();
        } catch (...) {
            return "";
        }
        if (msg.type != jl::JsonValue::Type::Object) return "";
        const jl::JsonValue* v = msg.get("v");
        if (!v || v->type != jl::JsonValue::Type::Number || v->number != 1)
            return "";
        const std::string id = str_arg(msg, "id");
        const std::string op = str_arg(msg, "op");
        if (id.empty() || op.empty()) return "";
        const jl::JsonValue* args = msg.get("args");
        jl::JsonValue a;
        if (args && args->type != jl::JsonValue::Type::Object)
            return err(id, "BAD_ENVELOPE", "args must be an object");
        if (args) a = *args;
        try {
            return ok(id, dispatch(op, a));
        } catch (const ProxyError& e) {
            return err(id, e.code, e.message);
        } catch (const std::exception& e) {
            return err(id, "TRANSPORT_ERROR", std::string(e.what()).substr(0, 200));
        }
    }

  private:
    fs::path queue_dir_;
    std::string requester_actor_;
    std::string tool_id_;
    std::map<std::string, std::string> modes_;   /* channel -> process|submit */
    std::map<std::string, std::string> actors_;  /* submit channel -> actor */
    jl::JsonValue last_request_payload_;
    bool bound_ = false;

    std::string ok(const std::string& id, const jl::JsonValue& result) {
        return jl::json_serialize(
                   jobj({{"v", jnum(1)}, {"id", jstr(id)},
                         {"ok", jbool(true)}, {"result", result}})) +
               "\n";
    }
    std::string err(const std::string& id, const std::string& code,
                    const std::string& message) {
        return jl::json_serialize(
                   jobj({{"v", jnum(1)},
                         {"id", jstr(id)},
                         {"ok", jbool(false)},
                         {"error", jobj({{"code", jstr(code)},
                                         {"message", jstr(message)}})}})) +
               "\n";
    }
    [[noreturn]] void fail(const char* code, const std::string& msg) {
        throw ProxyError{code, msg};
    }
    void require_bound() {
        if (!bound_) fail("PERMISSION_DENIED", "hello required first");
    }
    std::string need_str(const jl::JsonValue& a, const char* key) {
        std::string s = str_arg(a, key);
        if (s.empty()) fail("BAD_ENVELOPE", std::string("missing/invalid arg: ") + key);
        return s;
    }
    void need_mode(const jl::JsonValue& a, const char* side) {
        const std::string ch = str_arg(a, "channel");
        auto it = modes_.find(ch);
        if (it == modes_.end() || it->second != side)
            fail("CHANNEL_NOT_BOUND",
                 ch.empty() ? "?" : ch + " is " +
                             (it == modes_.end() ? "unbound" : it->second) +
                             ", op needs " + side);
    }
    void authorize(const std::string& channel, const std::string& command) {
        if (actors_.find(channel) == actors_.end())
            fail("CHANNEL_NOT_BOUND", channel + " not submit-bound");
        emit_call("authorize", {jstr(actors_[channel]), jstr(""),
                                jstr(command)});
        if (command == "forbidden-command")
            fail("PERMISSION_DENIED", "PERMISSION_DENIED");
    }

    jl::JsonValue dispatch(const std::string& op, const jl::JsonValue& a) {
        if (op == "hello") return op_hello(a);
        if (op == "ping") return jobj({{"pong", jbool(true)}});
        require_bound();
        if (op == "claim") { need_mode(a, "process"); return op_claim(a); }
        if (op == "respond") { need_mode(a, "process"); return op_respond(a); }
        if (op == "request_cancelled") { need_mode(a, "process"); return op_request_cancelled(a); }
        if (op == "progress") { need_mode(a, "process"); return op_progress(a); }
        if (op == "notify_for_request") { need_mode(a, "process"); return op_notify(a); }
        if (op == "notification_stamp") { need_mode(a, "process"); return op_stamp(); }
        if (op == "claim_pushed") { need_mode(a, "process"); return op_claim_pushed(); }
        if (op == "acknowledge_push") { need_mode(a, "process"); return op_ack_push(a); }
        if (op == "request") { need_mode(a, "submit"); return op_request(a); }
        if (op == "response") { need_mode(a, "submit"); return op_response(a); }
        if (op == "cancel") { need_mode(a, "submit"); return op_cancel(a); }
        if (op == "push") { need_mode(a, "submit"); return op_push(a); }
        fail("BAD_ENVELOPE", "unknown op: " + op);
    }

    /* -- hello ------------------------------------------------------ */
    jl::JsonValue op_hello(const jl::JsonValue& a) {
        if (bound_) fail("PERMISSION_DENIED", "already bound");
        const std::string tid = str_arg(a, "tool_id");
        if (tid.empty() || !std::regex_match(tid, kToolIdRe) ||
            tid == "main-system")
            fail("PERMISSION_DENIED", "invalid tool_id");
        if (str_arg(a, "workspace_instance_id").empty())
            fail("PERMISSION_DENIED", "workspace_instance_id required");
        const jl::JsonValue* channels = a.get("channels");
        if (!channels || channels->type != jl::JsonValue::Type::Object ||
            channels->object.empty())
            fail("BAD_ENVELOPE", "channels must be a non-empty object");
        const jl::JsonValue* submit = a.get("submit");

        std::map<std::string, std::string> modes;
        std::map<std::string, std::string> actors;
        for (const auto& kv : channels->object) {
            std::string ch = kv.first, mode;
            for (auto& c : ch) c = static_cast<char>(std::tolower(c));
            if (kv.second.type == jl::JsonValue::Type::String)
                mode = kv.second.string;
            for (auto& c : mode) c = static_cast<char>(std::tolower(c));
            if ((ch != "system" && ch != "ai") ||
                (mode != "process" && mode != "submit"))
                fail("PERMISSION_DENIED",
                     "invalid channel/mode: " + kv.first);
            modes[ch] = mode;
            if (mode == "submit") {
                const jl::JsonValue* binding =
                    (submit && submit->type == jl::JsonValue::Type::Object)
                        ? submit->get(ch)
                        : nullptr;
                if (!binding)
                    fail("PERMISSION_DENIED",
                         "submit channel " + ch + " requires submit binding");
                const std::string actor = str_arg(*binding, "actor");
                if (actor.empty())
                    fail("PERMISSION_DENIED",
                         "submit." + ch + ".actor required");
                actors[ch] = actor;
            }
        }
        tool_id_ = tid;
        modes_ = modes;
        actors_ = actors;
        bound_ = true;

        jl::JsonValue modes_out;
        modes_out.type = jl::JsonValue::Type::Object;
        for (const auto& kv : modes_) modes_out.object.push_back({kv.first, jstr(kv.second)});
        return jobj({{"agent", jstr(kAgentId)},
                     {"v", jnum(1)},
                     {"channels", modes_out}});
    }

    /* -- process 側 -------------------------------------------------- */
    jl::JsonValue op_claim(const jl::JsonValue&) {
        emit_call("claim", {});
        if (queue_dir_.empty())
            return jobj({{"request",
                          jobj({{"request_id", jstr("req-77")},
                                {"command", jstr("diag.run")},
                                {"payload", jobj({{"k", jnum(1)}})}})}});
        jl::JsonValue req;
        req.type = jl::JsonValue::Type::Null;
        std::vector<fs::path> entries;
        std::error_code ec;
        for (auto& e : fs::directory_iterator(queue_dir_, ec)) {
            const std::string name = e.path().filename().string();
            if (name.rfind("req-", 0) == 0 &&
                e.path().extension() == ".json")
                entries.push_back(e.path());
        }
        std::sort(entries.begin(), entries.end());
        for (const auto& req_path : entries) {
            jl::JsonValue row;
            if (!queue_read(req_path, &row)) continue;
            if (str_arg(row, "target_tool_id") != tool_id_) continue;
            fs::path claimed = queue_path(queue_dir_, str_arg(row, "request_id"), "claimed");
            if (!MoveFileExA(req_path.string().c_str(),
                             claimed.string().c_str(), 0))
                continue; /* 已被其他 worker 取走 */
            bool status_set = false;
            for (auto& kv : row.object)
                if (kv.first == "status") {
                    kv.second = jstr("claimed");
                    status_set = true;
                }
            if (!status_set)
                row.object.push_back({"status", jstr("claimed")});
            const jl::JsonValue* ac = row.get("attempt_count");
            const double n = (ac && ac->type == jl::JsonValue::Type::Number)
                                 ? ac->number + 1
                                 : 1;
            /* 更新既有欄位值。 */
            for (auto& kv : row.object)
                if (kv.first == "attempt_count") kv.second = jnum(n);
            if (!row.get("attempt_count"))
                row.object.push_back({"attempt_count", jnum(n)});
            queue_write(claimed, row);
            req = jobj({{"request_id", jstr(str_arg(row, "request_id"))},
                        {"requester_actor", jstr(str_arg(row, "requester_actor"))},
                        {"target_tool_id", jstr(str_arg(row, "target_tool_id"))},
                        {"payload", row.get("payload") ? *row.get("payload")
                                                      : jl::JsonValue{}},
                        {"lease_until", row.get("lease_until")
                                            ? *row.get("lease_until")
                                            : jl::JsonValue{}},
                        {"attempt_count", jnum(n)}});
            break;
        }
        return jobj({{"request", req}});
    }

    jl::JsonValue op_respond(const jl::JsonValue& a) {
        const std::string rid = need_str(a, "request_id");
        const jl::JsonValue* response = a.get("response");
        emit_call("respond", {jstr(rid), response ? *response : jl::JsonValue{}});
        if (!queue_dir_.empty()) {
            fs::path p;
            std::string state;
            if (!queue_find(queue_dir_, rid, &p, &state) || state != "claimed")
                return jbool(false);
            jl::JsonValue row;
            if (!queue_read(p, &row))
                row = jobj({{"request_id", jstr(rid)}});
            for (auto& kv : row.object)
                if (kv.first == "status") kv.second = jstr("completed");
            row.object.push_back(
                {"response", response ? *response : jl::JsonValue{}});
            queue_write(queue_path(queue_dir_, rid, "done"), row);
            std::error_code ec;
            fs::remove(p, ec);
        }
        return jbool(true);
    }

    jl::JsonValue op_request_cancelled(const jl::JsonValue& a) {
        const std::string rid = need_str(a, "request_id");
        emit_call("request_cancelled", {jstr(rid)});
        if (!queue_dir_.empty()) {
            std::error_code ec;
            return jbool(fs::exists(queue_path(queue_dir_, rid, "cancelled"), ec));
        }
        return jbool(true);
    }

    jl::JsonValue op_progress(const jl::JsonValue& a) {
        emit_call("progress", {jstr(str_arg(a, "request_id")),
                               a.get("payload") ? *a.get("payload")
                                                : jl::JsonValue{}});
        return jbool(true);
    }

    jl::JsonValue op_notify(const jl::JsonValue& a) {
        emit_call("notify_for_request", {jstr(str_arg(a, "request_id"))});
        return jl::JsonValue{};
    }

    jl::JsonValue op_stamp() {
        emit_call("notification_stamp", {});
        return jarr({jnum(7), jnum(3)});
    }

    jl::JsonValue op_claim_pushed() {
        emit_call("claim_pushed", {});
        return jobj({{"push",
                      jobj({{"push_id", jstr("p-1")},
                            {"payload", jobj({{"note", jstr("hi")}})}})}});
    }

    jl::JsonValue op_ack_push(const jl::JsonValue& a) {
        emit_call("acknowledge_push", {jstr(str_arg(a, "push_id")),
                                       a.get("response")
                                           ? *a.get("response")
                                           : jl::JsonValue{}});
        return jbool(true);
    }

    /* -- submit 側 --------------------------------------------------- */
    jl::JsonValue op_request(const jl::JsonValue& a) {
        const std::string channel = str_arg(a, "channel");
        const std::string target = need_str(a, "target_tool_id");
        const std::string command = need_str(a, "command");
        const jl::JsonValue* payload = a.get("payload");
        if (!payload || payload->type != jl::JsonValue::Type::Object)
            fail("BAD_ENVELOPE", "payload must be an object");
        authorize(channel, command);
        std::string rid = str_arg(a, "request_id");
        if (rid.empty()) rid = "request-native";
        emit_call("request", {jstr(target), jstr(rid), *payload});
        jl::JsonValue stored = *payload;
        stored.object.push_back({"_governed_command", jstr(command)});
        last_request_payload_ = stored;
        if (!queue_dir_.empty()) {
            fs::create_directories(queue_dir_);
            queue_write(queue_path(queue_dir_, rid, "req"),
                        jobj({{"request_id", jstr(rid)},
                              {"requester_actor",
                               jstr(requester_actor_.empty()
                                        ? "governance/tool/" + tool_id_
                                        : requester_actor_)},
                              {"target_tool_id", jstr(target)},
                              {"payload", stored},
                              {"status", jstr("queued")},
                              {"lease_until", jl::JsonValue{}},
                              {"attempt_count", jnum(0)}}));
        }
        return jobj({{"request_id", jstr(rid)},
                     {"queued", jbool(true)}});
    }

    jl::JsonValue op_response(const jl::JsonValue& a) {
        const std::string rid = need_str(a, "request_id");
        emit_call("response",
                  {jstr(str_arg(a, "target_tool_id")), jstr(rid)});
        if (!queue_dir_.empty()) {
            fs::path done = queue_path(queue_dir_, rid, "done");
            jl::JsonValue row;
            std::error_code ec;
            if (fs::exists(done, ec) && queue_read(done, &row))
                return jobj({{"status", jstr("completed")},
                             {"request_id", jstr(rid)},
                             {"response", row.get("response")
                                              ? *row.get("response")
                                              : jl::JsonValue{}}});
            return jobj({{"status", jstr("pending")},
                         {"request_id", jstr(rid)}});
        }
        return jobj({{"status", jstr("completed")},
                     {"request_id", jstr(rid)},
                     {"response",
                      jobj({{"echo", last_request_payload_.type ==
                                                  jl::JsonValue::Type::Null
                                              ? jl::JsonValue{}
                                              : last_request_payload_}})}});
    }

    jl::JsonValue op_cancel(const jl::JsonValue& a) {
        const std::string rid = need_str(a, "request_id");
        emit_call("cancel",
                  {jstr(str_arg(a, "target_tool_id")), jstr(rid)});
        if (!queue_dir_.empty()) {
            fs::path p;
            std::string state;
            if (!queue_find(queue_dir_, rid, &p, &state) ||
                state == "done" || state == "cancelled")
                return jbool(false);
            jl::JsonValue row;
            if (!queue_read(p, &row))
                row = jobj({{"request_id", jstr(rid)}});
            for (auto& kv : row.object)
                if (kv.first == "status") kv.second = jstr("cancelled");
            queue_write(queue_path(queue_dir_, rid, "cancelled"), row);
            if (state == "req") {
                std::error_code ec;
                fs::remove(p, ec);
            }
        }
        return jbool(true);
    }

    jl::JsonValue op_push(const jl::JsonValue& a) {
        const std::string channel = str_arg(a, "channel");
        const std::string target = need_str(a, "target_tool_id");
        const std::string command = need_str(a, "command");
        const jl::JsonValue* payload = a.get("payload");
        if (!payload || payload->type != jl::JsonValue::Type::Object)
            fail("BAD_ENVELOPE", "payload must be an object");
        authorize(channel, command);
        std::string pid = str_arg(a, "push_id");
        if (pid.empty()) pid = "push-native";
        emit_call("push", {jstr(target), jstr(pid), *payload});
        return jobj({{"push_id", jstr(pid)}});
    }
};

}  // namespace

int main() {
    /* stdio JSONL：每行一封包直到 EOF（與 transport_proxy.main 同構）。 */
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
    Agent agent;
    std::string line;
    while (std::getline(std::cin, line)) {
        while (!line.empty() &&
               (line.back() == '\r' || line.back() == '\n'))
            line.pop_back();
        if (line.empty()) continue;
        const std::string out = agent.handle_line(line);
        if (!out.empty()) {
            std::cout << out;
            std::cout.flush();
        }
        if (std::cin.eof()) break;
    }
    return 0;
}

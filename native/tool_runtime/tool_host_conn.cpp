/* tool_host_conn.cpp — 連線協議層：conn_loop HTTP 閘門路由、
 * ws_loop WS 帧重組、§3 WS 命令受理（request/cancel→submit 代理）。
 * 由 conn_worker 固定池執行緒驅動（bounded-concurrency/v1）；Impl
 * 與線上助手見 tool_host_internal.h。非 Windows → 空 TU（入口
 * stub 在 tool_host.cpp）。
 */
#include "tool_host_internal.h"

#ifdef _WIN32

namespace gptbridge {
namespace toolhost {

using namespace detail;

void ToolHost::conn_loop(intptr_t sock) {
    const SOCKET c = static_cast<SOCKET>(sock);
    /* 計數已於 conn_worker 入帳；此處只負責離開時銷帳。 */
    struct ConnGuard {
        std::atomic<int>& n;
        ~ConnGuard() { n.fetch_sub(1); }
    } guard{impl_->active_conns};
    std::string buf;
    buf.reserve(8192);
    char tmp[8192];
    gtw::HttpRequest req;
    size_t consumed = 0;
    bool parsed = false;
    while (!impl_->stop_flag.load()) {
        const int n = recv(c, tmp, sizeof(tmp), 0);
        if (n <= 0) goto done;
        buf.append(tmp, static_cast<size_t>(n));
        if (buf.size() > 16 * 1024) goto done;
        if (buf.find("\r\n\r\n") != std::string::npos) {
            parsed = gtw::http_request_parse(
                reinterpret_cast<const uint8_t*>(buf.data()), buf.size(),
                &req, &consumed);
            break;
        }
    }
    if (!parsed) goto done;

    switch (gtw::route_request(req, impl_->cfg.session_token,
                               impl_->cfg.workspace_instance_id,
                               impl_->cfg.shutdown_token)) {
    case gtw::GateDecision::Health:
        send_all(c, gtw::http_ok_bytes(
                        jl::json_serialize(health_snapshot()),
                        "application/json"));
        break;
    case gtw::GateDecision::Metrics:
        send_all(c, gtw::http_ok_bytes(
                        jl::json_serialize(metrics_snapshot()),
                        "application/json"));
        break;
    case gtw::GateDecision::Shutdown:
        send_all(c, gtw::http_ok_bytes("OK", "text/plain"));
        impl_->stop_flag.store(true);
        /* 同 request_stop：關 listen socket 喚醒阻塞中的 accept()，
           否則 run() 的 join 會卡死。 */
        closesocket(impl_->listen_sock);
        break;
    case gtw::GateDecision::Upgrade: {
        std::string accept_key;
        if (!gtw::ws_validate_upgrade(req, &accept_key)) {
            send_all(c, gtw::http_forbidden_bytes());
            break;
        }
        const char* key = req.header("sec-websocket-key");
        if (!send_all(c, gtw::ws_upgrade_response(key ? key : ""))) break;
        impl_->n_upgrades.fetch_add(1);
        ws_loop(c, buf.substr(consumed));
        break;
    }
    case gtw::GateDecision::Forbidden:
    default:
        send_all(c, gtw::http_forbidden_bytes());
        break;
    }
done:
    {
        std::lock_guard<std::mutex> lk(impl_->conn_mu);
        impl_->conns.erase(c);
    }
    closesocket(c);
}

void ToolHost::ws_loop(intptr_t sock, std::string pending) {
    const SOCKET c = static_cast<SOCKET>(sock);
    std::string buf = std::move(pending);
    std::string message; /* text/binary + continuation 重組（≤1MiB） */
    bool in_message = false;
    char tmp[8192];
    while (!impl_->stop_flag.load()) {
        gtw::WsFrame f;
        const int64_t used = gtw::ws_frame_decode(
            reinterpret_cast<const uint8_t*>(buf.data()), buf.size(), &f);
        if (used < 0) break;
        if (used > 0) {
            buf.erase(0, static_cast<size_t>(used));
            if (f.opcode == gtw::WsOp::Ping) {
                std::lock_guard<std::mutex> lk(impl_->send_mu);
                if (!send_all(c, gtw::ws_pong(f.payload))) break;
            } else if (f.opcode == gtw::WsOp::Close) {
                {
                    std::lock_guard<std::mutex> lk(impl_->send_mu);
                    send_all(c, gtw::ws_close(1000, ""));
                }
                break;
            } else if (f.opcode == gtw::WsOp::Text ||
                       f.opcode == gtw::WsOp::Binary ||
                       f.opcode == gtw::WsOp::Continuation) {
                /* Python websockets 以「訊息」為單位交付：首幀
                   text/binary＋continuation 至 FIN；binary 同樣餵
                   json.loads（bytes 可解析）。孤立 continuation 或
                   訊息途中新 data frame → 協定錯關閉。 */
                if (f.opcode == gtw::WsOp::Continuation) {
                    if (!in_message) break;
                } else {
                    if (in_message) break;
                    in_message = true;
                }
                message.append(f.payload);
                if (message.size() > 1024 * 1024) break;
                if (f.fin) {
                    handle_ws_message(c, message);
                    message.clear();
                    in_message = false;
                }
            }
            continue;
        }
        const int n = recv(c, tmp, sizeof(tmp), 0);
        if (n <= 0) break;
        buf.append(tmp, static_cast<size_t>(n));
    }
    /* 連線結束：移除指向本 socket 的 waiters（不回推結果）。 */
    std::lock_guard<std::mutex> lk(impl_->waiter_mu);
    for (auto it = impl_->waiters.begin(); it != impl_->waiters.end();) {
        it = (it->second == c) ? impl_->waiters.erase(it) : std::next(it);
    }
}

/* ---- WS 命令（§3） ---- */

void ToolHost::send_event(intptr_t sock, const std::string& event,
                          const jl::JsonValue& payload) {
    std::lock_guard<std::mutex> lk(impl_->send_mu);
    send_all(sock, http_event_frame(event, payload));
}

void ToolHost::send_result_error(intptr_t sock, const std::string& command,
                                 const std::string& request_id,
                                 const std::string& code) {
    /* Python：event = f"{command}_result" if command else "error"。 */
    const std::string event =
        command.empty() ? "error" : command + "_result";
    send_event(sock, event,
               jobj({{"ok", jbool(false)},
                     {"tool_id", jstr(impl_->cfg.tool_id)},
                     {"request_id", jstr(request_id)},
                     {"error_code", jstr(code)},
                     {"message", jstr(code)}}));
}

void ToolHost::handle_ws_message(intptr_t sock, const std::string& text) {
    const SOCKET c = static_cast<SOCKET>(sock);
    impl_->n_commands.fetch_add(1);
    bool ok = false;
    const jl::JsonValue msg = jparse(text, &ok);
    const jl::JsonValue* cmd =
        (ok && msg.type == jl::JsonValue::Type::Object)
            ? get_str(msg, "command")
            : nullptr;
    const std::string command = cmd ? cmd->string : "";
    const jl::JsonValue* payload =
        (ok && msg.type == jl::JsonValue::Type::Object)
            ? msg.get("payload")
            : nullptr;
    const std::string request_id = [&] {
        const jl::JsonValue* r =
            (payload && payload->type == jl::JsonValue::Type::Object)
                ? get_str(*payload, "request_id")
                : nullptr;
        return r ? r->string : std::string();
    }();

    /* Python：非 dict 訊息／空 command／payload 非 dict → except →
       "{command}_result"（或 "error"）PERMISSION_DENIED。 */
    if (!ok || msg.type != jl::JsonValue::Type::Object ||
        command.empty() || payload == nullptr ||
        payload->type != jl::JsonValue::Type::Object) {
        impl_->n_denied.fetch_add(1);
        send_result_error(c, command, request_id, "PERMISSION_DENIED");
        return;
    }

    /* §3.1 toolbox_cancel_tool_run：submit 側 cancel 經代理。 */
    if (command == "toolbox_cancel_tool_run") {
        tpx::ProxyResponse resp;
        bool cancelled = false;
        const std::string& ch = impl_->cfg.process_channels.front();
        if (!request_id.empty() &&
            impl_->proxy_submit(
                "cancel",
                tpx::args_submit_cancel(ch, impl_->cfg.tool_id,
                                        request_id),
                &resp) &&
            resp.ok &&
            resp.result.type == jl::JsonValue::Type::Bool) {
            cancelled = resp.result.boolean;
        }
        /* 本地執行中 request：立取消旗標＋工具自備取消。 */
        std::shared_ptr<std::atomic<bool>> flag;
        {
            std::lock_guard<std::mutex> lk(impl_->exec_mu);
            auto it = impl_->exec_flags.find(request_id);
            if (it != impl_->exec_flags.end()) flag = it->second;
        }
        if (flag) {
            flag->store(true);
            if (impl_->hooks.cancellation)
                impl_->hooks.cancellation(request_id);
            cancelled = true;
        }
        /* Python：{"ok": cancelled, "cancelled": cancelled, …} */
        send_event(c, "toolbox_cancel_tool_run_result",
                   jobj({{"ok", jbool(cancelled)},
                         {"cancelled", jbool(cancelled)},
                         {"tool_id", jstr(impl_->cfg.tool_id)},
                         {"request_id", jstr(request_id)}}));
        return;
    }

    /* §3.1 前置校驗 → PERMISSION_DENIED 形式。
       payload.tool_id 的 Python `or` 語義：falsy（null/false/0/""/
       []/{}）→ self；truthy 非字串或字串 ≠ tool_id → deny。 */
    const jl::JsonValue* tid = payload->get("tool_id");
    bool tid_deny = false;
    if (tid != nullptr) {
        switch (tid->type) {
            case jl::JsonValue::Type::Null: break;
            case jl::JsonValue::Type::Bool:
                tid_deny = tid->boolean; break;
            case jl::JsonValue::Type::Number:
                tid_deny = tid->number != 0; break;
            case jl::JsonValue::Type::String:
                tid_deny =
                    !tid->string.empty() &&
                    tid->string != impl_->cfg.tool_id;
                break;
            case jl::JsonValue::Type::Array:
                tid_deny = !tid->array.empty(); break;
            case jl::JsonValue::Type::Object:
                tid_deny = !tid->object.empty(); break;
        }
    }
    const bool valid =
        !tid_deny &&
        gptbridge_gt_request_valid(
            command.c_str(), 1, request_id.c_str(),
            tid && tid->type == jl::JsonValue::Type::String
                ? tid->string.c_str() : nullptr,
            impl_->cfg.tool_id.c_str()) != 0;
    if (!valid) {
        impl_->n_denied.fetch_add(1);
        send_result_error(c, command, request_id, "PERMISSION_DENIED");
        return;
    }

    /* §3.2 受理：waiter 先登記（Python：waiters[rid]=ws 在 request
       之前）→ submit 入傳輸 → COMMAND_RECEIVED；submit 失敗 → 撤
       waiter＋PERMISSION_DENIED（Python except 統一 DENIED）。 */
    {
        std::lock_guard<std::mutex> lk(impl_->waiter_mu);
        impl_->waiters[request_id] = c;
    }
    tpx::ProxyResponse resp;
    const std::string& ch = impl_->cfg.process_channels.front();
    if (!impl_->proxy_submit(
            "request",
            tpx::args_submit_request(ch, impl_->cfg.tool_id, command,
                                     jl::json_serialize(*payload),
                                     request_id),
            &resp) ||
        !resp.ok) {
        {
            std::lock_guard<std::mutex> lk(impl_->waiter_mu);
            impl_->waiters.erase(request_id);
        }
        impl_->n_denied.fetch_add(1);
        send_result_error(c, command, request_id, "PERMISSION_DENIED");
        return;
    }
    send_event(c, "COMMAND_RECEIVED",
               jobj({{"command", jstr(command)},
                     {"status", jstr("processing")}}));
}

} // namespace toolhost
} // namespace gptbridge

#endif /* _WIN32 */

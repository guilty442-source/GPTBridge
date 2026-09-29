// proxy_wire_agent Agent submit-side ops (unity-included inside class Agent)

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

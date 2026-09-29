// tool_host suite cases group c (unity-included by suite_tool_host.cpp)

void run_cases_c() {
    /* live P2 sidecar：spawn 原生 proxy_wire_agent.exe fixture，
       驗證 CreateProcess 管道＋JSONL codec＋代理 dispatch 端到端。
       fixture 缺失 → BLOCKED（證據不完整，非 PASS）。
       手動 record（非 NT_TEST）：BLOCKED 與 PASS/FAIL 只能記一筆。 */
    {
        const char* name = "live_p2_sidecar_smoke";
        const double t0 = native_tests::now_ms();
        std::string detail;
        bool blocked = false;
        const bool pass = [&]() -> bool {
            auto check = [&](bool cond, const char* msg) -> bool {
                if (!cond) detail = msg;
                return cond;
            };
            /* fixture 與套件 exe 同目錄（native/test_suites/bin）。 */
            char exe_buf[MAX_PATH] = {0};
            GetModuleFileNameA(nullptr, exe_buf, MAX_PATH);
            std::string fixture = exe_buf;
            const size_t slash = fixture.find_last_of("\\/");
            fixture = (slash == std::string::npos)
                          ? "proxy_wire_agent.exe"
                          : fixture.substr(0, slash + 1) +
                                "proxy_wire_agent.exe";
            if (GetFileAttributesA(fixture.c_str()) ==
                INVALID_FILE_ATTRIBUTES) {
                detail = "proxy_wire_agent.exe missing";
                blocked = true;
                return true;
            }

            tpx::ProxySidecar sidecar;
            tpx::SidecarError err;
            tpx::ProxyResponse resp;
            if (!sidecar.start("\"" + fixture + "\"", &err)) {
                detail = "fixture spawn failed";
                blocked = true;
                return true;
            }
            if (!check(sidecar.call("ping", tpx::args_empty(), &resp,
                                    &err) &&
                           resp.ok,
                       "ping transport ok"))
                return false;
            if (!check(resp.valid, "ping response decoded")) return false;
            const jl::JsonValue* pong = resp.result.get("pong");
            if (!check(pong && pong->type == jl::JsonValue::Type::Bool &&
                           pong->boolean,
                       "ping pong true"))
                return false;

            /* hello 前的 process op → PERMISSION_DENIED（協定層）。 */
            if (!check(sidecar.call("claim", tpx::args_channel("system"),
                                    &resp, &err),
                       "pre-hello claim transport ok"))
                return false;
            if (!check(!resp.ok &&
                           resp.error_code == "PERMISSION_DENIED",
                       "pre-hello claim denied"))
                return false;

            /* fixture hello 無 env 閘 → 綁定成功，channels 回顯。 */
            if (!check(sidecar.call(
                           "hello",
                           tpx::args_hello(
                               "test-tool", "ws-instance-1",
                               {tpx::HelloChannel{"system", "process"}},
                               {}),
                           &resp, &err),
                       "hello transport ok"))
                return false;
            if (!check(resp.ok, "fixture hello bound")) return false;
            const jl::JsonValue* chans = resp.result.get("channels");
            if (!check(chans &&
                           str_val(chans->get("system")) == "process",
                       "hello channels echoed"))
                return false;

            /* 綁定後 claim → fixture canned 列（無佇列 env）。 */
            if (!check(sidecar.call("claim", tpx::args_channel("system"),
                                    &resp, &err) &&
                           resp.ok,
                       "post-hello claim ok"))
                return false;
            const jl::JsonValue* req = resp.result.get("request");
            if (!check(req && str_val(req->get("request_id")) ==
                                  "req-77",
                       "claim returns canned req-77"))
                return false;

            /* 未知 op → BAD_ENVELOPE，錯誤不殺連線 → ping 再通。 */
            if (!check(sidecar.call("bogus_op", tpx::args_empty(),
                                    &resp, &err) &&
                           !resp.ok &&
                           resp.error_code == "BAD_ENVELOPE",
                       "unknown op -> BAD_ENVELOPE"))
                return false;
            if (!check(
                    sidecar.call("ping", tpx::args_empty(), &resp,
                                 &err) &&
                        resp.ok,
                    "sidecar survives error envelope"))
                return false;

            sidecar.stop();
            return check(
                !sidecar.call("ping", tpx::args_empty(), &resp, &err) &&
                    !err.code.empty(),
                "post-stop call fails with error code");
        }();
        if (blocked)
            native_tests::record_blocked(SUITE, name, detail);
        else
            native_tests::record(SUITE, name, pass, detail,
                                 native_tests::now_ms() - t0);
    }

    /* live e2e：tool_host 以真實 ProxySidecar spawn 兩支原生
       proxy_wire_agent.exe（process＋submit 綁定；檔案佇列共享狀態），
       WS 命令走 transport-proxy/v1 dispatch 全程——
       request→佇列→claim→execute→respond→waiter 推送＋
       cancel→request_cancelled→不 respond。fixture 缺失 → BLOCKED。 */
    {
        const char* name = "live_sidecar_e2e";
        const double t0 = native_tests::now_ms();
        std::string detail;
        bool blocked = false;
        const bool pass = [&]() -> bool {
            auto check = [&](bool cond, const char* msg) -> bool {
                if (!cond) detail = msg;
                return cond;
            };
            namespace fs = std::filesystem;
            char cwd[MAX_PATH] = {0};
            GetCurrentDirectoryA(MAX_PATH, cwd);
            char root_buf[MAX_PATH] = {0};
            const char* env_root = std::getenv("GPTBRIDGE_PROJECT_ROOT");
            if (env_root && env_root[0]) {
                strncpy_s(root_buf, env_root, MAX_PATH - 1);
            } else {
                GetFullPathNameA(
                    (std::string(cwd) + "\\..\\..\\..").c_str(),
                    MAX_PATH, root_buf, nullptr);
            }
            const fs::path root = root_buf;
            /* 原生線協定 fixture：與套件 exe 同目錄（bin/）。 */
            char exe_buf[MAX_PATH] = {0};
            GetModuleFileNameA(nullptr, exe_buf, MAX_PATH);
            std::string agent = exe_buf;
            const size_t slash = agent.find_last_of("\\/");
            agent = (slash == std::string::npos)
                        ? "proxy_wire_agent.exe"
                        : agent.substr(0, slash + 1) +
                              "proxy_wire_agent.exe";
            if (GetFileAttributesA(agent.c_str()) ==
                INVALID_FILE_ATTRIBUTES) {
                detail = "proxy_wire_agent.exe missing";
                blocked = true;
                return true;
            }
            const fs::path queue_dir =
                root / "native" / "test_suites" / "bin" /
                ("_wire_queue_" +
                 std::to_string(GetCurrentProcessId()));
            fs::create_directories(queue_dir);
            SetEnvironmentVariableA(
                "GPTBRIDGE_WIRE_QUEUE",
                queue_dir.generic_string().c_str());

            WSADATA wsa;
            if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0)
                return check(false, "wsa");
            const int port = free_port();

            th::ToolHostConfig cfg;
            cfg.tool_id = "test-tool";
            cfg.port = port;
            cfg.session_token = TOKEN;
            cfg.env_gate_passed = true;  /* 測試直構 config：顯式標記已過 env 閘 */
            cfg.env_gate_proof = th::ToolHost::issue_test_gate_proof();
            cfg.shutdown_token = "sh-token";
            char wsid[GPTBRIDGE_GT_INSTANCE_ID_LEN + 1] = {0};
            gptbridge_gt_workspace_instance_id("test-tool", port, wsid);
            cfg.workspace_instance_id = wsid;
            const std::string cmdline = "\"" + agent + "\"";
            cfg.proxy_command_line = cmdline;
            cfg.proxy_command_line_submit = cmdline;
            cfg.submit_actor = "governance/tool/test-tool";
            cfg.submit_authorizer =
                "governance_rule.permission_directory.registries."
                "permissions.tool_routes:authorize_tool_self_route";

            std::atomic<bool> slow_started{false};
            th::ToolHostHooks hooks;
            hooks.executor = [&](const std::string& command,
                                 const jl::JsonValue& payload,
                                 const std::string&,
                                 const std::atomic<bool>& cancelled) {
                if (command == "slow") {
                    slow_started.store(true);
                    for (int i = 0; i < 800 && !cancelled.load(); ++i)
                        Sleep(10);
                    return jobj({{"ok", jbool(true)}});
                }
                return jobj({{"ok", jbool(true)},
                             {"echo_command", jstr(command)},
                             {"echo_x", payload.get("x")
                                             ? *payload.get("x")
                                             : jl::JsonValue{}}});
            };

            th::ToolHost host;
            std::string err;
            if (!host.start(cfg, std::move(hooks), &err))
                return check(false, ("start: " + err).c_str());

            SOCKET ws = connect_loop(port);
            const std::string wsreq =
                "GET /?token=" + TOKEN + "&instance=" +
                std::string(wsid) +
                " HTTP/1.1\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n";
            send(ws, wsreq.data(), static_cast<int>(wsreq.size()), 0);
            char rbuf[512];
            int rn = recv(ws, rbuf, sizeof(rbuf) - 1, 0);
            rbuf[rn > 0 ? rn : 0] = '\0';
            if (!check(std::string(rbuf).find("101") !=
                           std::string::npos,
                       "ws upgrade 101 (live e2e)"))
                return false;

            const std::string cmd = jl::json_serialize(jobj({
                {"command", jstr("echo")},
                {"payload", jobj({{"request_id", jstr("r-live-1")},
                                  {"tool_id", jstr("test-tool")},
                                  {"x", jstr("v")}})},
            }));
            send(ws, masked_text(cmd).data(),
                 static_cast<int>(masked_text(cmd).size()), 0);
            gtw::WsFrame f;

            if (!check(read_frame(ws, &f, 15000) &&
                           f.payload.find("COMMAND_RECEIVED") !=
                               std::string::npos,
                       "recv COMMAND_RECEIVED (live e2e)"))
                return false;

            if (!check(read_frame(ws, &f, 15000) &&
                           f.payload.find("echo_result") !=
                               std::string::npos &&
                           f.payload.find("r-live-1") !=
                               std::string::npos,
                       "recv echo_result (live e2e)"))
                return false;
            bool done_seen = false;
            for (int i = 0; i < 100 && !done_seen; ++i) {
                done_seen =
                    fs::exists(queue_dir / "done-r-live-1.json");
                if (!done_seen) Sleep(50);
            }
            if (!check(done_seen, "respond persisted (live e2e)"))
                return false;

            const std::string slow = jl::json_serialize(jobj({
                {"command", jstr("slow")},
                {"payload", jobj({{"request_id", jstr("r-live-2")},
                                  {"tool_id", jstr("test-tool")}})},
            }));
            send(ws, masked_text(slow).data(),
                 static_cast<int>(masked_text(slow).size()), 0);
            if (!check(read_frame(ws, &f, 15000) &&
                           f.payload.find("COMMAND_RECEIVED") !=
                               std::string::npos,
                       "recv slow received (live e2e)"))
                return false;
            for (int i = 0; i < 500 && !slow_started.load(); ++i)
                Sleep(10);
            if (!check(slow_started.load(),
                       "slow executor started (live e2e)"))
                return false;
            const std::string cc = jl::json_serialize(jobj({
                {"command", jstr("toolbox_cancel_tool_run")},
                {"payload", jobj({{"request_id", jstr("r-live-2")}})},
            }));
            send(ws, masked_text(cc).data(),
                 static_cast<int>(masked_text(cc).size()), 0);
            if (!check(read_frame(ws, &f, 15000) &&
                           f.payload.find("\"cancelled\":true") !=
                               std::string::npos,
                       "cancelled true (live e2e)"))
                return false;
            bool unexpected = false;
            if (read_frame(ws, &f, 3000))
                unexpected =
                    f.payload.find("slow_result") != std::string::npos;
            if (!check(!unexpected,
                       "cancelled request not responded (live e2e)"))
                return false;

            closesocket(ws);
            host.request_stop();
            host.run();
            WSACleanup();
            SetEnvironmentVariableA("GPTBRIDGE_WIRE_QUEUE", nullptr);
            std::error_code ec;
            fs::remove_all(queue_dir, ec);
            return true;
        }();
        if (blocked)
            native_tests::record_blocked(SUITE, name, detail);
        else
            native_tests::record(SUITE, name, pass, detail,
                                 native_tests::now_ms() - t0);
    }
}

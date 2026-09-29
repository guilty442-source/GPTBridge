// tool_host suite cases group b (unity-included by suite_tool_host.cpp)

void run_cases_b() {
    NT_TEST(SUITE, "http_ws_claim_end_to_end") {
        WSADATA wsa;
        NT_CHECK(WSAStartup(MAKEWORD(2, 2), &wsa) == 0, "wsa");
        const int port = free_port();
        FakeProxy proxy;
        std::atomic<int> exec_calls{0};

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
        cfg.submit_actor = "governance/tool/test-tool";
        cfg.submit_authorizer =
            "governance_rule.permission_directory.registries."
            "permissions.tool_routes:authorize_tool_self_route";

        th::ToolHostHooks hooks;
        hooks.executor = [&](const std::string& command,
                             const jl::JsonValue& payload,
                             const std::string& request_id,
                             const std::atomic<bool>&) {
            exec_calls.fetch_add(1);
            return jobj({{"ok", jbool(true)},
                         {"echo_command", jstr(command)},
                         {"echo_x", payload.get("x")
                                         ? *payload.get("x")
                                         : jl::JsonValue{}}});
        };
        hooks.proxy_call = [&](const std::string& op,
                               const std::string& args,
                               tpx::ProxyResponse* out,
                               tpx::SidecarError* e) {
            return proxy.call(op, args, out, e);
        };
        /* WS request/cancel 走 submit 側（submit 綁定）。 */
        hooks.proxy_submit_call = hooks.proxy_call;

        th::ToolHost host;
        std::string err;
        NT_CHECK(host.start(cfg, std::move(hooks), &err),
                 ("start: " + err).c_str());
        NT_CHECK(proxy.hellos == 2, "hello sent (process+submit)");

        /* /health 200＋tool_id；/metrics 200。 */
        std::string h = http_get(port, "/health");
        NT_CHECK(h.find("200") != std::string::npos, "health 200");
        NT_CHECK(h.find("test-tool") != std::string::npos,
                 "health tool_id");
        NT_CHECK(http_get(port, "/metrics").find("200") !=
                     std::string::npos,
                 "metrics 200");
        NT_CHECK(http_get(port, "/shutdown").find("403") !=
                     std::string::npos,
                 "shutdown no token 403");
        NT_CHECK(http_get(port, "/shutdown",
                          "X-GPTBridge-Shutdown-Token: wrong\r\n")
                         .find("403") != std::string::npos,
                 "shutdown wrong token 403");

        /* WS upgrade：錯 token → 403；正確 → 101。 */
        SOCKET bad = connect_loop(port);
        const std::string badreq =
            "GET /?token=" + std::string(64, 'b') + "&instance=" +
            std::string(wsid) +
            " HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n";
        send(bad, badreq.data(), static_cast<int>(badreq.size()), 0);
        char rbuf[512];
        int rn = recv(bad, rbuf, sizeof(rbuf) - 1, 0);
        rbuf[rn > 0 ? rn : 0] = '\0';
        NT_CHECK(std::string(rbuf).find("403") != std::string::npos,
                 "ws bad token 403");
        closesocket(bad);

        SOCKET ws = connect_loop(port);
        const std::string wsreq =
            "GET /?token=" + TOKEN + "&instance=" + std::string(wsid) +
            " HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n";
        send(ws, wsreq.data(), static_cast<int>(wsreq.size()), 0);
        rn = recv(ws, rbuf, sizeof(rbuf) - 1, 0);
        rbuf[rn > 0 ? rn : 0] = '\0';
        NT_CHECK(std::string(rbuf).find("101") != std::string::npos,
                 "ws upgrade 101");
        NT_CHECK(std::string(rbuf).find("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=") !=
                     std::string::npos,
                 "accept key rfc vector");

        /* 合法命令 → COMMAND_RECEIVED → claim→execute→respond→
           waiter 推送 {cmd}_result。 */
        const std::string cmd = jl::json_serialize(jobj({
            {"command", jstr("echo")},
            {"payload", jobj({{"request_id", jstr("r-1")},
                              {"tool_id", jstr("test-tool")},
                              {"x", jstr("v")}})},
        }));
        send(ws, masked_text(cmd).data(),
             static_cast<int>(masked_text(cmd).size()), 0);
        gtw::WsFrame f;
        NT_CHECK(read_frame(ws, &f), "recv COMMAND_RECEIVED");
        NT_CHECK(f.payload.find("COMMAND_RECEIVED") != std::string::npos,
                 "received event");

        NT_CHECK(read_frame(ws, &f, 8000), "recv echo_result");
        NT_CHECK(f.payload.find("echo_result") != std::string::npos,
                 "result event name");
        NT_CHECK(f.payload.find("r-1") != std::string::npos,
                 "result request_id");
        NT_CHECK(exec_calls.load() == 1, "executor ran once");
        NT_CHECK(proxy.responded.size() == 1, "respond called");

        /* 異形命令 → PERMISSION_DENIED 形式。 */
        const std::string bad2 = jl::json_serialize(jobj({
            {"command", jstr("evil")},
            {"payload", jobj({{"request_id", jstr("r-2")},
                              {"tool_id", jstr("other-tool")}})},
        }));
        send(ws, masked_text(bad2).data(),
             static_cast<int>(masked_text(bad2).size()), 0);
        NT_CHECK(read_frame(ws, &f), "recv denied result");
        NT_CHECK(f.payload.find("PERMISSION_DENIED") !=
                     std::string::npos,
                 "denied code");
        NT_CHECK(f.payload.find("evil_result") != std::string::npos,
                 "denied event name");

        /* Python `or` 語義：truthy 非字串 tool_id → deny；
           falsy（0/null）→ self → COMMAND_RECEIVED。 */
        const std::string truthy = jl::json_serialize(jobj({
            {"command", jstr("echo")},
            {"payload", jobj({{"request_id", jstr("r-t")},
                              {"tool_id", jnum(5)}})},
        }));
        send(ws, masked_text(truthy).data(),
             static_cast<int>(masked_text(truthy).size()), 0);
        NT_CHECK(read_frame(ws, &f), "recv truthy deny");
        NT_CHECK(f.payload.find("PERMISSION_DENIED") !=
                     std::string::npos,
                 "truthy non-self tool_id denied");
        const std::string falsy = jl::json_serialize(jobj({
            {"command", jstr("echo")},
            {"payload", jobj({{"request_id", jstr("r-f")},
                              {"tool_id", jnum(0)}})},
        }));
        send(ws, masked_text(falsy).data(),
             static_cast<int>(masked_text(falsy).size()), 0);
        NT_CHECK(read_frame(ws, &f), "recv falsy received");
        NT_CHECK(f.payload.find("COMMAND_RECEIVED") !=
                     std::string::npos,
                 "falsy tool_id treated as self");
        /* 消掉 r-f 的結果推送，避免干擾後續 read_frame。 */
        NT_CHECK(read_frame(ws, &f, 8000), "recv falsy result");

        /* 空 command → event="error"（非 "error_result"）。 */
        const std::string nocmd = jl::json_serialize(jobj({
            {"command", jstr("")},
            {"payload", jobj({{"request_id", jstr("r-e")}})},
        }));
        send(ws, masked_text(nocmd).data(),
             static_cast<int>(masked_text(nocmd).size()), 0);
        NT_CHECK(read_frame(ws, &f), "recv error event");
        NT_CHECK(f.payload.find("\"event\":\"error\"") !=
                     std::string::npos,
                 "empty command -> error event");
        NT_CHECK(f.payload.find("error_result") == std::string::npos,
                 "no error_result suffix");

        /* toolbox_cancel_tool_run：未知 id → ok false／cancelled false
           （Python：ok==cancelled）。 */
        const std::string cc = jl::json_serialize(jobj({
            {"command", jstr("toolbox_cancel_tool_run")},
            {"payload", jobj({{"request_id", jstr("no-such")}})},
        }));
        send(ws, masked_text(cc).data(),
             static_cast<int>(masked_text(cc).size()), 0);
        NT_CHECK(read_frame(ws, &f), "recv cancel result");
        NT_CHECK(f.payload.find("toolbox_cancel_tool_run_result") !=
                     std::string::npos,
                 "cancel result event");
        NT_CHECK(f.payload.find("\"cancelled\":false") !=
                     std::string::npos,
                 "cancelled false");
        NT_CHECK(f.payload.find("\"ok\":false") != std::string::npos,
                 "ok==cancelled (false)");

        closesocket(ws);

        /* /shutdown 正 token → 200 → run() 返回。 */
        std::thread runner([&] { host.run(); });
        const std::string shut = http_get(
            port, "/shutdown",
            "X-GPTBridge-Shutdown-Token: sh-token\r\n");
        NT_CHECK(shut.find("200") != std::string::npos,
                 "shutdown 200");
        runner.join();
        WSACleanup();
    }
    NT_END_TEST(SUITE, "http_ws_claim_end_to_end");

    NT_TEST(SUITE, "cancel_during_execution") {
        WSADATA wsa;
        NT_CHECK(WSAStartup(MAKEWORD(2, 2), &wsa) == 0, "wsa");
        const int port = free_port();
        FakeProxy proxy;
        proxy.cancelled.push_back("r-x"); /* 預置已取消 */
        std::atomic<bool> flag_seen{false};

        th::ToolHostConfig cfg;
        cfg.tool_id = "test-tool";
        cfg.port = port;
        cfg.session_token = TOKEN;
        cfg.env_gate_passed = true;  /* 測試直構 config：顯式標記已過 env 閘 */
        cfg.env_gate_proof = th::ToolHost::issue_test_gate_proof();
        cfg.shutdown_token = "";
        char wsid[GPTBRIDGE_GT_INSTANCE_ID_LEN + 1] = {0};
        gptbridge_gt_workspace_instance_id("test-tool", port, wsid);
        cfg.workspace_instance_id = wsid;

        th::ToolHostHooks hooks;
        hooks.executor = [&](const std::string&,
                             const jl::JsonValue&,
                             const std::string&,
                             const std::atomic<bool>& cancelled) {
            /* 等到取消旗標立起（claim 迴圈 100ms 輪詢會立它）。 */
            for (int i = 0; i < 400 && !cancelled.load(); ++i) Sleep(10);
            flag_seen = cancelled.load();
            return jobj({{"ok", jbool(true)}});
        };
        std::atomic<int> cancel_notices{0};
        hooks.cancellation = [&](const std::string&) {
            cancel_notices.fetch_add(1);
        };
        hooks.proxy_call = [&](const std::string& op,
                               const std::string& args,
                               tpx::ProxyResponse* out,
                               tpx::SidecarError* e) {
            return proxy.call(op, args, out, e);
        };

        th::ToolHost host;
        std::string err;
        NT_CHECK(host.start(cfg, std::move(hooks), &err), "start");

        /* 直接經假 proxy 佇列塞入一筆 request（繞過 WS）。 */
        {
            jl::JsonValue row = jobj({});
            row.object.emplace_back("request_id", jstr("r-x"));
            jl::JsonValue pl = jobj({{"request_id", jstr("r-x")}});
            pl.object.emplace_back("_governed_command", jstr("slow"));
            row.object.emplace_back("payload", pl);
            row.object.emplace_back("requester_actor", jstr("t"));
            std::lock_guard<std::mutex> lk(proxy.mu);
            proxy.queued.push_back(std::move(row));
        }
        /* 等 claim 輪詢到達並執行至取消（最多 8s）。 */
        for (int i = 0; i < 800 && !flag_seen.load(); ++i) Sleep(10);
        NT_CHECK(flag_seen.load(), "cancel flag reached executor");
        NT_CHECK(cancel_notices.load() >= 1, "cancellation hook fired");
        for (int i = 0; i < 200 && proxy.responded.empty(); ++i)
            Sleep(10);
        NT_CHECK(proxy.responded.empty(),
                 "cancelled request not responded (§4)");
        host.request_stop();
        host.run();
        WSACleanup();
    }
    NT_END_TEST(SUITE, "cancel_during_execution");

    NT_TEST(SUITE, "notify_stamp_wake") {
        /* §4 通知加速：notification_stamp 寫入戳變化須中斷空轉退避
           即刻重取（Python _listen_for_notifications 等價）。
           無喚醒路徑時最壞需等滿當前 backoff（封頂 500ms）。 */
        WSADATA wsa;
        NT_CHECK(WSAStartup(MAKEWORD(2, 2), &wsa) == 0, "wsa");
        const int port = free_port();
        FakeProxy proxy;

        th::ToolHostConfig cfg;
        cfg.tool_id = "test-tool";
        cfg.port = port;
        cfg.session_token = TOKEN;
        cfg.env_gate_passed = true;  /* 測試直構 config：顯式標記已過 env 閘 */
        cfg.env_gate_proof = th::ToolHost::issue_test_gate_proof();
        cfg.shutdown_token = "";
        char wsid[GPTBRIDGE_GT_INSTANCE_ID_LEN + 1] = {0};
        gptbridge_gt_workspace_instance_id("test-tool", port, wsid);
        cfg.workspace_instance_id = wsid;

        th::ToolHostHooks hooks;
        hooks.executor = [&](const std::string& command,
                             const jl::JsonValue&,
                             const std::string&,
                             const std::atomic<bool>&) {
            return jobj({{"ok", jbool(true)},
                         {"echo_command", jstr(command)}});
        };
        hooks.proxy_call = [&](const std::string& op,
                               const std::string& args,
                               tpx::ProxyResponse* out,
                               tpx::SidecarError* e) {
            return proxy.call(op, args, out, e);
        };

        th::ToolHost host;
        std::string err;
        NT_CHECK(host.start(cfg, std::move(hooks), &err), "start");

        /* 等探針進入穩態（首次觀測視為變化→先觸發一次喚醒，第二次
           起才是週期輪詢）。輪詢等待而非定值 Sleep——並行閘門／
           高負載下定值等待是抖動源。 */
        const auto t_wait = std::chrono::steady_clock::now();
        while (proxy.stamp_probes.load() < 2 &&
               std::chrono::steady_clock::now() - t_wait <
                   std::chrono::seconds(10)) {
            Sleep(20);
        }
        NT_CHECK(proxy.stamp_probes.load() >= 2, "stamp probe running");

        /* store 寫入 → stamp 變化＋新 request：探針須在空轉等待中
           觀測到變化並即刻重取（機制驗證：無喚醒路徑的實作根本不會
           呼叫 notification_stamp）。回應預算放寬至 1.5s 以吸收
           排程抖動。 */
        const int probes_before = proxy.stamp_probes.load();
        proxy.stamp.fetch_add(1);
        proxy.enqueue_request("r-wake", "echo");
        const auto t0 = std::chrono::steady_clock::now();
        bool responded = false;
        /* 兩條件共用同一 1.5s 預算：探針在 deadline==probe 週期重合時
           於次一空轉入口才計數，respond 後單次取樣在並行閘門高負載
           下會搶先於探針（flaky）。有界輪詢保留斷言強度。 */
        while (std::chrono::steady_clock::now() - t0 <
               std::chrono::milliseconds(1500)) {
            {
                std::lock_guard<std::mutex> lk(proxy.mu);
                responded = !proxy.responded.empty();
            }
            if (responded &&
                proxy.stamp_probes.load() > probes_before)
                break;
            Sleep(10);
        }
        NT_CHECK(proxy.stamp_probes.load() > probes_before,
                 "stamp change observed during idle wait");
        NT_CHECK(responded,
                 "stamp change woke claim loop (<1.5s)");
        host.request_stop();
        host.run();
        WSACleanup();
    }
    NT_END_TEST(SUITE, "notify_stamp_wake");
}

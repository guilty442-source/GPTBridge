// tool_host suite cases group a (unity-included by suite_tool_host.cpp)

void run_cases_a() {
    NT_TEST(SUITE, "env_load_and_gate") {
        /* 無 env → load_env fail-closed。 */
        th::ToolHostConfig cfg;
        std::string err;
        NT_CHECK(!th::ToolHost::load_env(&cfg, &err),
                 "missing env -> deny");
        NT_CHECK(err.find("PERMISSION_DENIED") != std::string::npos,
                 "deny code");
    }
    NT_END_TEST(SUITE, "env_load_and_gate");

    NT_TEST(SUITE, "forged_gate_flag_denied") {
        /* P6：env_gate_passed 只是自我聲明——直構 config 置位旗標但無
           登錄區 proof，start() 必須 fail-closed（env-gate-bypass）。 */
        th::ToolHostConfig cfg;
        cfg.tool_id = "test-tool";
        cfg.port = 45999;
        cfg.session_token = TOKEN;
        cfg.env_gate_passed = true;
        /* env_gate_proof 刻意留空：偽造旗標無法產生登錄區 nonce。 */
        std::string err;
        th::ToolHost host;
        NT_CHECK(!host.start(cfg, {}, &err), "forged flag -> deny");
        NT_CHECK(err.find("env-gate-bypass") != std::string::npos,
                 "deny code env-gate-bypass");
        /* 無效 proof 字串同樣不得通過。 */
        cfg.env_gate_proof = "deadbeef";
        th::ToolHost host2;
        NT_CHECK(!host2.start(cfg, {}, &err), "bogus proof -> deny");
        NT_CHECK(err.find("env-gate-bypass") != std::string::npos,
                 "deny code env-gate-bypass (bogus proof)");
    }
    NT_END_TEST(SUITE, "forged_gate_flag_denied");
}

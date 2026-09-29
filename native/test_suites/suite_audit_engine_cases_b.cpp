// audit_engine_suite cases 11-20 (unity-included by suite_audit_engine.cpp)

void run_cases_b() {
    NT_TEST("audit_engine_suite", "kind_json_key_value") {
        fs::path dir = make_case_dir("jsonkv");
        write_file(dir / "inv.json",
            "{\"tools\":{\"a\":{\"formal\":false,"
            "\"formality\":\"approved-implementation-x\"},"
            "\"b\":{\"formal\":false,\"formality\":\"bounded\"}},"
            "\"items\":[{\"id\":\"x\",\"formal\":false},"
            "{\"id\":\"y\",\"formal\":true}]}");
        std::vector<AuditCheck> checks = {
            {"kv1", "json-key-value", "inv.json", "",
             {"tools.a.formal=false"}, 0, ""},
            {"kv2", "json-key-value", "inv.json", "",
             {"tools.a.formality^=approved-implementation-"}, 0, ""},
            {"kv3", "json-key-value", "inv.json", "",
             {"tools.b.formality=bounded"}, 0, ""},
            {"kv4", "json-key-value", "inv.json", "",
             {"tools.a.formal=true"}, 0, ""},
            {"kv5", "json-key-value", "inv.json", "",
             {"tools.c.formal=false"}, 0, ""},
            {"kv6", "json-key-value", "inv.json", "",
             {"tools.a.formality=approved-implementation-x"}, 0, ""},
            /* 陣列元素以 id 選取（Python dict-comp by id 對齊） */
            {"kv7", "json-key-value", "inv.json", "",
             {"items[x].formal=false"}, 0, ""},
            {"kv8", "json-key-value", "inv.json", "",
             {"items[z].formal=false"}, 0, ""},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "kv1")->status == AuditStatus::PASS,
                 "bool equality");
        NT_CHECK(find(report, "kv2")->status == AuditStatus::PASS,
                 "string prefix");
        NT_CHECK(find(report, "kv3")->status == AuditStatus::PASS,
                 "string equality");
        NT_CHECK(find(report, "kv4")->status == AuditStatus::FAIL,
                 "wrong bool fails");
        NT_CHECK(find(report, "kv5")->status == AuditStatus::FAIL,
                 "missing path fails");
        NT_CHECK(find(report, "kv6")->status == AuditStatus::PASS,
                 "full string equality");
        NT_CHECK(find(report, "kv7")->status == AuditStatus::PASS,
                 "array element by id");
        NT_CHECK(find(report, "kv8")->status == AuditStatus::FAIL,
                 "unknown array id fails");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_json_key_value");

    NT_TEST("audit_engine_suite", "kind_json_key_absent") {
        fs::path dir = make_case_dir("jsonabsent");
        write_file(dir / "m.json",
            "{\"name_key\":\"tool.name\",\"window\":{\"title_key\":\"t\"}}");
        write_file(dir / "bad.json",
            "{\"name\":\"x\",\"window\":{\"title\":\"t\"}}");
        write_file(dir / "nullkey.json", "{\"name\":null}");
        std::vector<AuditCheck> checks = {
            {"ab1", "json-key-absent", "m.json", "",
             {"name", "window.title"}, 0, ""},
            {"ab2", "json-key-absent", "bad.json", "",
             {"name", "window.title"}, 0, ""},
            /* null 值視為不存在（對齊 Python get() 缺席語義） */
            {"ab3", "json-key-absent", "nullkey.json", "",
             {"name"}, 0, ""},
            {"ab4", "json-key-absent", "gone.json", "",
             {"name"}, 0, ""},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "ab1")->status == AuditStatus::PASS,
                 "keys absent");
        NT_CHECK(find(report, "ab2")->status == AuditStatus::FAIL,
                 "forbidden key present");
        NT_CHECK(find(report, "ab3")->status == AuditStatus::PASS,
                 "null counts as absent");
        NT_CHECK(find(report, "ab4")->status == AuditStatus::FAIL,
                 "unreadable fails closed");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_json_key_absent");

    NT_TEST("audit_engine_suite", "kind_json_key_value_ne") {
        fs::path dir = make_case_dir("jsonne");
        write_file(dir / "m.json",
            "{\"enabled\":false,\"lifecycle\":{\"stoppable\":false},"
            "\"status\":\"retired\",\"indep\":false}");
        std::vector<AuditCheck> checks = {
            /* status != running → PASS（retired） */
            {"n1", "json-key-value", "m.json", "",
             {"status!=running"}, 0, ""},
            /* enabled != true → PASS（false） */
            {"n2", "json-key-value", "m.json", "",
             {"enabled!=true"}, 0, ""},
            /* 缺路徑 → 不可能等值 → PASS */
            {"n3", "json-key-value", "m.json", "",
             {"missing.path!=x"}, 0, ""},
            /* stoppable == false，斷言 !=false → FAIL */
            {"n4", "json-key-value", "m.json", "",
             {"lifecycle.stoppable!=false"}, 0, ""},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "n1")->status == AuditStatus::PASS,
                 "!= on different value");
        NT_CHECK(find(report, "n2")->status == AuditStatus::PASS,
                 "!= bool false vs true");
        NT_CHECK(find(report, "n3")->status == AuditStatus::PASS,
                 "missing path satisfies !=");
        NT_CHECK(find(report, "n4")->status == AuditStatus::FAIL,
                 "!= on equal value fails");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_json_key_value_ne");

    NT_TEST("audit_engine_suite", "kind_file_not_contains_unless") {
        fs::path dir = make_case_dir("ncunless");
        write_file(dir / "ok.py",
            "async_playwright + InProcessEmbeddedBrowser");
        write_file(dir / "bad.py", "import async_playwright");
        write_file(dir / "clean.py", "nothing here");
        std::vector<AuditCheck> checks = {
            /* 含 marker 且含解禁標記 → PASS */
            {"u1", "file-not-contains-unless", "ok.py", "",
             {"async_playwright"}, 0, "", false, false, {},
             {"InProcessEmbeddedBrowser"}},
            /* 含 marker 且無解禁標記 → FAIL */
            {"u2", "file-not-contains-unless", "bad.py", "",
             {"async_playwright"}, 0, "", false, false, {},
             {"InProcessEmbeddedBrowser"}},
            /* 無 marker → PASS */
            {"u3", "file-not-contains-unless", "clean.py", "",
             {"async_playwright"}, 0, "", false, false, {},
             {"InProcessEmbeddedBrowser"}},
            /* optional 目標缺席 → PASS */
            {"u4", "file-not-contains-unless", "gone.py", "",
             {"async_playwright"}, 0, "", true, false, {},
             {"InProcessEmbeddedBrowser"}},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "u1")->status == AuditStatus::PASS,
                 "marker relieved by unless");
        NT_CHECK(find(report, "u2")->status == AuditStatus::FAIL,
                 "marker without unless fails");
        NT_CHECK(find(report, "u3")->status == AuditStatus::PASS,
                 "no marker passes");
        NT_CHECK(find(report, "u4")->status == AuditStatus::PASS,
                 "optional missing passes");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_file_not_contains_unless");

    NT_TEST("audit_engine_suite", "kind_glob_absent") {
        fs::path dir = make_case_dir("globabs");
        write_file(dir / "src" / "app.js", "x");
        write_file(dir / "src" / "deep" / "old.ts", "x");
        write_file(dir / "node_modules" / "dep" / "lib.ts", "x");
        write_file(dir / ".hidden" / "x.ts", "x");
        std::vector<AuditCheck> checks = {
            {"a1", "glob-absent", "", "*.ts", {}, 0, "", false, false,
             {"node_modules"}},
            {"a2", "glob-absent", "", "*.tsx", {}, 0, "", false, false,
             {"node_modules"}},
            /* 子樹掃描：src/ 底下的 .ts 仍命中 */
            {"a3", "glob-absent", "src", "*.ts", {}, 0, "", false, false,
             {}},
            /* 子樹不存在 → 無命中 PASS */
            {"a4", "glob-absent", "nope", "*.ts", {}, 0, "", false, false,
             {}},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "a1")->status == AuditStatus::FAIL,
                 "deep .ts found (excluded + dotdir hits ignored)");
        NT_CHECK(find(report, "a2")->status == AuditStatus::PASS,
                 "no .tsx anywhere");
        NT_CHECK(find(report, "a3")->status == AuditStatus::FAIL,
                 "subtree hit reported");
        NT_CHECK(find(report, "a4")->status == AuditStatus::PASS,
                 "absent subtree passes");
        NT_CHECK(find(report, "a1")->detail.find("old.ts")
                     != std::string::npos,
                 "hit detail names the file");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_glob_absent");

    NT_TEST("audit_engine_suite", "kind_tree_not_contains") {
        fs::path dir = make_case_dir("treenc");
        write_file(dir / "pkg" / "a.py", "clean");
        write_file(dir / "pkg" / "sub" / "deep.py", "import forbidden_mod");
        write_file(dir / "pkg" / "sub" / "skip.txt", "import forbidden_mod");
        write_file(dir / "pkg" / "node_modules" / "x.py",
                   "import forbidden_mod");
        std::vector<AuditCheck> checks = {
            /* 遞迴命中深層檔案；.txt 不命中 pattern；node_modules 略過 */
            {"t1", "tree-not-contains", "pkg", "*.py",
             {"forbidden_mod"}, 0, "", false, false,
             {"node_modules"}},
            /* 乾淨子樹 PASS */
            {"t2", "tree-not-contains", "pkg", "*.py",
             {"never_present"}, 0, "", false, false, {}},
            /* 子樹缺席 → FAIL（fail-closed） */
            {"t3", "tree-not-contains", "nope", "*.py",
             {"x"}, 0, "", false, false, {}},
            /* optional 缺席 → PASS */
            {"t4", "tree-not-contains", "nope", "*.py",
             {"x"}, 0, "", true, false, {}},
            /* 大小寫不敏感 */
            {"t5", "tree-not-contains", "pkg", "*.py",
             {"FORBIDDEN_MOD"}, 0, "", false, true,
             {"node_modules"}},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "t1")->status == AuditStatus::FAIL,
                 "deep forbidden marker detected");
        NT_CHECK(find(report, "t1")->detail.find("deep.py")
                     != std::string::npos,
                 "hit detail names the file");
        NT_CHECK(find(report, "t2")->status == AuditStatus::PASS,
                 "clean subtree passes");
        NT_CHECK(find(report, "t3")->status == AuditStatus::FAIL,
                 "missing subtree fails closed");
        NT_CHECK(find(report, "t4")->status == AuditStatus::PASS,
                 "optional missing subtree passes");
        NT_CHECK(find(report, "t5")->status == AuditStatus::FAIL,
                 "ignore_case match detected");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_tree_not_contains");

    NT_TEST("audit_engine_suite", "kind_file_not_contains") {
        fs::path dir = make_case_dir("notcontains");
        write_file(dir / "m.py", "clean source");
        write_file(dir / "bad.py", "uses GovernanceEnforcer here");
        std::vector<AuditCheck> checks = {
            {"f1", "file-not-contains", "m.py", "",
             {"GovernanceEnforcer"}, 0, "", false, false},
            {"f2", "file-not-contains", "bad.py", "",
             {"GovernanceEnforcer"}, 0, "", false, false},
            /* 必要目標缺席 → FAIL */
            {"f3", "file-not-contains", "absent.py", "",
             {"x"}, 0, "", false, false},
            /* optional 目標缺席 → PASS（條件式掃描語義） */
            {"f4", "file-not-contains", "absent.py", "",
             {"x"}, 0, "", true, false},
            /* ignore_case：內容大寫仍攔小寫標記 */
            {"f5", "file-not-contains", "bad.py", "",
             {"governanceenforcer"}, 0, "", false, true},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "f1")->status == AuditStatus::PASS, "clean");
        NT_CHECK(find(report, "f2")->status == AuditStatus::FAIL,
                 "forbidden marker");
        NT_CHECK(find(report, "f3")->status == AuditStatus::FAIL,
                 "required target missing");
        NT_CHECK(find(report, "f4")->status == AuditStatus::PASS,
                 "optional target missing");
        NT_CHECK(find(report, "f5")->status == AuditStatus::FAIL,
                 "ignore_case still catches");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_file_not_contains");

    NT_TEST("audit_engine_suite", "kind_json_has_keys_and_dir") {
        fs::path dir = make_case_dir("jsonkeys");
        write_file(dir / "ok.json", "{\"a\":1,\"b\":2}");
        write_file(dir / "missing.json", "{\"a\":1}");
        write_file(dir / "notobj.json", "[1,2]");
        write_file(dir / "bad.json", "{broken");
        fs::create_directories(dir / "real");
        std::vector<AuditCheck> checks = {
            {"j1", "json-has-keys", "ok.json", "", {"a", "b"}, 0, ""},
            {"j2", "json-has-keys", "missing.json", "", {"a", "b"}, 0, ""},
            {"j3", "json-has-keys", "notobj.json", "", {"a"}, 0, ""},
            {"j4", "json-has-keys", "bad.json", "", {"a"}, 0, ""},
            {"d1", "dir-exists", "real", "", {}, 0, ""},
            {"d2", "dir-exists", "nothere", "", {}, 0, ""},
            {"d3", "dir-exists", "ok.json", "", {}, 0, ""},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "j1")->status == AuditStatus::PASS, "all keys");
        NT_CHECK(find(report, "j2")->status == AuditStatus::FAIL,
                 "missing key");
        NT_CHECK(find(report, "j3")->status == AuditStatus::FAIL,
                 "non-object root");
        NT_CHECK(find(report, "j4")->status == AuditStatus::FAIL,
                 "malformed json");
        NT_CHECK(find(report, "d1")->status == AuditStatus::PASS, "dir");
        NT_CHECK(find(report, "d2")->status == AuditStatus::FAIL,
                 "missing dir");
        NT_CHECK(find(report, "d3")->status == AuditStatus::FAIL,
                 "file is not a dir");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_json_has_keys_and_dir");

    NT_TEST("audit_engine_suite", "report_json_shape") {
        gptbridge::AuditReport report;
        report.manifest_ok = true;
        report.checks.push_back(
            {"id\"esc", "file-exists", AuditStatus::PASS, ""});
        report.passed = 1;
        const std::string json = gptbridge::audit_report_json(report);
        NT_CHECK(json.find("\"passed\":1") != std::string::npos,
                 "passed count serialized");
        NT_CHECK(json.find("id\\\"esc") != std::string::npos,
                 "ids escaped");
        NT_CHECK(json.find("\"total\":1") != std::string::npos,
                 "total serialized");
    } NT_END_TEST("audit_engine_suite", "report_json_shape");

    NT_TEST("audit_engine_suite", "kind_py_bucket_budget") {
        fs::path dir = make_case_dir("pybudget");
        /* recipe: scan_roots=["src"], exclude_dirs=["__pycache__"],
         * exclude_file_substr=["/tests/"], rules GOV->"gov/" first-match,
         * fallback GENERAL_APP.  budgets: GOV 1/10, GENERAL_APP 2/40. */
        write_file(dir / "src" / "gov" / "a.py", "x\ny\n");
        write_file(dir / "src" / "misc" / "b.py", "x\n");
        write_file(dir / "src" / "misc" / "c.py", "x\nz\n");
        write_file(dir / "src" / "tests" / "t.py", "ignored\n");
        write_file(dir / "src" / "__pycache__" / "d.py", "ignored\n");
        const std::string baseline =
            "{\"measurement\":{"
            "\"scan_roots\":[\"src\"],"
            "\"exclude_dirs\":[\"__pycache__\"],"
            "\"exclude_file_substr\":[\"/tests/\"],"
            "\"rules\":{\"GOV\":[\"gov/\"]},"
            "\"fallback_bucket\":\"GENERAL_APP\"},"
            "\"zero_targets\":{\"GENERAL_APP\":{\"files\":2,\"loc\":40}},"
            "\"allowed_zones\":{\"GOV\":{\"files\":1,\"loc\":10}}}";
        write_file(dir / "baseline.json", baseline);
        std::vector<AuditCheck> checks = {
            {"pb1", "py-bucket-budget", "baseline.json", "", {}, 0, ""},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "pb1")->status == AuditStatus::PASS,
                 "all buckets within budget");
        /* over-budget GOVERNANCE fails */
        const std::string tight =
            "{\"measurement\":{"
            "\"scan_roots\":[\"src\"],"
            "\"exclude_dirs\":[\"__pycache__\"],"
            "\"exclude_file_substr\":[\"/tests/\"],"
            "\"rules\":{\"GOV\":[\"gov/\"]},"
            "\"fallback_bucket\":\"GENERAL_APP\"},"
            "\"zero_targets\":{\"GENERAL_APP\":{\"files\":2,\"loc\":40}},"
            "\"allowed_zones\":{\"GOV\":{\"files\":0,\"loc\":10}}}";
        write_file(dir / "baseline.json", tight);
        report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "pb1")->status == AuditStatus::FAIL,
                 "over-budget allowed zone fails");
        /* unreadable baseline fails closed */
        checks[0].path = "absent.json";
        report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "pb1")->status == AuditStatus::FAIL,
                 "missing baseline fails closed");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_py_bucket_budget");

    NT_TEST("audit_engine_suite", "kind_json_array_min_count") {
        fs::path dir = make_case_dir("arrmin");
        write_file(dir / "inv.json",
            "{\"rows\":["
            "{\"source_test\":\"tools/a/tests/t1.py\",\"status\":\"PENDING\"},"
            "{\"source_test\":\"tools/b/tests/t2.py\",\"status\":\"MIGRATED\"},"
            "{\"source_test\":\"tools/a/tests/t3.py\",\"status\":\"PENDING\"}"
            "]}");
        AuditCheck c;
        c.id = "amc1"; c.kind = "json-array-min-count";
        c.path = "inv.json"; c.items = "rows";
        c.markers = {"status=PENDING", "source_test^=tools/a/"};
        c.min_count = 2;
        auto report = gptbridge::audit_run({c}, native_tests::u8path(dir));
        NT_CHECK(find(report, "amc1")->status == AuditStatus::PASS,
                 "two PENDING rows under tools/a/ match");
        c.min_count = 3;
        report = gptbridge::audit_run({c}, native_tests::u8path(dir));
        NT_CHECK(find(report, "amc1")->status == AuditStatus::FAIL,
                 "count below min_count fails");
        NT_CHECK(find(report, "amc1")->detail.find("matched 2") !=
                     std::string::npos, "detail reports matched count");
        c.min_count = 1;
        c.markers = {"status=PENDING", "source_test^=tools/b/"};
        report = gptbridge::audit_run({c}, native_tests::u8path(dir));
        NT_CHECK(find(report, "amc1")->status == AuditStatus::FAIL,
                 "PENDING under tools/b absent fails");
        c.markers = {"status=MIGRATED", "source_test^=tools/b/"};
        report = gptbridge::audit_run({c}, native_tests::u8path(dir));
        NT_CHECK(find(report, "amc1")->status == AuditStatus::PASS,
                 "MIGRATED row under tools/b matches");
        c.items = "absent_arr"; c.markers = {"status=PENDING"};
        report = gptbridge::audit_run({c}, native_tests::u8path(dir));
        NT_CHECK(find(report, "amc1")->status == AuditStatus::FAIL,
                 "missing array path fails closed");
        c.items = "rows"; c.path = "absent.json"; c.optional = true;
        report = gptbridge::audit_run({c}, native_tests::u8path(dir));
        NT_CHECK(find(report, "amc1")->status == AuditStatus::PASS,
                 "optional missing file passes");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_json_array_min_count");

    NT_TEST("audit_engine_suite", "kind_fail") {
        AuditCheck c;
        c.id = "f1"; c.kind = "fail"; c.reason = "non-conforming target";
        auto report = gptbridge::audit_run({c}, ".");
        const auto* res = find(report, "f1");
        NT_CHECK(res->status == AuditStatus::FAIL,
                 "fail kind always fails");
        NT_CHECK(res->detail == "non-conforming target",
                 "fail detail carries reason");
    } NT_END_TEST("audit_engine_suite", "kind_fail");

}

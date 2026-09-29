// audit_engine_suite cases 1-10 (unity-included by suite_audit_engine.cpp)

void run_cases_a() {
    NT_TEST("audit_engine_suite", "manifest_parse_ok") {
        fs::path dir = make_case_dir("parse");
        fs::path m = dir / "m.json";
        write_file(m,
            "{\"schema\":\"star-audit-manifest/v1\",\"checks\":["
            "{\"id\":\"a\",\"kind\":\"file-exists\",\"path\":\"x.txt\"},"
            "{\"id\":\"b\",\"kind\":\"file-contains\",\"path\":\"x.txt\","
            "\"markers\":[\"m1\",\"m2\"]},"
            "{\"id\":\"c\",\"kind\":\"glob-min-count\",\"glob\":\"d/*.h\","
            "\"min_count\":2},"
            "{\"id\":\"d\",\"kind\":\"delegated\",\"reason\":\"py\"}]}");
        std::vector<AuditCheck> checks;
        std::string error;
        NT_CHECK(gptbridge::audit_load_manifest(native_tests::u8path(m), &checks, &error),
                 "manifest should parse");
        NT_CHECK(checks.size() == 4, "expected 4 checks");
        NT_CHECK(checks[0].id == "a" && checks[0].kind == "file-exists",
                 "first check fields");
        NT_CHECK(checks[1].markers.size() == 2, "markers decoded");
        NT_CHECK(checks[2].min_count == 2, "min_count decoded");
        NT_CHECK(checks[3].kind == "delegated" && checks[3].reason == "py",
                 "delegated fields");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "manifest_parse_ok");

    NT_TEST("audit_engine_suite", "manifest_fail_closed") {
        fs::path dir = make_case_dir("badm");
        fs::path m = dir / "m.json";
        std::vector<AuditCheck> checks;
        std::string error;
        write_file(m, "{not json");
        NT_CHECK(!gptbridge::audit_load_manifest(native_tests::u8path(m), &checks, &error),
                 "malformed json must fail");
        NT_CHECK(!error.empty(), "error reason required");
        NT_CHECK(!gptbridge::audit_load_manifest(
                     native_tests::u8path(dir / "absent.json"), &checks, &error),
                 "missing manifest must fail");
        write_file(m, "{\"schema\":\"x\"}");
        NT_CHECK(!gptbridge::audit_load_manifest(native_tests::u8path(m), &checks, &error),
                 "missing checks[] must fail");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "manifest_fail_closed");

    NT_TEST("audit_engine_suite", "kind_file_exists") {
        fs::path dir = make_case_dir("exists");
        write_file(dir / "ok.txt", "x");
        std::vector<AuditCheck> checks = {
            {"e1", "file-exists", "ok.txt", "", {}, 0, ""},
            {"e2", "file-exists", "missing.txt", "", {}, 0, ""},
            {"n1", "file-not-exists", "gone.txt", "", {}, 0, ""},
            {"n2", "file-not-exists", "ok.txt", "", {}, 0, ""},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(report.passed == 2 && report.failed == 2, "2 pass / 2 fail");
        NT_CHECK(find(report, "e1")->status == AuditStatus::PASS, "e1 pass");
        NT_CHECK(find(report, "e2")->status == AuditStatus::FAIL, "e2 fail");
        NT_CHECK(find(report, "n1")->status == AuditStatus::PASS, "n1 pass");
        NT_CHECK(find(report, "n2")->status == AuditStatus::FAIL, "n2 fail");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_file_exists");

    NT_TEST("audit_engine_suite", "kind_file_contains") {
        fs::path dir = make_case_dir("contains");
        write_file(dir / "c.txt", "alpha beta gamma");
        std::vector<AuditCheck> checks = {
            {"c1", "file-contains", "c.txt", "", {"alpha", "gamma"}, 0, ""},
            {"c2", "file-contains", "c.txt", "", {"delta"}, 0, ""},
            {"c3", "file-contains", "absent.txt", "", {"x"}, 0, ""},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "c1")->status == AuditStatus::PASS, "all markers");
        NT_CHECK(find(report, "c2")->status == AuditStatus::FAIL,
                 "missing marker");
        NT_CHECK(find(report, "c3")->status == AuditStatus::FAIL,
                 "unreadable target");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_file_contains");

    NT_TEST("audit_engine_suite", "kind_pollution") {
        fs::path dir = make_case_dir("pollution");
        write_file(dir / "clean.txt",
                   "\xe7\xb4\x94\xe6\xb7\xa8\xe4\xb8\xad\xe6\x96\x87? "
                   "\xe5\x96\xae\xe4\xb8\x80\xe5\x95\x8f\xe8\x99\x9f\n");
        write_file(dir / "q.txt", "double??pollution");
        write_file(dir / "fffd.txt", std::string("bad") + "\xef\xbf\xbd" + "x");
        write_file(dir / "bom.txt", std::string("\xef\xbb\xbf") + "bom");
        write_file(dir / "moji.txt", std::string("m") + "\xc3\x83" + "o");
        write_file(dir / "pua.txt", std::string("p") + "\xee\x80\x80");
        write_file(dir / "ctrl.txt", std::string("c") + '\x01');
        std::vector<AuditCheck> checks = {
            {"p0", "text-no-pollution", "clean.txt", "", {}, 0, ""},
            {"p1", "text-no-pollution", "q.txt", "", {}, 0, ""},
            {"p2", "text-no-pollution", "fffd.txt", "", {}, 0, ""},
            {"p3", "text-no-pollution", "bom.txt", "", {}, 0, ""},
            {"p4", "text-no-pollution", "moji.txt", "", {}, 0, ""},
            {"p5", "text-no-pollution", "pua.txt", "", {}, 0, ""},
            {"p6", "text-no-pollution", "ctrl.txt", "", {}, 0, ""},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "p0")->status == AuditStatus::PASS,
                 "clean text must pass");
        for (int i = 1; i <= 6; ++i)
            NT_CHECK(find(report, "p" + std::to_string(i))->status
                         == AuditStatus::FAIL,
                     "polluted fixture must fail");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_pollution");

    NT_TEST("audit_engine_suite", "kind_glob_and_delegated") {
        fs::path dir = make_case_dir("glob");
        write_file(dir / "inc" / "a.h", "");
        write_file(dir / "inc" / "b.h", "");
        write_file(dir / "inc" / "c.c", "");
        std::vector<AuditCheck> checks = {
            {"g1", "glob-min-count", "", "inc/*.h", {}, 2, ""},
            {"g2", "glob-min-count", "", "inc/*.h", {}, 3, ""},
            {"d1", "delegated", "", "", {}, 0, "python oracle"},
            {"u1", "unknown-kind", "", "", {}, 0, ""},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "g1")->status == AuditStatus::PASS, "glob >= 2");
        NT_CHECK(find(report, "g2")->status == AuditStatus::FAIL, "glob < 3");
        NT_CHECK(find(report, "d1")->status == AuditStatus::DELEGATED,
                 "delegated kind");
        NT_CHECK(find(report, "u1")->status == AuditStatus::DELEGATED,
                 "unknown kind delegates, never silently passes");
        NT_CHECK(report.delegated == 2, "delegated count");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_glob_and_delegated");

    NT_TEST("audit_engine_suite", "kind_glob_not_contains") {
        fs::path dir = make_case_dir("globnc");
        write_file(dir / "host" / "clean.cs", "public class A {}");
        write_file(dir / "host" / "bad.cs", "uses HMACSHA256");
        write_file(dir / "host" / "note.txt", "HMACSHA256 in text");
        std::vector<AuditCheck> checks = {
            {"nc1", "glob-not-contains", "", "host/*.cs",
             {"HMACSHA"}, 0, "", false, false},
            {"nc2", "glob-not-contains", "", "host/*.cs",
             {"Npgsql"}, 0, "", false, false},
            {"nc3", "glob-not-contains", "", "absent/*.cs",
             {"x"}, 0, "", false, false},
            {"nc4", "glob-not-contains", "", "absent/*.cs",
             {"x"}, 0, "", true, false},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "nc1")->status == AuditStatus::FAIL,
                 "forbidden marker in matched file");
        NT_CHECK(find(report, "nc2")->status == AuditStatus::PASS,
                 "clean glob passes");
        NT_CHECK(find(report, "nc3")->status == AuditStatus::FAIL,
                 "missing dir fails closed");
        NT_CHECK(find(report, "nc4")->status == AuditStatus::PASS,
                 "optional missing dir passes");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_glob_not_contains");

    NT_TEST("audit_engine_suite", "kind_glob_contains") {
        fs::path dir = make_case_dir("globc");
        write_file(dir / "sp" / "a.py", "stdin=subprocess.DEVNULL");
        write_file(dir / "sp" / "b.py", "close_fds=True");
        std::vector<AuditCheck> checks = {
            /* union 語義：兩 marker 分散於不同檔仍 PASS */
            {"gc1", "glob-contains", "", "sp/*.py",
             {"stdin=subprocess.DEVNULL", "close_fds=True"},
             0, "", false, false},
            {"gc2", "glob-contains", "", "sp/*.py",
             {"stdin=subprocess.DEVNULL", "missing-marker"},
             0, "", false, false},
            {"gc3", "glob-contains", "", "absent/*.py",
             {"x"}, 0, "", false, false},
        };
        auto report = gptbridge::audit_run(checks, native_tests::u8path(dir));
        NT_CHECK(find(report, "gc1")->status == AuditStatus::PASS,
                 "markers split across files still found");
        NT_CHECK(find(report, "gc2")->status == AuditStatus::FAIL,
                 "absent marker fails");
        NT_CHECK(find(report, "gc3")->status == AuditStatus::FAIL,
                 "missing dir fails closed");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_glob_contains");
}

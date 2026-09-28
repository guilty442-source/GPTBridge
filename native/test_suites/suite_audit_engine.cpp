// suite_audit_engine.cpp — 原生審計引擎測試（P0-9 §1.1）
//
// 覆蓋：manifest 解析（fail-closed）、各檢查 kind、污染掃描、
// delegated 移交語義、glob 計數、報告序列化。
// 每項檢查以 temp dir 自建 fixture（唯讀驗證，不觸動倉庫檔案）。
#include "harness.hpp"
#include "audit_engine.h"

#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;
using gptbridge::AuditCheck;
using gptbridge::AuditStatus;

namespace {

fs::path make_case_dir(const std::string& name) {
    fs::path dir = fs::temp_directory_path() / ("audit_eng_" + name);
    std::error_code ec;
    fs::remove_all(dir, ec);
    fs::create_directories(dir, ec);
    return dir;
}

void write_file(const fs::path& path, const std::string& content) {
    std::error_code ec;
    fs::create_directories(path.parent_path(), ec);
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    out << content;
}

void remove_dir(const fs::path& dir) {
    std::error_code ec;
    fs::remove_all(dir, ec);
}

const gptbridge::AuditCheckResult* find(
    const gptbridge::AuditReport& report, const std::string& id) {
    for (const auto& c : report.checks)
        if (c.id == id) return &c;
    return nullptr;
}

}  // namespace

int main() {
    NT_SUITE("audit_engine_suite");

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

    return native_tests::report("audit_engine_suite.json");
}

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
        NT_CHECK(gptbridge::audit_load_manifest(m.u8string(), &checks, &error),
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
        NT_CHECK(!gptbridge::audit_load_manifest(m.u8string(), &checks, &error),
                 "malformed json must fail");
        NT_CHECK(!error.empty(), "error reason required");
        NT_CHECK(!gptbridge::audit_load_manifest(
                     (dir / "absent.json").u8string(), &checks, &error),
                 "missing manifest must fail");
        write_file(m, "{\"schema\":\"x\"}");
        NT_CHECK(!gptbridge::audit_load_manifest(m.u8string(), &checks, &error),
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
        auto report = gptbridge::audit_run(checks, dir.u8string());
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
        auto report = gptbridge::audit_run(checks, dir.u8string());
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
        auto report = gptbridge::audit_run(checks, dir.u8string());
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
        auto report = gptbridge::audit_run(checks, dir.u8string());
        NT_CHECK(find(report, "g1")->status == AuditStatus::PASS, "glob >= 2");
        NT_CHECK(find(report, "g2")->status == AuditStatus::FAIL, "glob < 3");
        NT_CHECK(find(report, "d1")->status == AuditStatus::DELEGATED,
                 "delegated kind");
        NT_CHECK(find(report, "u1")->status == AuditStatus::DELEGATED,
                 "unknown kind delegates, never silently passes");
        NT_CHECK(report.delegated == 2, "delegated count");
        remove_dir(dir);
    } NT_END_TEST("audit_engine_suite", "kind_glob_and_delegated");

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
        auto report = gptbridge::audit_run(checks, dir.u8string());
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
        auto report = gptbridge::audit_run(checks, dir.u8string());
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

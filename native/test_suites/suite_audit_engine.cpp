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

#include "suite_audit_engine_cases_a.cpp"
#include "suite_audit_engine_cases_b.cpp"

}  // namespace

int main() {
    NT_SUITE("audit_engine_suite");

    run_cases_a();
    run_cases_b();

    return native_tests::report("audit_engine_suite.json");
}

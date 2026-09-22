/* audit_engine.cpp — 原生審計引擎實作（C++17，純標準庫，唯讀）

manifest（star-audit-manifest/v1）由 Python 受管工具產生；本引擎執行
可歸約的檢查 kind，其餘一律 delegated。任何輸入異常 → fail-closed。
*/
#include "audit_engine.h"

#include "jsonlite.h"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <sstream>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif

namespace fs = std::filesystem;

namespace gptbridge {

using jsonlite::JsonError;
using jsonlite::JsonParser;
using jsonlite::JsonValue;

namespace {

/* JSON reader moved to shared native/include/jsonlite.h (extracted
 * unchanged; audit semantics preserved — malformed input still throws
 * JsonError and aborts the manifest load). */

/* ------------------------------------------------------------------
 * File helpers (read-only)
 * ------------------------------------------------------------------ */

bool read_file(const fs::path& path, std::string* out) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    std::ostringstream ss;
    ss << f.rdbuf();
    if (f.bad()) return false;
    *out = ss.str();
    return true;
}

bool is_readonly(const fs::path& path) {
#ifdef _WIN32
    DWORD attrs = GetFileAttributesW(path.wstring().c_str());
    if (attrs == INVALID_FILE_ATTRIBUTES) return false;
    return (attrs & FILE_ATTRIBUTE_READONLY) != 0;
#else
    std::error_code ec;
    auto perms = fs::status(path, ec).permissions();
    if (ec) return false;
    return (perms & fs::perms::owner_write) == fs::perms::none;
#endif
}

/* Simple single-level wildcard match: '*' any run, '?' one char. */
bool wildcard_match(const std::string& pattern, const std::string& name) {
    size_t pi = 0, ni = 0, star_p = std::string::npos, star_n = 0;
    while (ni < name.size()) {
        if (pi < pattern.size() &&
            (pattern[pi] == '?' || pattern[pi] == name[ni])) {
            ++pi; ++ni;
        } else if (pi < pattern.size() && pattern[pi] == '*') {
            star_p = pi++; star_n = ni;
        } else if (star_p != std::string::npos) {
            pi = star_p + 1; ni = ++star_n;
        } else return false;
    }
    while (pi < pattern.size() && pattern[pi] == '*') ++pi;
    return pi == pattern.size();
}

/* Text-pollution scan — port of _TEXT_POLLUTION regex + C0 control rule:
 *   \?{2,} | U+FFFD (EF BF BD) | BOM (EF BB BF) | ï»¿ | Ã. | Â. |
 *   â(€|€™|€œ|€\x9d) | U+E000–U+F8FF (PUA) | C0 except \n\r\t
 */
bool contains_pollution(const std::string& text) {
    const unsigned char* s = reinterpret_cast<const unsigned char*>(text.data());
    const size_t n = text.size();
    size_t run_q = 0;
    for (size_t i = 0; i < n; ++i) {
        unsigned char c = s[i];
        if (c == '?') { if (++run_q >= 2) return true; }
        else run_q = 0;
        if (c < 0x20 && c != '\n' && c != '\r' && c != '\t') return true;
        if (c == 0xEF && i + 2 < n) {
            if (s[i + 1] == 0xBF && s[i + 2] == 0xBD)
                return true;                    /* U+FFFD */
            if (s[i + 1] == 0xBB && s[i + 2] == 0xBF)
                return true;                    /* U+FEFF (BOM) */
            if (s[i + 1] >= 0x80 && s[i + 1] <= 0xA3)
                return true;                    /* PUA U+F000–F8FF */
        }
        if (c == 0xEE) return true;             /* PUA U+E000–EFFF */
        if (c == 0xC3 && i + 1 < n &&
            (s[i + 1] == 0x83 || s[i + 1] == 0x82))
            return true;                        /* Ã. / Â. mojibake */
        if (c == 0xE2 && i + 2 < n && s[i + 1] == 0x82 &&
            (s[i + 2] == 0xAC || s[i + 2] == 0x99 ||
             s[i + 2] == 0x9C || s[i + 2] == 0x9D))
            return true;                        /* â€ â€™ â€œ â€\x9d */
    }
    return false;
}

std::string to_lower(const std::string& s) {
    std::string out = s;
    for (auto& c : out)
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return out;
}

AuditCheckResult run_check(const AuditCheck& check, const std::string& root) {
    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    const fs::path target = fs::u8path(root) / fs::u8path(check.path);
    std::error_code ec;

    if (check.kind == "delegated") {
        r.status = AuditStatus::DELEGATED;
        r.detail = check.reason;
        return r;
    }
    if (check.kind == "file-exists") {
        if (fs::is_regular_file(target, ec)) { r.status = AuditStatus::PASS; }
        else { r.status = AuditStatus::FAIL; r.detail = "missing: " + check.path; }
        return r;
    }
    if (check.kind == "file-not-exists") {
        if (!fs::exists(target, ec) && !ec) { r.status = AuditStatus::PASS; }
        else { r.status = AuditStatus::FAIL; r.detail = "forbidden path present: " + check.path; }
        return r;
    }
    if (check.kind == "file-readonly") {
        if (!fs::is_regular_file(target, ec)) {
            r.status = AuditStatus::FAIL; r.detail = "missing: " + check.path;
        } else if (is_readonly(target)) {
            r.status = AuditStatus::PASS;
        } else {
            r.status = AuditStatus::FAIL; r.detail = "not read-only: " + check.path;
        }
        return r;
    }
    if (check.kind == "dir-exists") {
        /* 語義對齊 Python：必須為實體目錄且非 symlink */
        if (fs::is_symlink(target, ec)) {
            r.status = AuditStatus::FAIL;
            r.detail = "must be a physical directory (symlink): " + check.path;
        } else if (fs::is_directory(target, ec)) {
            r.status = AuditStatus::PASS;
        } else {
            r.status = AuditStatus::FAIL; r.detail = "missing dir: " + check.path;
        }
        return r;
    }
    if (check.kind == "file-contains") {
        std::string content;
        if (!read_file(target, &content)) {
            if (check.optional && !fs::exists(target, ec)) {
                r.status = AuditStatus::PASS;
                return r;
            }
            r.status = AuditStatus::FAIL; r.detail = "unreadable: " + check.path;
            return r;
        }
        for (const auto& m : check.markers) {
            if (content.find(m) == std::string::npos) {
                r.status = AuditStatus::FAIL;
                r.detail = "missing marker: " + m;
                return r;
            }
        }
        r.status = AuditStatus::PASS;
        return r;
    }
    if (check.kind == "file-not-contains") {
        std::string content;
        if (!read_file(target, &content)) {
            if (check.optional && !fs::exists(target, ec)) {
                r.status = AuditStatus::PASS;   /* 條件式禁標檢查：缺席即略過 */
                return r;
            }
            r.status = AuditStatus::FAIL; r.detail = "unreadable: " + check.path;
            return r;
        }
        const std::string haystack =
            check.ignore_case ? to_lower(content) : content;
        for (const auto& m : check.markers) {
            const std::string needle =
                check.ignore_case ? to_lower(m) : m;
            if (haystack.find(needle) != std::string::npos) {
                r.status = AuditStatus::FAIL;
                r.detail = "forbidden marker present: " + m;
                return r;
            }
        }
        r.status = AuditStatus::PASS;
        return r;
    }
    if (check.kind == "json-has-keys") {
        std::string content;
        if (!read_file(target, &content)) {
            r.status = AuditStatus::FAIL; r.detail = "unreadable: " + check.path;
            return r;
        }
        JsonValue doc;
        try { doc = JsonParser(content).parse(); }
        catch (const JsonError&) {
            r.status = AuditStatus::FAIL;
            r.detail = "invalid json: " + check.path;
            return r;
        }
        if (doc.type != JsonValue::Type::Object) {
            r.status = AuditStatus::FAIL;
            r.detail = "json root is not an object: " + check.path;
            return r;
        }
        for (const auto& key : check.markers) {
            if (doc.get(key) == nullptr) {
                r.status = AuditStatus::FAIL;
                r.detail = "missing key: " + key;
                return r;
            }
        }
        r.status = AuditStatus::PASS;
        return r;
    }
    if (check.kind == "text-no-pollution") {
        std::string content;
        if (!read_file(target, &content)) {
            r.status = AuditStatus::FAIL; r.detail = "unreadable: " + check.path;
            return r;
        }
        if (contains_pollution(content)) {
            r.status = AuditStatus::FAIL; r.detail = "pollution: " + check.path;
        } else {
            r.status = AuditStatus::PASS;
        }
        return r;
    }
    if (check.kind == "json-parses") {
        std::string content;
        if (!read_file(target, &content)) {
            r.status = AuditStatus::FAIL; r.detail = "unreadable: " + check.path;
            return r;
        }
        try { JsonParser(content).parse(); r.status = AuditStatus::PASS; }
        catch (const JsonError&) {
            r.status = AuditStatus::FAIL; r.detail = "invalid json: " + check.path;
        }
        return r;
    }
    if (check.kind == "glob-min-count") {
        const fs::path g = fs::u8path(check.glob);
        const fs::path dir = fs::u8path(root) / g.parent_path();
        const std::string pattern = g.filename().u8string();
        std::int64_t count = 0;
        if (fs::is_directory(dir, ec)) {
            for (const auto& entry : fs::directory_iterator(dir, ec)) {
                if (entry.is_regular_file(ec) &&
                    wildcard_match(pattern, entry.path().filename().u8string()))
                    ++count;
            }
        }
        if (count >= check.min_count) { r.status = AuditStatus::PASS; }
        else {
            r.status = AuditStatus::FAIL;
            r.detail = "glob " + check.glob + " count " +
                       std::to_string(count) + " < " +
                       std::to_string(check.min_count);
        }
        return r;
    }
    /* 未支援 kind：delegated（顯式移交，不靜默） */
    r.status = AuditStatus::DELEGATED;
    r.detail = "unsupported kind";
    return r;
}

}  // namespace

AuditReport audit_run(const std::vector<AuditCheck>& checks,
                      const std::string& root) {
    AuditReport report;
    report.manifest_ok = true;
    for (const auto& check : checks) {
        AuditCheckResult r = run_check(check, root);
        switch (r.status) {
            case AuditStatus::PASS: ++report.passed; break;
            case AuditStatus::FAIL: ++report.failed; break;
            case AuditStatus::DELEGATED: ++report.delegated; break;
        }
        report.checks.push_back(std::move(r));
    }
    return report;
}

bool audit_load_manifest(const std::string& manifest_path,
                         std::vector<AuditCheck>* out_checks,
                         std::string* out_error) {
    std::string text;
    if (!read_file(fs::u8path(manifest_path), &text)) {
        if (out_error) *out_error = "manifest unreadable: " + manifest_path;
        return false;
    }
    JsonValue root;
    try {
        root = JsonParser(text).parse();
    } catch (const JsonError&) {
        if (out_error) *out_error = "manifest is not valid JSON";
        return false;
    }
    const JsonValue* checks = root.get("checks");
    if (!checks || checks->type != JsonValue::Type::Array) {
        if (out_error) *out_error = "manifest missing checks[]";
        return false;
    }
    for (const auto& item : checks->array) {
        if (item.type != JsonValue::Type::Object) {
            if (out_error) *out_error = "check entry is not an object";
            return false;
        }
        AuditCheck c;
        auto get_str = [&](const char* key) -> std::string {
            const JsonValue* v = item.get(key);
            return (v && v->type == JsonValue::Type::String) ? v->string : "";
        };
        c.id = get_str("id");
        c.kind = get_str("kind");
        c.path = get_str("path");
        c.glob = get_str("glob");
        c.reason = get_str("reason");
        if (const JsonValue* v = item.get("min_count"))
            if (v->type == JsonValue::Type::Number)
                c.min_count = static_cast<std::int64_t>(v->number);
        if (const JsonValue* v = item.get("optional"))
            if (v->type == JsonValue::Type::Bool)
                c.optional = v->boolean;
        if (const JsonValue* v = item.get("ignore_case"))
            if (v->type == JsonValue::Type::Bool)
                c.ignore_case = v->boolean;
        if (const JsonValue* v = item.get("markers"))
            if (v->type == JsonValue::Type::Array)
                for (const auto& m : v->array)
                    if (m.type == JsonValue::Type::String)
                        c.markers.push_back(m.string);
        if (c.id.empty() || c.kind.empty()) {
            if (out_error) *out_error = "check entry missing id/kind";
            return false;
        }
        out_checks->push_back(std::move(c));
    }
    return true;
}

std::string audit_report_json(const AuditReport& report) {
    auto esc = [](const std::string& s) {
        std::string out;
        for (char c : s) {
            switch (c) {
                case '"': out += "\\\""; break;
                case '\\': out += "\\\\"; break;
                case '\n': out += "\\n"; break;
                case '\r': out += "\\r"; break;
                case '\t': out += "\\t"; break;
                default:
                    if (static_cast<unsigned char>(c) < 0x20) {
                        char buf[8];
                        std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                        out += buf;
                    } else out += c;
            }
        }
        return out;
    };
    std::string out = "{\"engine\":\"star-audit-engine/v1\",\"manifest_ok\":";
    out += report.manifest_ok ? "true" : "false";
    out += ",\"manifest_error\":\"" + esc(report.manifest_error) + "\"";
    out += ",\"checks\":[";
    for (size_t i = 0; i < report.checks.size(); ++i) {
        const auto& c = report.checks[i];
        const char* status = c.status == AuditStatus::PASS ? "PASS"
            : c.status == AuditStatus::FAIL ? "FAIL" : "DELEGATED";
        out += i ? "," : "";
        out += "{\"id\":\"" + esc(c.id) + "\",\"kind\":\"" + esc(c.kind) +
               "\",\"status\":\"" + status +
               "\",\"detail\":\"" + esc(c.detail) + "\"}";
    }
    out += "],\"passed\":" + std::to_string(report.passed) +
           ",\"failed\":" + std::to_string(report.failed) +
           ",\"delegated\":" + std::to_string(report.delegated) +
           ",\"total\":" + std::to_string(report.checks.size()) + "}";
    return out;
}

}  // namespace gptbridge

#ifdef GPTBRIDGE_AUDIT_ENGINE_CLI
int main(int argc, char** argv) {
    std::string manifest, root, report_path;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--manifest" && i + 1 < argc) manifest = argv[++i];
        else if (arg == "--root" && i + 1 < argc) root = argv[++i];
        else if (arg == "--report" && i + 1 < argc) report_path = argv[++i];
    }
    if (manifest.empty() || root.empty()) {
        std::fprintf(stderr,
            "usage: audit-engine --manifest <path> --root <path> [--report <path>]\n");
        return 2;
    }
    std::vector<gptbridge::AuditCheck> checks;
    std::string error;
    gptbridge::AuditReport report;
    if (!gptbridge::audit_load_manifest(manifest, &checks, &error)) {
        report.manifest_ok = false;
        report.manifest_error = error;
    } else {
        report = gptbridge::audit_run(checks, root);
    }
    const std::string json = gptbridge::audit_report_json(report);
    if (!report_path.empty()) {
        std::ofstream out(fs::u8path(report_path), std::ios::binary | std::ios::trunc);
        out << json << "\n";
    }
    std::fputs(json.c_str(), stdout);
    std::fputc('\n', stdout);
    return (report.manifest_ok && report.failed == 0) ? 0 : 1;
}
#endif

/* audit_engine.cpp — 原生審計引擎實作（C++17，純標準庫，唯讀）

manifest（star-audit-manifest/v1）由 Python 受管工具產生；本引擎執行
可歸約的檢查 kind，其餘一律 delegated。任何輸入異常 → fail-closed。
*/
#include "audit_engine.h"

#include "jsonlite.h"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <future>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <thread>
#include <unordered_map>

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

// C++20+: path::u8string() returns std::u8string (char8_t). Byte-preserving
// conversion so UTF-8 filenames still bind to std::string comparisons.
std::string u8_bytes(const fs::path& p) {
#if defined(__cpp_char8_t)
    const auto s = p.u8string();
    return std::string(reinterpret_cast<const char*>(s.c_str()), s.size());
#else
    return p.u8string();
#endif
}

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

/* ------------------------------------------------------------------
 * Per-run shared resource caches (bounded: one entry per distinct path)
 *
 * The manifest concentrates content/JSON work on a handful of hot
 * targets — the five zh-TW mirror parts absorb ~1k JSON checks plus
 * not-contains scans, and the permission registries absorb several
 * hundred marker checks.  Every check previously re-read and re-parsed
 * them independently.  Successes are memoized as shared_ptr<const T>
 * so the worker threads share one build; failures are NEVER cached —
 * a transient IO/parse error must not stick (fail-closed parity).
 * ------------------------------------------------------------------ */

/* The memo maps are per-run: audit_run() calls cache_reset() first,
 * so a file mutated between runs (rewritten baseline, edited target)
 * is never served stale while a single run still shares one read. */
std::mutex g_cache_mu;
std::unordered_map<std::string, std::shared_ptr<const std::string>>
    g_text_cache;
std::unordered_map<std::string, std::shared_ptr<const std::string>>
    g_lower_cache;
std::unordered_map<std::string, std::shared_ptr<const JsonValue>>
    g_json_cache;

void cache_reset() {
    std::lock_guard<std::mutex> g(g_cache_mu);
    g_text_cache.clear();
    g_lower_cache.clear();
    g_json_cache.clear();
}

std::shared_ptr<const std::string> cached_text(const fs::path& target) {
    const std::string key = u8_bytes(target.lexically_normal());
    {
        std::lock_guard<std::mutex> g(g_cache_mu);
        const auto it = g_text_cache.find(key);
        if (it != g_text_cache.end()) return it->second;
    }
    auto content = std::make_shared<std::string>();
    if (!read_file(target, content.get())) return nullptr;
    std::lock_guard<std::mutex> g(g_cache_mu);
    return g_text_cache.emplace(std::move(key), std::move(content))
        .first->second;
}

std::shared_ptr<const std::string> cached_lower(const fs::path& target) {
    const std::string key = u8_bytes(target.lexically_normal());
    {
        std::lock_guard<std::mutex> g(g_cache_mu);
        const auto it = g_lower_cache.find(key);
        if (it != g_lower_cache.end()) return it->second;
    }
    const auto text = cached_text(target);
    if (!text) return nullptr;
    auto lowered = std::make_shared<std::string>(to_lower(*text));
    std::lock_guard<std::mutex> g(g_cache_mu);
    return g_lower_cache.emplace(std::move(key), std::move(lowered))
        .first->second;
}

/* unreadable_out distinguishes "file missing/unreadable" from
 * "present but invalid JSON" — both yield nullptr but the check sites
 * report different detail strings (and optional→PASS applies only to
 * the missing case). */
std::shared_ptr<const JsonValue> cached_json(const fs::path& target,
                                             bool* unreadable_out) {
    const std::string key = u8_bytes(target.lexically_normal());
    {
        std::lock_guard<std::mutex> g(g_cache_mu);
        const auto it = g_json_cache.find(key);
        if (it != g_json_cache.end()) {
            *unreadable_out = false;
            return it->second;
        }
    }
    const auto text = cached_text(target);
    if (!text) {
        *unreadable_out = true;
        return nullptr;
    }
    std::shared_ptr<JsonValue> doc;
    try { doc = std::make_shared<JsonValue>(JsonParser(*text).parse()); }
    catch (const JsonError&) {
        *unreadable_out = false;
        return nullptr;
    }
    std::lock_guard<std::mutex> g(g_cache_mu);
    *unreadable_out = false;
    return g_json_cache.emplace(std::move(key), std::move(doc))
        .first->second;
}

/* dotted 路徑解析：逐層走 object；段名可帶 [KEY] 選取 object 陣列中
 * id==KEY 的元素（對齊 Python {item['id']: item for item in arr}）。
 * 路徑不可解析時回傳 nullptr。 */
const JsonValue* resolve_json_path(const JsonValue& doc,
                                   const std::string& dotted) {
    const JsonValue* node = &doc;
    size_t start = 0;
    while (node != nullptr) {
        const size_t dot = dotted.find('.', start);
        std::string key = dotted.substr(
            start, dot == std::string::npos ? std::string::npos : dot - start);
        std::string array_key;
        const size_t bracket = key.find('[');
        if (bracket != std::string::npos && key.back() == ']') {
            array_key = key.substr(bracket + 1, key.size() - bracket - 2);
            key = key.substr(0, bracket);
        }
        node = node->get(key);
        if (node == nullptr) return nullptr;
        if (!array_key.empty()) {
            if (node->type != JsonValue::Type::Array) return nullptr;
            const JsonValue* found_elem = nullptr;
            for (const auto& elem : node->array) {
                if (elem.type == JsonValue::Type::Object) {
                    const JsonValue* idv = elem.get("id");
                    if (idv != nullptr &&
                        idv->type == JsonValue::Type::String &&
                        idv->string == array_key) {
                        found_elem = &elem;
                        break;
                    }
                }
            }
            node = found_elem;
            if (node == nullptr) return nullptr;
        }
        if (dot == std::string::npos) break;
        start = dot + 1;
    }
    return node;
}

#include "audit_engine_checks_a.cpp"
#include "audit_engine_checks_b.cpp"
#include "audit_engine_checks_c.cpp"

AuditCheckResult run_check(const AuditCheck& check, const std::string& root) {
    const fs::path target = fs::u8path(root) / fs::u8path(check.path);
    std::error_code ec;

    if (check.kind == "delegated") return check_delegated(check, root, target, ec);
    if (check.kind == "fail") return check_fail(check, root, target, ec);
    if (check.kind == "file-exists") return check_file_exists(check, root, target, ec);
    if (check.kind == "file-not-exists") return check_file_not_exists(check, root, target, ec);
    if (check.kind == "file-readonly") return check_file_readonly(check, root, target, ec);
    if (check.kind == "dir-exists") return check_dir_exists(check, root, target, ec);
    if (check.kind == "file-contains") return check_file_contains(check, root, target, ec);
    if (check.kind == "file-not-contains") return check_file_not_contains(check, root, target, ec);
    if (check.kind == "file-not-contains-unless") return check_file_not_contains_unless(check, root, target, ec);
    if (check.kind == "json-has-keys") return check_json_has_keys(check, root, target, ec);
    if (check.kind == "json-key-value") return check_json_key_value(check, root, target, ec);
    if (check.kind == "json-key-absent") return check_json_key_absent(check, root, target, ec);
    if (check.kind == "json-array-min-count") return check_json_array_min_count(check, root, target, ec);
    if (check.kind == "text-no-pollution") return check_text_no_pollution(check, root, target, ec);
    if (check.kind == "json-parses") return check_json_parses(check, root, target, ec);
    if (check.kind == "glob-min-count") return check_glob_min_count(check, root, target, ec);
    if (check.kind == "glob-contains") return check_glob_contains(check, root, target, ec);
    if (check.kind == "glob-not-contains") return check_glob_not_contains(check, root, target, ec);
    if (check.kind == "glob-absent") return check_glob_absent(check, root, target, ec);
    if (check.kind == "tree-not-contains") return check_tree_not_contains(check, root, target, ec);
    if (check.kind == "py-bucket-budget") return check_py_bucket_budget(check, root, target, ec);

    AuditCheckResult r;
    r.id = check.id;
    r.kind = check.kind;
    r.status = AuditStatus::DELEGATED;
    r.detail = "unsupported kind";
    return r;
}

}  // namespace

AuditReport audit_run(const std::vector<AuditCheck>& checks,
                      const std::string& root) {
    cache_reset();
    AuditReport report;
    report.manifest_ok = true;
    // Bounded parallel execution — 5-core budget, 4 workers max (thread_budget cap)
    // C++ primary, Python delegated remain delegated; hot path stays C-native.
    const size_t kMaxWorkers = 4;
    const size_t n = checks.size();
    if (n <= 64 || kMaxWorkers == 1) {
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
    std::vector<AuditCheckResult> results(n);
    std::vector<std::future<void>> futures;
    futures.reserve(kMaxWorkers);
    std::atomic<size_t> next_idx{0};
    auto worker = [&]() {
        size_t idx;
        while ((idx = next_idx.fetch_add(1)) < n) {
            results[idx] = run_check(checks[idx], root);
        }
    };
    for (size_t i = 0; i < kMaxWorkers; ++i) {
        futures.emplace_back(std::async(std::launch::async, worker));
    }
    for (auto& f : futures) f.wait();
    for (auto& r : results) {
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
        c.items = get_str("items");
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
        if (const JsonValue* v = item.get("exclude"))
            if (v->type == JsonValue::Type::Array)
                for (const auto& m : v->array)
                    if (m.type == JsonValue::Type::String)
                        c.exclude.push_back(m.string);
        if (const JsonValue* v = item.get("unless"))
            if (v->type == JsonValue::Type::Array)
                for (const auto& m : v->array)
                    if (m.type == JsonValue::Type::String)
                        c.unless.push_back(m.string);
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

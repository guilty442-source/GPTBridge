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
#include <mutex>
#include <sstream>
#include <thread>

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
    if (check.kind == "fail") {
        /* 匯出期已確認的違規（如 self-health 非法 test_targets）——
         * 確定性 FAIL，reason 攜帶人讀原因。 */
        r.status = AuditStatus::FAIL;
        r.detail = check.reason.empty() ? "declared fail row" : check.reason;
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
    if (check.kind == "file-not-contains-unless") {
        /* 條件式禁標：markers 任一命中時，檔案必須同時持有至少一個
         * unless 解禁標記，否則 FAIL。
         * 對齊 Python 複合條件「含 A 且不含 B → 錯」。 */
        std::string content;
        if (!read_file(target, &content)) {
            if (check.optional && !fs::exists(target, ec)) {
                r.status = AuditStatus::PASS;
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
                bool relieved = false;
                for (const auto& u : check.unless) {
                    const std::string un =
                        check.ignore_case ? to_lower(u) : u;
                    if (haystack.find(un) != std::string::npos) {
                        relieved = true;
                        break;
                    }
                }
                if (!relieved) {
                    r.status = AuditStatus::FAIL;
                    r.detail = "forbidden marker present: " + m;
                    return r;
                }
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
    if (check.kind == "json-key-value") {
        /* markers 格式 "<dotted.path><op><literal>"：
         *   "="  等值（Bool/Number/String 型別比對）
         *   "!=" 不等值（路徑存在時值必須不同；路徑缺席視為 PASS）
         *   "^=" 字串前綴
         *   ">=" 數值下限
         * 路徑逐層走 object；段名可帶 [KEY] 選取 object 陣列中
         * id==KEY 的元素；缺鍵／型別不符／檔案不可讀 → FAIL。*/
        std::string content;
        if (!read_file(target, &content)) {
            if (check.optional && !fs::exists(target, ec)) {
                r.status = AuditStatus::PASS;
                return r;
            }
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
        for (const auto& marker : check.markers) {
            size_t op_pos = std::string::npos;
            std::string op;
            for (const char* cand : {"!=", "^=", ">=", "="}) {
                const size_t pos = marker.find(cand);
                if (pos != std::string::npos) {
                    op_pos = pos; op = cand; break;
                }
            }
            if (op_pos == std::string::npos || op_pos == 0) {
                r.status = AuditStatus::FAIL;
                r.detail = "malformed json-key-value marker: " + marker;
                return r;
            }
            const std::string dotted = marker.substr(0, op_pos);
            const std::string literal = marker.substr(op_pos + op.size());
            const JsonValue* node = resolve_json_path(doc, dotted);
            bool ok = false;
            if (op == "!=") {
                /* 不等值斷言：路徑缺席 → 值不可能等於 literal → PASS；
                 * 路徑存在 → 型別化比對，等值即 FAIL。 */
                if (node == nullptr) {
                    ok = true;
                } else if (literal == "true" || literal == "false") {
                    ok = !(node->type == JsonValue::Type::Bool &&
                           node->boolean == (literal == "true"));
                } else if (node->type == JsonValue::Type::Number) {
                    try {
                        ok = node->number != std::stod(literal);
                    } catch (...) { ok = false; }
                } else if (node->type == JsonValue::Type::String) {
                    ok = node->string != literal;
                } else {
                    ok = true;
                }
            } else {
                if (node == nullptr) {
                    r.status = AuditStatus::FAIL;
                    r.detail = "missing json path: " + dotted;
                    return r;
                }
                if (op == "=") {
                    if (literal == "true" || literal == "false") {
                        ok = node->type == JsonValue::Type::Bool &&
                             node->boolean == (literal == "true");
                    } else if (node->type == JsonValue::Type::Number) {
                        try {
                            ok = node->number == std::stod(literal);
                        } catch (...) { ok = false; }
                    } else if (node->type == JsonValue::Type::String) {
                        ok = node->string == literal;
                    }
                } else if (op == "^=") {
                    ok = node->type == JsonValue::Type::String &&
                         node->string.size() >= literal.size() &&
                         node->string.compare(
                             0, literal.size(), literal) == 0;
                } else { /* ">=" */
                    if (node->type == JsonValue::Type::Number) {
                        try {
                            ok = node->number >= std::stod(literal);
                        } catch (...) { ok = false; }
                    }
                }
            }
            if (!ok) {
                r.status = AuditStatus::FAIL;
                r.detail = "json path check failed: " + marker;
                return r;
            }
        }
        r.status = AuditStatus::PASS;
        return r;
    }
    if (check.kind == "json-key-absent") {
        /* markers 為不得存在的 dotted 路徑（同 json-key-value 的路徑
         * 語法）；任一路徑可解析 → FAIL。缺席／null 皆視為不存在。*/
        std::string content;
        if (!read_file(target, &content)) {
            if (check.optional && !fs::exists(target, ec)) {
                r.status = AuditStatus::PASS;
                return r;
            }
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
        for (const auto& marker : check.markers) {
            const JsonValue* node = resolve_json_path(doc, marker);
            if (node != nullptr && node->type != JsonValue::Type::Null) {
                r.status = AuditStatus::FAIL;
                r.detail = "forbidden json key present: " + marker;
                return r;
            }
        }
        r.status = AuditStatus::PASS;
        return r;
    }
    if (check.kind == "json-array-min-count") {
        /* items 指定陣列路徑（空 → 文件根即陣列）；每個 object 元素以
         * markers 逐條作「欄位<op>literal」謂詞過濾（op 同
         * json-key-value：!= ^= >= =；欄位缺席時 != 視為成立）。
         * 全部謂詞成立者計 1；計數 >= min_count → PASS。 */
        std::string content;
        if (!read_file(target, &content)) {
            if (check.optional && !fs::exists(target, ec)) {
                r.status = AuditStatus::PASS;
                return r;
            }
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
        const JsonValue* arr =
            check.items.empty() ? &doc : resolve_json_path(doc, check.items);
        if (arr == nullptr || arr->type != JsonValue::Type::Array) {
            r.status = AuditStatus::FAIL;
            r.detail = "missing json array: " +
                (check.items.empty() ? std::string("<root>") : check.items);
            return r;
        }
        std::int64_t count = 0;
        for (const auto& elem : arr->array) {
            if (elem.type != JsonValue::Type::Object) continue;
            bool match = true;
            for (const auto& marker : check.markers) {
                size_t op_pos = std::string::npos;
                std::string op;
                for (const char* cand : {"!=", "^=", ">=", "="}) {
                    const size_t pos = marker.find(cand);
                    if (pos != std::string::npos) {
                        op_pos = pos; op = cand; break;
                    }
                }
                if (op_pos == std::string::npos || op_pos == 0) {
                    r.status = AuditStatus::FAIL;
                    r.detail = "malformed array predicate: " + marker;
                    return r;
                }
                const JsonValue* fv = resolve_json_path(
                    elem, marker.substr(0, op_pos));
                const std::string literal =
                    marker.substr(op_pos + op.size());
                bool ok;
                if (op == "!=") {
                    if (fv == nullptr ||
                        fv->type == JsonValue::Type::Null) {
                        ok = true;
                    } else if (literal == "true" || literal == "false") {
                        ok = !(fv->type == JsonValue::Type::Bool &&
                               fv->boolean == (literal == "true"));
                    } else if (fv->type == JsonValue::Type::Number) {
                        try { ok = fv->number != std::stod(literal); }
                        catch (...) { ok = false; }
                    } else if (fv->type == JsonValue::Type::String) {
                        ok = fv->string != literal;
                    } else {
                        ok = true;
                    }
                } else if (op == "^=") {
                    ok = fv != nullptr &&
                         fv->type == JsonValue::Type::String &&
                         fv->string.size() >= literal.size() &&
                         fv->string.compare(
                             0, literal.size(), literal) == 0;
                } else if (op == ">=") {
                    ok = fv != nullptr &&
                         fv->type == JsonValue::Type::Number;
                    if (ok) {
                        try { ok = fv->number >= std::stod(literal); }
                        catch (...) { ok = false; }
                    }
                } else { /* "=" */
                    if (fv == nullptr) {
                        ok = false;
                    } else if (literal == "true" || literal == "false") {
                        ok = fv->type == JsonValue::Type::Bool &&
                             fv->boolean == (literal == "true");
                    } else if (fv->type == JsonValue::Type::Number) {
                        try { ok = fv->number == std::stod(literal); }
                        catch (...) { ok = false; }
                    } else {
                        ok = fv->type == JsonValue::Type::String &&
                             fv->string == literal;
                    }
                }
                if (!ok) { match = false; break; }
            }
            if (match) ++count;
        }
        if (count >= check.min_count) {
            r.status = AuditStatus::PASS;
        } else {
            r.status = AuditStatus::FAIL;
            r.detail = "array " +
                (check.items.empty() ? std::string("<root>") : check.items) +
                " matched " + std::to_string(count) +
                " < min_count " + std::to_string(check.min_count);
        }
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
        const std::string pattern = u8_bytes(g.filename());
        std::int64_t count = 0;
        if (fs::is_directory(dir, ec)) {
            for (const auto& entry : fs::directory_iterator(dir, ec)) {
                if (entry.is_regular_file(ec) &&
                    wildcard_match(pattern, u8_bytes(entry.path().filename())))
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
    if (check.kind == "glob-contains") {
        /* 平層 glob：每個 marker 必須在「至少一個」命中檔案中出現
         * （union 語義——marker 可分散於不同檔，對齊 Python join-scan）。
         * 目錄缺席或檔案不可讀 → FAIL（fail-closed），optional 才豁免。*/
        const fs::path g = fs::u8path(check.glob);
        const fs::path dir = fs::u8path(root) / g.parent_path();
        const std::string pattern = u8_bytes(g.filename());
        if (!fs::is_directory(dir, ec)) {
            if (check.optional) {
                r.status = AuditStatus::PASS;
            } else {
                r.status = AuditStatus::FAIL;
                r.detail = "missing dir for glob: " + check.glob;
            }
            return r;
        }
        std::vector<bool> found(check.markers.size(), false);
        size_t found_count = 0;
        for (const auto& entry : fs::directory_iterator(dir, ec)) {
            if (found_count == check.markers.size()) break;
            if (!entry.is_regular_file(ec) ||
                !wildcard_match(pattern, u8_bytes(entry.path().filename())))
                continue;
            std::string content;
            if (!read_file(entry.path(), &content)) continue;
            const std::string haystack =
                check.ignore_case ? to_lower(content) : content;
            for (size_t i = 0; i < check.markers.size(); ++i) {
                if (found[i]) continue;
                const std::string needle =
                    check.ignore_case ? to_lower(check.markers[i])
                                      : check.markers[i];
                if (haystack.find(needle) != std::string::npos) {
                    found[i] = true;
                    ++found_count;
                }
            }
        }
        if (found_count == check.markers.size()) {
            r.status = AuditStatus::PASS;
        } else {
            for (size_t i = 0; i < check.markers.size(); ++i)
                if (!found[i]) {
                    r.detail = "marker absent from glob matches: " +
                               check.markers[i];
                    break;
                }
            r.status = AuditStatus::FAIL;
        }
        return r;
    }
    if (check.kind == "glob-not-contains") {
        /* 平層 glob（parent 目錄 + 檔名 pattern）：每個命中檔案都不得
         * 含任一 marker。目錄缺席 → FAIL（fail-closed），optional 才豁免。*/
        const fs::path g = fs::u8path(check.glob);
        const fs::path dir = fs::u8path(root) / g.parent_path();
        const std::string pattern = u8_bytes(g.filename());
        if (!fs::is_directory(dir, ec)) {
            if (check.optional) {
                r.status = AuditStatus::PASS;
            } else {
                r.status = AuditStatus::FAIL;
                r.detail = "missing dir for glob: " + check.glob;
            }
            return r;
        }
        std::string hit_path, hit_marker;
        for (const auto& entry : fs::directory_iterator(dir, ec)) {
            if (!entry.is_regular_file(ec) ||
                !wildcard_match(pattern, u8_bytes(entry.path().filename())))
                continue;
            std::string content;
            if (!read_file(entry.path(), &content)) continue;
            const std::string haystack =
                check.ignore_case ? to_lower(content) : content;
            for (const auto& m : check.markers) {
                const std::string needle =
                    check.ignore_case ? to_lower(m) : m;
                if (haystack.find(needle) != std::string::npos) {
                    hit_marker = m;
                    std::error_code rec;
                    hit_path = u8_bytes(
                        fs::relative(entry.path(), root, rec));
                    if (rec) hit_path = u8_bytes(entry.path().filename());
                    break;
                }
            }
            if (!hit_marker.empty()) break;
        }
        if (hit_marker.empty()) { r.status = AuditStatus::PASS; }
        else {
            r.status = AuditStatus::FAIL;
            r.detail = "forbidden marker '" + hit_marker + "' in " +
                       hit_path;
        }
        return r;
    }
    if (check.kind == "glob-absent") {
        /* 遞迴掃描 check.path 子樹（空字串 = 專案根）：檔名命中 check.glob
         * wildcard 即 FAIL。exclude 目錄名與 dotdir 於任意深度略過，
         * 對齊 Python os.walk + dirnames 修剪語義。*/
        const fs::path base = check.path.empty()
            ? fs::u8path(root)
            : fs::u8path(root) / fs::u8path(check.path);
        if (!fs::is_directory(base, ec)) {
            r.status = AuditStatus::PASS;   /* 無子樹 → 無命中 */
            return r;
        }
        auto excluded = [&](const fs::path& p) {
            const std::string name = u8_bytes(p.filename());
            if (!name.empty() && name[0] == '.') return true;
            for (const auto& ex : check.exclude)
                if (name == ex) return true;
            return false;
        };
        std::string hit;
        std::error_code iec;
        fs::recursive_directory_iterator it(
            base, fs::directory_options::skip_permission_denied, iec);
        const fs::recursive_directory_iterator dend;
        while (!iec && it != dend) {
            std::error_code sec;
            if (it->is_directory(sec)) {
                if (excluded(it->path())) it.disable_recursion_pending();
            } else if (it->is_regular_file(sec)) {
                if (wildcard_match(check.glob,
                                   u8_bytes(it->path().filename()))) {
                    std::error_code rec;
                    hit = u8_bytes(fs::relative(it->path(), base, rec));
                    if (rec) hit = u8_bytes(it->path().filename());
                    break;
                }
            }
            it.increment(iec);
        }
        if (hit.empty()) { r.status = AuditStatus::PASS; }
        else {
            r.status = AuditStatus::FAIL;
            r.detail = "forbidden file present: " + hit;
        }
        return r;
    }
    if (check.kind == "tree-not-contains") {
        /* glob-not-contains 的遞迴版：check.path 子樹（空字串=專案根）
         * 內檔名命中 check.glob 的每個檔案都不得含任一 marker。
         * exclude 目錄名與 dotdir 於任意深度略過（同 glob-absent）。
         * 子樹缺席 → FAIL（fail-closed），optional 才豁免。*/
        const fs::path base = check.path.empty()
            ? fs::u8path(root)
            : fs::u8path(root) / fs::u8path(check.path);
        if (!fs::is_directory(base, ec)) {
            if (check.optional) {
                r.status = AuditStatus::PASS;
            } else {
                r.status = AuditStatus::FAIL;
                r.detail = "missing subtree: " + check.path;
            }
            return r;
        }
        auto excluded = [&](const fs::path& p) {
            const std::string name = u8_bytes(p.filename());
            if (!name.empty() && name[0] == '.') return true;
            for (const auto& ex : check.exclude)
                if (name == ex) return true;
            return false;
        };
        std::string hit_path, hit_marker;
        std::error_code iec;
        fs::recursive_directory_iterator it(
            base, fs::directory_options::skip_permission_denied, iec);
        const fs::recursive_directory_iterator dend;
        while (!iec && it != dend && hit_marker.empty()) {
            std::error_code sec;
            if (it->is_directory(sec)) {
                if (excluded(it->path())) it.disable_recursion_pending();
            } else if (it->is_regular_file(sec)
                       && wildcard_match(
                           check.glob, u8_bytes(it->path().filename()))) {
                std::string content;
                if (read_file(it->path(), &content)) {
                    const std::string haystack =
                        check.ignore_case ? to_lower(content) : content;
                    for (const auto& m : check.markers) {
                        const std::string needle =
                            check.ignore_case ? to_lower(m) : m;
                        if (haystack.find(needle) != std::string::npos) {
                            hit_marker = m;
                            std::error_code rec;
                            hit_path = u8_bytes(
                                fs::relative(it->path(), base, rec));
                            if (rec)
                                hit_path =
                                    u8_bytes(it->path().filename());
                            break;
                        }
                    }
                }
            }
            it.increment(iec);
        }
        if (hit_marker.empty()) { r.status = AuditStatus::PASS; }
        else {
            r.status = AuditStatus::FAIL;
            r.detail = "forbidden marker in " + hit_path + ": " + hit_marker;
        }
        return r;
    }
    /* 未支援 kind：delegated（顯式移交，不靜默） */
    if (check.kind == "py-bucket-budget") {
        /* Python-minimization ratchet (native replacement for the retired
         * pytest gate).  check.path = baseline JSON carrying the embedded
         * "measurement" recipe: scan_roots / exclude_dirs /
         * exclude_file_substr / rules (ordered first-match substring map)
         * / fallback_bucket, plus the zero_targets + allowed_zones budgets.
         * Any bucket measuring above its budget -> FAIL (only-tighten). */
        std::string content;
        if (!read_file(target, &content)) {
            r.status = AuditStatus::FAIL;
            r.detail = "unreadable: " + check.path;
            return r;
        }
        JsonValue doc;
        try { doc = JsonParser(content).parse(); }
        catch (const JsonError&) {
            r.status = AuditStatus::FAIL;
            r.detail = "invalid json: " + check.path;
            return r;
        }
        const JsonValue* meas = doc.get("measurement");
        const JsonValue* zero = doc.get("zero_targets");
        const JsonValue* allowed = doc.get("allowed_zones");
        if (meas == nullptr || meas->type != JsonValue::Type::Object ||
            zero == nullptr || zero->type != JsonValue::Type::Object ||
            allowed == nullptr || allowed->type != JsonValue::Type::Object) {
            r.status = AuditStatus::FAIL;
            r.detail = "baseline lacks measurement/budgets: " + check.path;
            return r;
        }
        auto str_list = [](const JsonValue* node) {
            std::vector<std::string> out;
            if (node != nullptr && node->type == JsonValue::Type::Array)
                for (const auto& e : node->array)
                    if (e.type == JsonValue::Type::String)
                        out.push_back(e.string);
            return out;
        };
        const std::vector<std::string> roots =
            str_list(meas->get("scan_roots"));
        const std::vector<std::string> ex_dirs =
            str_list(meas->get("exclude_dirs"));
        const std::vector<std::string> ex_sub =
            str_list(meas->get("exclude_file_substr"));
        const JsonValue* rules = meas->get("rules");
        if (roots.empty() || rules == nullptr ||
            rules->type != JsonValue::Type::Object) {
            r.status = AuditStatus::FAIL;
            r.detail = "baseline measurement recipe incomplete";
            return r;
        }
        const JsonValue* fbv = meas->get("fallback_bucket");
        const std::string fallback =
            (fbv != nullptr && fbv->type == JsonValue::Type::String)
                ? fbv->string : "GENERAL_APP";

        std::map<std::string, std::pair<long long, long long>> actual;
        auto count_file = [&](const fs::path& fp) {
            /* 單一檔案歸類計數：exclude_file_substr 與 rules first-match
             * 語義與目錄掃描一致。 */
            if (fp.extension() != ".py") return;
            std::error_code rec;
            std::string rel = u8_bytes(fs::relative(fp, fs::u8path(root), rec));
            if (rec) return;
            for (auto& ch : rel)
                if (ch == '\\') ch = '/';
            rel = to_lower(rel);
            for (const auto& sub : ex_sub)
                if (rel.find(to_lower(sub)) != std::string::npos) return;
            std::string bucket = fallback;
            for (const auto& kv : rules->object) {
                bool hit = false;
                if (kv.second.type == JsonValue::Type::Array) {
                    for (const auto& pv : kv.second.array) {
                        if (pv.type == JsonValue::Type::String &&
                            rel.find(pv.string) != std::string::npos) {
                            hit = true; break;
                        }
                    }
                }
                if (hit) { bucket = kv.first; break; }
            }
            std::string fsrc;
            if (!read_file(fp, &fsrc)) return;
            const long long loc =
                static_cast<long long>(
                    std::count(fsrc.begin(), fsrc.end(), '\n')) +
                ((!fsrc.empty() && fsrc.back() != '\n') ? 1 : 0);
            auto& slot = actual[bucket];
            slot.first += 1;
            slot.second += loc;
        };
        for (const auto& rr : roots) {
            const fs::path base = fs::u8path(root) / fs::u8path(rr);
            if (fs::is_regular_file(base, ec)) {
                /* 檔案級 root（如 main-system/run.py）直接計量。 */
                count_file(base);
                continue;
            }
            if (!fs::is_directory(base, ec)) continue;
            std::error_code iec;
            fs::recursive_directory_iterator it(
                base, fs::directory_options::skip_permission_denied, iec);
            const fs::recursive_directory_iterator dend;
            while (!iec && it != dend) {
                std::error_code sec;
                if (it->is_directory(sec)) {
                    const std::string dn = u8_bytes(it->path().filename());
                    for (const auto& ex : ex_dirs)
                        if (dn == ex) {
                            it.disable_recursion_pending();
                            break;
                        }
                } else if (it->is_regular_file(sec)) {
                    count_file(it->path());
                }
                it.increment(iec);
            }
        }
        std::string viol;
        auto check_budget = [&](const JsonValue& budgets) {
            for (const auto& kv : budgets.object) {
                if (kv.second.type != JsonValue::Type::Object) continue;
                const JsonValue* bf = kv.second.get("files");
                const JsonValue* bl = kv.second.get("loc");
                const long long bf_v =
                    (bf && bf->type == JsonValue::Type::Number)
                        ? (long long)bf->number : -1;
                const long long bl_v =
                    (bl && bl->type == JsonValue::Type::Number)
                        ? (long long)bl->number : -1;
                const auto got = actual.find(kv.first);
                const long long af =
                    got == actual.end() ? 0 : got->second.first;
                const long long al =
                    got == actual.end() ? 0 : got->second.second;
                if (af > bf_v)
                    viol += " " + kv.first + " files " +
                            std::to_string(af) + ">" +
                            std::to_string(bf_v) + ";";
                if (al > bl_v)
                    viol += " " + kv.first + " loc " +
                            std::to_string(al) + ">" +
                            std::to_string(bl_v) + ";";
            }
        };
        check_budget(*zero);
        check_budget(*allowed);
        if (viol.empty()) {
            r.status = AuditStatus::PASS;
        } else {
            r.status = AuditStatus::FAIL;
            r.detail = "python-minimization ratchet violated:" + viol;
        }
        return r;
    }
    r.status = AuditStatus::DELEGATED;
    r.detail = "unsupported kind";
    return r;
}

}  // namespace

AuditReport audit_run(const std::vector<AuditCheck>& checks,
                      const std::string& root) {
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

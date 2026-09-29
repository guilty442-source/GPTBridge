/* parse.cpp — file-sorter stdout/protocol parsing (egui parse.rs port). */
#include "parse.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <functional>

namespace fsui {
namespace fsp {

namespace {

std::string trim_copy(std::string s) {
    auto not_ws = [](unsigned char c) { return !std::isspace(c); };
    s.erase(s.begin(), std::find_if(s.begin(), s.end(), not_ws));
    s.erase(std::find_if(s.rbegin(), s.rend(), not_ws).base(), s.end());
    return s;
}

void each_line(const std::string& text,
               const std::function<void(const std::string&)>& fn) {
    size_t start = 0;
    while (start <= text.size()) {
        size_t nl = text.find('\n', start);
        std::string line = nl == std::string::npos
            ? text.substr(start) : text.substr(start, nl - start);
        if (!line.empty() && line.back() == '\r') line.pop_back();
        fn(line);
        if (nl == std::string::npos) break;
        start = nl + 1;
    }
}

const JsonValue* member(const JsonValue& v, const char* key) {
    return v.get(key);
}

bool is_array(const JsonValue& v) { return v.type == JsonValue::Type::Array; }
bool is_object(const JsonValue& v) { return v.type == JsonValue::Type::Object; }

} // namespace

std::string trim(const std::string& s) { return trim_copy(s); }

bool try_parse(const std::string& text, JsonValue* out) {
    try {
        *out = gptbridge::jsonlite::JsonParser(text).parse();
        return true;
    } catch (...) {
        return false;
    }
}

bool parse_tool_json(const std::string& stdout_text, JsonValue* out) {
    std::string trimmed = trim_copy(stdout_text);
    if (!trimmed.empty() && (trimmed[0] == '{' || trimmed[0] == '[') &&
        try_parse(trimmed, out)) {
        return true;
    }
    bool found = false;
    each_line(stdout_text, [&](const std::string& line) {
        if (found) return;
        std::string l = trim_copy(line);
        if (l.empty() || (l[0] != '{' && l[0] != '[')) return;
        if (try_parse(l, out)) found = true;
    });
    return found;
}

bool parse_with_prefix(const std::string& stdout_text, const char* prefix,
                       JsonValue* out) {
    bool found = false;
    std::string p = prefix;
    each_line(stdout_text, [&](const std::string& line) {
        if (found) return;
        std::string l = trim_copy(line);
        if (l.rfind(p, 0) != 0) return;
        JsonValue v;
        if (try_parse(trim_copy(l.substr(p.size())), &v) &&
            (is_object(v) || is_array(v))) {
            *out = v;
            found = true;
        }
    });
    return found;
}

bool parse_with_prefixes(const std::string& stdout_text,
                         const char* const* prefixes, size_t n,
                         JsonValue* out) {
    if (parse_tool_json(stdout_text, out)) return true;
    for (size_t i = 0; i < n; ++i)
        if (parse_with_prefix(stdout_text, prefixes[i], out)) return true;
    return false;
}

bool is_direct_child_folder_name(const std::string& value) {
    std::string name = trim_copy(value);
    return !name.empty() && name != "." && name != ".." &&
           name.find('/') == std::string::npos &&
           name.find('\\') == std::string::npos &&
           name.find(':') == std::string::npos;
}

std::vector<std::string> parse_destination_folders(const std::string& stdout_text) {
    std::vector<std::string> out;
    each_line(stdout_text, [&](const std::string& line) {
        if (!out.empty()) return;
        std::string l = trim_copy(line);
        if (l.rfind(kFoldersPrefix, 0) != 0) return;
        JsonValue v;
        if (!try_parse(trim_copy(l.substr(std::string(kFoldersPrefix).size())), &v) ||
            !is_array(v)) {
            return;
        }
        for (const auto& item : v.array) {
            if (item.type != JsonValue::Type::String) continue;
            std::string s = trim_copy(item.string);
            if (is_direct_child_folder_name(s)) out.push_back(s);
        }
        std::sort(out.begin(), out.end());
        out.erase(std::unique(out.begin(), out.end()), out.end());
    });
    return out;
}

std::string normalize_path(const std::string& value) {
    std::string s = trim_copy(value);
    while (!s.empty() && (s.back() == '\\' || s.back() == '/')) s.pop_back();
    for (auto& c : s) {
        if (c == '/') c = '\\';
        c = (char)std::tolower((unsigned char)c);
    }
    return s;
}

bool parse_profile(const std::string& stdout_text, const std::string& target_dir,
                   JsonValue* out) {
    JsonValue parsed;
    const char* prefixes[] = {kProfilesPrefix};
    if (!parse_with_prefixes(stdout_text, prefixes, 1, &parsed)) return false;
    std::vector<const JsonValue*> profiles;
    if (is_array(parsed)) {
        for (const auto& p : parsed.array) profiles.push_back(&p);
    } else if (const JsonValue* arr = member(parsed, "profiles");
               arr && is_array(*arr)) {
        for (const auto& p : arr->array) profiles.push_back(&p);
    } else if (const JsonValue* p = member(parsed, "profile");
               p && is_object(*p)) {
        profiles.push_back(p);
    } else {
        profiles.push_back(&parsed);
    }
    std::string norm = normalize_path(target_dir);
    for (const JsonValue* p : profiles) {
        std::string path;
        for (const char* key : {"target_dir", "source_dir", "path"}) {
            const JsonValue* e = member(*p, key);
            if (e && e->type == JsonValue::Type::String) { path = e->string; break; }
        }
        if (normalize_path(path) == norm) {
            *out = *p;
            return true;
        }
    }
    return false;
}

bool parse_sort_plan(const std::string& stdout_text, JsonValue* out) {
    JsonValue parsed;
    if (!parse_with_prefixes(stdout_text, kPlanPrefixes, 2, &parsed)) return false;
    const JsonValue* plan = member(parsed, "plan");
    *out = (plan && is_object(*plan)) ? *plan : parsed;
    return true;
}

std::string plan_id(const JsonValue& plan) {
    for (const char* key : {"plan_id", "id"}) {
        const JsonValue* e = member(plan, key);
        if (e && e->type == JsonValue::Type::String)
            return trim_copy(e->string);
    }
    return "";
}

const JsonValue* plan_actions(const JsonValue& plan) {
    for (const char* key : {"operations", "actions"}) {
        const JsonValue* e = member(plan, key);
        if (e && is_array(*e)) return e;
    }
    return nullptr;
}

unsigned long long plan_action_count(const JsonValue& plan) {
    if (const JsonValue* a = plan_actions(plan)) return (unsigned long long)a->array.size();
    const JsonValue* e = member(plan, "action_count");
    if (e && e->type == JsonValue::Type::Number) return (unsigned long long)e->number;
    const JsonValue* sum = member(plan, "summary");
    if (sum && is_object(*sum)) {
        const JsonValue* ready = member(*sum, "ready");
        if (ready && ready->type == JsonValue::Type::Number)
            return (unsigned long long)ready->number;
    }
    return 0;
}

std::vector<std::string> parse_keywords(const std::string& value) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : value) {
        if (c == '\n' || c == ',') { /* ',' handled below via utf8 seq */
        }
        cur += c;
        if (c == '\n' || c == ',' ) {
            cur.pop_back();
            std::string t = trim_copy(cur);
            if (!t.empty()) out.push_back(t);
            cur.clear();
        }
    }
    /* also split on the UTF-8 ideographic comma 0xEF 0xBC 0x8C */
    std::vector<std::string> pass2;
    const std::string icomma = "\xEF\xBC\x8C";
    auto split_icomma = [&](const std::string& in) {
        size_t pos = 0;
        for (;;) {
            size_t at = in.find(icomma, pos);
            std::string part = trim_copy(
                at == std::string::npos ? in.substr(pos) : in.substr(pos, at - pos));
            if (!part.empty()) pass2.push_back(part);
            if (at == std::string::npos) break;
            pos = at + icomma.size();
        }
    };
    std::string tail = trim_copy(cur);
    if (!tail.empty()) out.push_back(tail);
    for (const auto& item : out) split_icomma(item);
    std::sort(pass2.begin(), pass2.end());
    pass2.erase(std::unique(pass2.begin(), pass2.end()), pass2.end());
    return pass2;
}

/* `- [程式碼] kw → folder` / `- [資料夾] kw → folder` lines. */
std::vector<KeywordRule> parse_keyword_rules(const std::string& stdout_text) {
    std::vector<KeywordRule> out;
    const std::string arrow = "\xE2\x86\x92"; /* → */
    size_t pos = 0;
    while (pos <= stdout_text.size()) {
        size_t nl = stdout_text.find('\n', pos);
        std::string line = trim_copy(
            nl == std::string::npos ? stdout_text.substr(pos)
                                    : stdout_text.substr(pos, nl - pos));
        pos = nl == std::string::npos ? stdout_text.size() + 1 : nl + 1;
        if (line.size() < 4 || line.rfind("- [", 0) != 0) continue;
        size_t close = line.find(']');
        if (close == std::string::npos) continue;
        std::string src = line.substr(3, close - 3);
        std::string rest = trim_copy(line.substr(close + 1));
        size_t ar = rest.find(arrow);
        if (ar == std::string::npos) continue;
        KeywordRule rule;
        rule.keyword = trim_copy(rest.substr(0, ar));
        rule.folder = trim_copy(rest.substr(ar + arrow.size()));
        rule.source = (src == "資料夾") ? "folder" : "custom";
        if (!rule.keyword.empty()) out.push_back(std::move(rule));
    }
    return out;
}

const char* category_label(const std::string& category) {
    if (category == "non_person_image_candidate") return "可能不含人物";
    if (category == "large_video_file") return "過大影片";
    if (category == "bad_video_file") return "影片問題";
    if (category == "similar_video_duplicate") return "相似影片";
    if (category == "similar_image_duplicate") return "相似圖片";
    return nullptr;
}

std::string format_file_size(double size) {
    char buf[32];
    if (size >= 1073741824.0) std::snprintf(buf, sizeof(buf), "%.1f GB", size / 1073741824.0);
    else if (size >= 1048576.0) std::snprintf(buf, sizeof(buf), "%.1f MB", size / 1048576.0);
    else if (size >= 1024.0) std::snprintf(buf, sizeof(buf), "%.1f KB", size / 1024.0);
    else std::snprintf(buf, sizeof(buf), "%llu B", (unsigned long long)size);
    return buf;
}

namespace {

std::string field_str(const JsonValue& v, const char* key) {
    const JsonValue* e = member(v, key);
    return (e && e->type == JsonValue::Type::String) ? e->string : "";
}

double field_num(const JsonValue& v, const char* key, bool* present = nullptr) {
    const JsonValue* e = member(v, key);
    bool ok = e && e->type == JsonValue::Type::Number;
    if (present) *present = ok;
    return ok ? e->number : 0.0;
}

} // namespace

std::string cleanup_file_summary(const JsonValue& file) {
    std::vector<std::string> parts;
    if (const JsonValue* cats = member(file, "categories"); cats && is_array(*cats)) {
        std::string labels;
        for (const auto& c : cats->array) {
            if (c.type != JsonValue::Type::String) continue;
            const char* label = category_label(c.string);
            std::string s = label ? label : c.string;
            if (!labels.empty()) labels += " / ";
            labels += s;
        }
        if (!labels.empty()) parts.push_back(labels);
    }
    bool has;
    double size = field_num(file, "size", &has);
    if (has) parts.push_back(format_file_size(size));
    double sim = field_num(file, "video_similarity", &has);
    if (has) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "相似度 %.0f%%", sim);
        parts.push_back(buf);
    }
    std::string sim_to = field_str(file, "similar_to");
    if (!sim_to.empty()) parts.push_back("相似於 " + sim_to);
    std::string issue = field_str(file, "video_issue");
    if (!issue.empty()) parts.push_back("影片狀態 " + issue);
    bool has_w, has_h;
    double w = field_num(file, "width", &has_w);
    double h = field_num(file, "height", &has_h);
    if (has_w && has_h) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "%llux%llu", (unsigned long long)w,
                      (unsigned long long)h);
        parts.push_back(buf);
    }
    double conf = field_num(file, "visual_recognition_confidence", &has);
    if (has) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "模型信心 %.0f%%", conf * 100.0);
        parts.push_back(buf);
    }
    if (parts.empty()) return "已列入清理候選";
    std::string out;
    for (size_t i = 0; i < parts.size(); ++i) {
        if (i) out += " - ";
        out += parts[i];
    }
    return out;
}

std::string progress_text(const JsonValue& progress) {
    std::string msg = field_str(progress, "message");
    if (!msg.empty()) return msg;
    std::string phase = field_str(progress, "phase");
    if (phase == "folder_scan")
        return "掃描資料夾 " + field_str(progress, "current_folder");
    if (phase == "image_analysis")
        return "分析圖片 " + field_str(progress, "current_file");
    if (phase == "video_analysis")
        return "分析影片 " + field_str(progress, "current_file");
    if (!phase.empty()) return phase;
    return "掃描中";
}

std::string format_run_output(const JsonValue& payload) {
    std::string out;
    std::string so = field_str(payload, "stdout");
    while (!so.empty() && (so.back() == '\n' || so.back() == '\r')) so.pop_back();
    out += so;
    std::string se = field_str(payload, "stderr");
    while (!se.empty() && (se.back() == '\n' || se.back() == '\r')) se.pop_back();
    if (!se.empty()) {
        if (!out.empty()) out += "\n\n";
        out += se;
    }
    return out;
}

} // namespace fsp
} // namespace fsui

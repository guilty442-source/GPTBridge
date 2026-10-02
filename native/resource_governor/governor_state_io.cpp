/* governor_state_io.cpp — Rules 讀取與狀態/日誌輸出實作。
 *
 * 由 main.cpp 依 A185 拆分而來；語義不變。
 */
#include "governor_state_io.h"

#include "governor_host.h"

namespace governor = gptbridge::governor;
namespace jsonlite = gptbridge::jsonlite;

namespace governor_host {

governor::RulesDoc load_rules_file(const fs::path& rules_path) {
    std::error_code ec;
    if (!fs::exists(rules_path, ec)) return governor::RulesDoc{};
    bool ok = false;
    const std::string text = read_file_text(rules_path, ok);
    if (!ok) {
        governor::RulesDoc doc;
        doc.error = "OSError: cannot read rules file";
        return doc;
    }
    auto parsed = governor::parse_rules(text);
    if (!parsed.has_value()) {
        governor::RulesDoc doc;
        doc.error = parsed.error();
        return doc;
    }
    return std::move(*parsed);
}

void emit_logs(const fs::path& log_path,
               std::span<const jsonlite::JsonValue> entries) {
    const std::string stamp = utc_now_iso();
    jsonlite::JsonValue stamp_value;
    stamp_value.type = jsonlite::JsonValue::Type::String;
    stamp_value.string = stamp;
    for (const auto& entry : entries) {
        jsonlite::JsonValue with_at = entry;
        if (with_at.type == jsonlite::JsonValue::Type::Object)
            with_at.object.insert(with_at.object.begin(), {"at", stamp_value});
        append_log(log_path, jsonlite::json_serialize(with_at));
    }
}

void write_state(const fs::path& state_path, const governor::Snapshot& snap,
                 bool stopped, const std::string& reason) {
    using namespace governor::detail;
    jsonlite::JsonValue body = governor::snapshot_to_json(snap);
    if (stopped) {
        body = jobj({{"stopped", jbool(true)}, {"reason", jstr(reason)}});
    }
    if (body.type == jsonlite::JsonValue::Type::Object)
        body.object.insert(body.object.begin(), {"at", jstr(utc_now_iso())});
    write_atomic(state_path, jsonlite::json_serialize(body));
}

void load_advisor_state(const fs::path& advisor_path,
                        governor::AdvisorState& state) {
    bool ok = false;
    const std::string text = read_file_text(advisor_path, ok);
    if (!ok) return;
    try {
        const jsonlite::JsonValue doc = jsonlite::JsonParser(text).parse();
        if (doc.type != jsonlite::JsonValue::Type::Object) return;
        auto str_of = [&](const char* key) -> std::string {
            const jsonlite::JsonValue* value = doc.get(key);
            return value != nullptr &&
                           value->type == jsonlite::JsonValue::Type::String
                       ? value->string
                       : std::string{};
        };
        /* 只復原 "applied"（native 新增鍵）；舊 Python 記錄無此鍵 →
         * 保持未接管，首次評估重新決定（fail-closed 到 rules.mode）。
         * assist_anchor：手動協助錨點——重啟後若 configured_mode 已換
         * （anchor 不符），cycle_init 視 applied 為過期不落檔。 */
        state.applied_mode = str_of("applied");
        state.assist_anchor = str_of("assist_anchor");
        state.last_target = str_of("target");
        if (const jsonlite::JsonValue* streak = doc.get("streak");
            streak != nullptr &&
            streak->type == jsonlite::JsonValue::Type::Number)
            state.streak = static_cast<int>(streak->number);
        if (const jsonlite::JsonValue* switched = doc.get("last_switch_at");
            switched != nullptr &&
            switched->type == jsonlite::JsonValue::Type::Number)
            state.last_switch_unix = switched->number;
    } catch (const jsonlite::JsonError&) {
    }
}

void write_advisor_state(const fs::path& advisor_path,
                         const governor::Snapshot& snap) {
    if (!snap.advisor.has_value()) return;
    using namespace governor::detail;
    jsonlite::JsonValue body = *snap.advisor;
    if (body.type == jsonlite::JsonValue::Type::Object)
        body.object.insert(body.object.begin(), {"at", jstr(utc_now_iso())});
    write_atomic(advisor_path, jsonlite::json_serialize(body));
}

void append_mode_audit(const fs::path& audit_path,
                       const governor::Snapshot& snap) {
    if (!snap.mode_audit.has_value()) return;
    using namespace governor::detail;
    jsonlite::JsonValue row = *snap.mode_audit;
    if (row.type == jsonlite::JsonValue::Type::Object)
        row.object.emplace_back("timestamp", jstr(utc_now_iso()));
    append_log(audit_path, jsonlite::json_serialize(row));
}

}  // namespace governor_host

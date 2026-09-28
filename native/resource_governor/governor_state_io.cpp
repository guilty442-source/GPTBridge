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

}  // namespace governor_host

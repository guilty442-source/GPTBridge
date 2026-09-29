/* parse.h — file-sorter stdout/protocol parsing (egui parse.rs port). */
#ifndef GPTBRIDGE_FSUI_PARSE_H
#define GPTBRIDGE_FSUI_PARSE_H

#include <string>
#include <vector>

#include "jsonlite.h"

namespace fsui {
namespace fsp {

using gptbridge::jsonlite::JsonValue;

constexpr const char* kFoldersPrefix = "FILE_SORTER_FOLDERS_JSON=";
constexpr const char* kPlanPrefixes[] = {
    "FILE_SORTER_PREVIEW_JSON=", "FILE_SORTER_PLAN_JSON="};
constexpr const char* kProfilesPrefix = "FILE_SORTER_PROFILES_JSON=";

bool try_parse(const std::string& text, JsonValue* out);
bool parse_tool_json(const std::string& stdout_text, JsonValue* out);
bool parse_with_prefix(const std::string& stdout_text, const char* prefix,
                       JsonValue* out);
bool parse_with_prefixes(const std::string& stdout_text,
                         const char* const* prefixes, size_t n,
                         JsonValue* out);

bool is_direct_child_folder_name(const std::string& value);
std::vector<std::string> parse_destination_folders(const std::string& stdout_text);
std::string normalize_path(const std::string& value);
bool parse_profile(const std::string& stdout_text, const std::string& target_dir,
                   JsonValue* out);
bool parse_sort_plan(const std::string& stdout_text, JsonValue* out);
std::string plan_id(const JsonValue& plan);
const JsonValue* plan_actions(const JsonValue& plan); /* array or null */
unsigned long long plan_action_count(const JsonValue& plan);
std::vector<std::string> parse_keywords(const std::string& value);

const char* category_label(const std::string& category);
std::string format_file_size(double size);
std::string cleanup_file_summary(const JsonValue& file);
std::string progress_text(const JsonValue& progress);
std::string format_run_output(const JsonValue& payload);
std::string trim(const std::string& s);

} // namespace fsp
} // namespace fsui
#endif /* GPTBRIDGE_FSUI_PARSE_H */

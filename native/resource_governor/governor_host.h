/* governor_host.h — 宿主層字串/檔案/時間工具。
 *
 * 由 main.cpp 依 A185（≤500 effective 行）拆分而來；內容原樣平移，
 * 語義不變：UTF-8↔UTF-16 轉換、CLI 數值解析、根目錄探尋、原子寫入、
 * 日誌附加、UTC/monotonic 時間。
 */
#pragma once

#include <filesystem>
#include <string>
#include <vector>

namespace governor_host {

namespace fs = std::filesystem;

std::string narrow_str(const std::wstring& text);
std::string narrow_str(const wchar_t* text);
std::string path_u8(const fs::path& path);
std::wstring widen_str(const std::string& text);
std::vector<std::string> argv_utf8();
bool parse_number(const std::string& text, double& out);
bool parse_int(const std::string& text, int& out);
fs::path discover_root(const std::string& override_root);
std::string read_file_text(const fs::path& path, bool& ok);
bool write_atomic(const fs::path& path, const std::string& text);
void append_log(const fs::path& path, const std::string& line);
std::string utc_now_iso();
double monotonic_seconds();

}  // namespace governor_host

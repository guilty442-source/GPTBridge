/* governor_host.cpp — 宿主層字串/檔案/時間工具實作。
 *
 * 由 main.cpp 依 A185 拆分而來；語義不變。
 */
#include "governor_host.h"

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <shellapi.h>

#include <cstdio>
#include <fstream>
#include <iterator>
#include <sstream>

#pragma comment(lib, "shell32.lib")

namespace governor_host {

std::string narrow_str(const std::wstring& text) {
    if (text.empty()) return {};
    const int needed =
        ::WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<std::size_t>(needed > 0 ? needed - 1 : 0), '\0');
    if (needed > 0)
        ::WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, out.data(), needed, nullptr,
                              nullptr);
    return out;
}

std::string narrow_str(const wchar_t* text) {
    if (text == nullptr || *text == L'\0') return {};
    return narrow_str(std::wstring(text));
}

/* fs::path → UTF-8（C++20 的 u8string() 為 char8_t，避免轉型噪音）。 */
std::string path_u8(const fs::path& path) { return narrow_str(path.wstring()); }

std::wstring widen_str(const std::string& text) {
    if (text.empty()) return L"";
    const int needed = ::MultiByteToWideChar(CP_UTF8, 0, text.c_str(), -1, nullptr, 0);
    std::wstring out(static_cast<std::size_t>(needed > 0 ? needed - 1 : 0), L'\0');
    if (needed > 0)
        ::MultiByteToWideChar(CP_UTF8, 0, text.c_str(), -1, out.data(), needed);
    return out;
}

std::vector<std::string> argv_utf8() {
    int argc = 0;
    LPWSTR* raw = ::CommandLineToArgvW(::GetCommandLineW(), &argc);
    std::vector<std::string> out;
    if (raw != nullptr) {
        for (int i = 0; i < argc; ++i) out.push_back(narrow_str(raw[i]));
        ::LocalFree(raw);
    }
    return out;
}

bool parse_number(const std::string& text, double& out) {
    try {
        std::size_t used = 0;
        out = std::stod(text, &used);
        return used == text.size();
    } catch (...) {
        return false;
    }
}

bool parse_int(const std::string& text, int& out) {
    try {
        std::size_t used = 0;
        out = std::stoi(text, &used);
        return used == text.size();
    } catch (...) {
        return false;
    }
}

/* 根目錄探尋：--root 覆寫，否則自 exe 位置向上找 rules.json。 */
fs::path discover_root(const std::string& override_root) {
    if (!override_root.empty()) return fs::path(widen_str(override_root));
    wchar_t exe[MAX_PATH * 4]{};
    ::GetModuleFileNameW(nullptr, exe, static_cast<DWORD>(std::size(exe)));
    fs::path dir = fs::path(exe).parent_path();
    for (int depth = 0; depth < 8; ++depth) {
        if (fs::exists(dir / "main-system" / "config" / "resource-governor-rules.json"))
            return dir;
        if (!dir.has_parent_path()) break;
        dir = dir.parent_path();
    }
    return fs::current_path();
}

std::string read_file_text(const fs::path& path, bool& ok) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        ok = false;
        return {};
    }
    std::ostringstream ss;
    ss << input.rdbuf();
    ok = true;
    return ss.str();
}

bool write_atomic(const fs::path& path, const std::string& text) {
    std::error_code ec;
    fs::create_directories(path.parent_path(), ec);
    const fs::path tmp = path.wstring() + L".tmp";
    {
        std::ofstream output(tmp, std::ios::binary | std::ios::trunc);
        if (!output) return false;
        output << text;
        output.flush();
        if (!output) return false;
    }
    fs::rename(tmp, path, ec);
    if (ec) {
        fs::remove(tmp, ec);
        return false;
    }
    return true;
}

void append_log(const fs::path& path, const std::string& line) {
    std::error_code ec;
    fs::create_directories(path.parent_path(), ec);
    std::ofstream output(path, std::ios::binary | std::ios::app);
    if (output) output << line << "\n";
}

std::string utc_now_iso() {
    SYSTEMTIME st{};
    ::GetSystemTime(&st);
    char buffer[64]{};
    std::snprintf(buffer, sizeof(buffer), "%04u-%02u-%02uT%02u:%02u:%02u.%03u+00:00",
                  st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond,
                  st.wMilliseconds);
    return buffer;
}

double monotonic_seconds() {
    return static_cast<double>(::GetTickCount64()) / 1000.0;
}

}  // namespace governor_host

/* utf8.h — UTF-8 <-> UTF-16 conversion helpers (Win32, no deps). */
#ifndef GPTBRIDGE_FSUI_UTF8_H
#define GPTBRIDGE_FSUI_UTF8_H

#include <string>
#include <windows.h>

namespace fsui {

inline std::wstring widen(const std::string& s) {
    if (s.empty()) return L"";
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring out(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), out.data(), n);
    return out;
}

inline std::string narrow(const std::wstring& s) {
    if (s.empty()) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0, nullptr, nullptr);
    std::string out(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, s.data(), (int)s.size(), out.data(), n, nullptr, nullptr);
    return out;
}

/* Text of a Win32 control as UTF-8. */
inline std::string ctrl_text(HWND hwnd) {
    int len = GetWindowTextLengthW(hwnd);
    std::wstring w(len, L'\0');
    GetWindowTextW(hwnd, w.data(), len + 1);
    return narrow(w);
}

inline void set_text(HWND hwnd, const std::string& utf8) {
    SetWindowTextW(hwnd, widen(utf8).c_str());
}

} // namespace fsui
#endif /* GPTBRIDGE_FSUI_UTF8_H */

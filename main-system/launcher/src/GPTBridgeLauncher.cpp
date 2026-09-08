#pragma comment(linker, "/SUBSYSTEM:WINDOWS")

#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#define WIN32_LEAN_AND_MEAN

#include <windows.h>
#include <cstdio>
#include <string>
#include <vector>

static const wchar_t* kAppDisplayName =
    L"\x5C08\x6848\x7A0B\x5F0F\x5EAB";

static const wchar_t* kMsgNotInstalled =
    L"\x7A0B\x5F0F\x5EAB\x555F\x52D5\x5668\x5C1A\x672A\x5B89\x88DD"
    L"\xFF0C\x8ACB\x57F7\x884C launcher\\scripts\\install.ps1\x3002";

static const wchar_t* kMsgMissing =
    L"\x627E\x4E0D\x5230\x7A0B\x5F0F\x5EAB\x6216\x555F\x52D5\x6A21\x7D44"
    L"\xFF0C\x8ACB\x91CD\x65B0\x5B89\x88DD\x555F\x52D5\x5668\x3002";

static std::wstring Quote(const std::wstring& value)
{
    std::wstring escaped;
    for (wchar_t ch : value) {
        if (ch == L'"') {
            escaped += L'\\';
        }
        escaped += ch;
    }
    return L"\"" + escaped + L"\"";
}

static bool FileExists(const wchar_t* path)
{
    DWORD attr = GetFileAttributesW(path);
    return attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY);
}

static bool DirExists(const wchar_t* path)
{
    DWORD attr = GetFileAttributesW(path);
    return attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY);
}

static std::wstring ReadRootFile(const std::wstring& rootFile)
{
    HANDLE file = CreateFileW(rootFile.c_str(), GENERIC_READ,
                              FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                              FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        return L"";
    }
    LARGE_INTEGER size = {};
    if (!GetFileSizeEx(file, &size) || size.QuadPart > 1048576) {
        CloseHandle(file);
        return L"";
    }
    std::vector<BYTE> buffer(static_cast<size_t>(size.QuadPart));
    DWORD read = 0;
    BOOL ok = ReadFile(file, buffer.data(), static_cast<DWORD>(buffer.size()),
                       &read, nullptr);
    CloseHandle(file);
    if (!ok) {
        return L"";
    }
    int wide = MultiByteToWideChar(CP_UTF8, 0,
                                   reinterpret_cast<LPCCH>(buffer.data()),
                                   static_cast<int>(read), nullptr, 0);
    if (wide <= 0) {
        return L"";
    }
    std::wstring text(static_cast<size_t>(wide), L'\0');
    MultiByteToWideChar(CP_UTF8, 0,
                        reinterpret_cast<LPCCH>(buffer.data()),
                        static_cast<int>(read), &text[0], wide);
    if (!text.empty() && text[0] == L'\ufeff') {
        text = text.substr(1);
    }
    size_t start = text.find_first_not_of(L" \t\r\n");
    size_t end = text.find_last_not_of(L" \t\r\n");
    if (start == std::wstring::npos) {
        return L"";
    }
    return text.substr(start, end - start + 1);
}

static std::wstring GetLocalAppData()
{
    wchar_t buffer[MAX_PATH] = {};
    DWORD len = GetEnvironmentVariableW(L"LOCALAPPDATA", buffer, MAX_PATH);
    if (len == 0 || len >= MAX_PATH) {
        return L"";
    }
    return std::wstring(buffer);
}

static std::wstring GetLastErrorResource()
{
    wchar_t* message = nullptr;
    DWORD flags = FORMAT_MESSAGE_ALLOCATE_BUFFER |
                  FORMAT_MESSAGE_FROM_SYSTEM |
                  FORMAT_MESSAGE_IGNORE_INSERTS;
    DWORD len = FormatMessageW(flags, nullptr, GetLastError(), 0,
                               reinterpret_cast<wchar_t*>(&message), 0, nullptr);
    std::wstring text;
    if (message) {
        text.assign(message, len);
        LocalFree(message);
    }
    return text;
}

static void ShowError(const std::wstring& message)
{
    std::wstring localAppData = GetLocalAppData();
    if (localAppData.empty()) {
        return;
    }

    std::wstring logDir = localAppData + L"\\GPTBridgeLauncher\\logs";
    CreateDirectoryW(logDir.c_str(), nullptr);
    std::wstring logPath = logDir + L"\\launcher.log";

    SYSTEMTIME st;
    GetLocalTime(&st);
    wchar_t timeBuf[64];
    swprintf_s(timeBuf, L"[%04d-%02d-%02d %02d:%02d:%02d.%03d] ",
               st.wYear, st.wMonth, st.wDay,
               st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);

    std::wstring full = std::wstring(timeBuf) + kAppDisplayName + L" ERROR: " + message + L"\r\n";

    int utf8Size = WideCharToMultiByte(CP_UTF8, 0, full.c_str(), -1,
                                       nullptr, 0, nullptr, nullptr);
    if (utf8Size <= 0) {
        return;
    }
    std::vector<char> utf8(static_cast<size_t>(utf8Size));
    WideCharToMultiByte(CP_UTF8, 0, full.c_str(), -1,
                        utf8.data(), utf8Size, nullptr, nullptr);

    HANDLE file = CreateFileW(logPath.c_str(), FILE_APPEND_DATA | FILE_WRITE_DATA,
                              FILE_SHARE_READ, nullptr, OPEN_ALWAYS,
                              FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        return;
    }
    SetFilePointer(file, 0, nullptr, FILE_END);
    DWORD written;
    WriteFile(file, utf8.data(), static_cast<DWORD>(utf8Size - 1),
              &written, nullptr);
    CloseHandle(file);
}

static int LaunchHost(const std::wstring& projectRoot,
                      const std::wstring& launchScript)
{
    SetEnvironmentVariableW(L"ELECTRON_RUN_AS_NODE", nullptr);

    std::wstring commandLine =
        L"powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden"
        L" -File " + Quote(launchScript) +
        L" -ProjectRoot " + Quote(projectRoot);

    STARTUPINFOW startupInfo = {};
    startupInfo.cb = sizeof(STARTUPINFOW);
    startupInfo.dwFlags = STARTF_USESHOWWINDOW;
    startupInfo.wShowWindow = SW_HIDE;

    PROCESS_INFORMATION processInfo = {};

    std::vector<wchar_t> cmdLine(commandLine.begin(), commandLine.end());
    cmdLine.push_back(L'\0');

    if (!CreateProcessW(nullptr, cmdLine.data(), nullptr, nullptr, FALSE,
                        CREATE_NO_WINDOW, nullptr, projectRoot.c_str(),
                        &startupInfo, &processInfo)) {
        ShowError(GetLastErrorResource());
        return 1;
    }

    CloseHandle(processInfo.hThread);
    CloseHandle(processInfo.hProcess);
    return 0;
}

int WINAPI wWinMain(HINSTANCE, HINSTANCE, LPWSTR, int)
{
    std::wstring localAppData = GetLocalAppData();
    if (localAppData.empty()) {
        ShowError(kMsgNotInstalled);
        return 1;
    }

    std::wstring rootFile =
        localAppData + L"\\GPTBridgeLauncher\\config\\root.txt";
    if (!FileExists(rootFile.c_str())) {
        ShowError(kMsgNotInstalled);
        return 1;
    }

    std::wstring projectRoot = ReadRootFile(rootFile);
    if (projectRoot.empty()) {
        ShowError(kMsgNotInstalled);
        return 1;
    }

    std::wstring launchScript =
        projectRoot + L"\\launcher\\scripts\\start.ps1";
    if (!DirExists(projectRoot.c_str()) || !FileExists(launchScript.c_str())) {
        ShowError(kMsgMissing);
        return 1;
    }

    return LaunchHost(projectRoot, launchScript);
}
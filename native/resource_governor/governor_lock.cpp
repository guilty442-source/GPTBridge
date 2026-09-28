/* governor_lock.cpp — 行程鎖與自我身分實作。
 *
 * 由 main.cpp 依 A185 拆分而來；語義不變。
 */
#include "governor_lock.h"

#include <rpc.h>
#include <rpcdce.h>
#include <tlhelp32.h>

#include <chrono>
#include <cstdio>
#include <iterator>
#include <map>
#include <vector>

#include "governor_host.h"
#include "jsonlite.h"
#include "resource_governor.h"

#pragma comment(lib, "rpcrt4.lib")
#pragma comment(lib, "advapi32.lib")

namespace jsonlite = gptbridge::jsonlite;

namespace governor_host {

/* ---------------- 鎖（與 process_lock.py 等價） ---------------- */
bool pid_alive(unsigned long pid) {
    HANDLE proc = ::OpenProcess(0x1000 /*QUERY_LIMITED_INFORMATION*/, FALSE, pid);
    if (proc != nullptr) {
        DWORD code = 0;
        ::GetExitCodeProcess(proc, &code);
        ::CloseHandle(proc);
        return code == 259 /*STILL_ACTIVE*/;
    }
    return ::GetLastError() != 87 /*ERROR_INVALID_PARAMETER*/;
}

bool owner_alive(const fs::path& lock_path) {
    std::error_code ec;
    const auto mtime = fs::last_write_time(lock_path, ec);
    double age = 0.0;
    if (!ec) {
        const auto now = fs::file_time_type::clock::now();
        age = std::chrono::duration<double>(now - mtime).count();
        if (age < 0) age = 0;
    } else {
        return false;
    }
    bool ok = false;
    const std::string text = read_file_text(lock_path, ok);
    if (ok) {
        try {
            jsonlite::JsonValue root =
                jsonlite::JsonParser(text).parse();
            if (const jsonlite::JsonValue* pid_value = root.get("pid");
                pid_value != nullptr &&
                pid_value->type == jsonlite::JsonValue::Type::Number) {
                const auto pid =
                    static_cast<unsigned long>(pid_value->number);
                return pid_alive(pid);
            }
        } catch (const jsonlite::JsonError&) {
        }
    }
    return age < 10.0;
}

bool lock_is_active(const fs::path& lock_path) {
    std::error_code ec;
    return fs::exists(lock_path, ec) && owner_alive(lock_path);
}

std::string LockGuard::make_token() {
    UUID uuid{};
    ::UuidCreate(&uuid);
    RPC_WSTR text = nullptr;
    std::string out;
    if (::UuidToStringW(&uuid, &text) == RPC_S_OK && text != nullptr) {
        out = narrow_str(reinterpret_cast<const wchar_t*>(text));
        ::RpcStringFreeW(&text);
    }
    if (out.empty()) {
        char buffer[64]{};
        std::snprintf(buffer, sizeof(buffer), "%lu-%llu-%u",
                      ::GetCurrentProcessId(),
                      static_cast<unsigned long long>(::GetTickCount64()),
                      static_cast<unsigned>(::GetTickCount()));
        out = buffer;
    }
    for (char& ch : out)
        if (ch == '-') ch = 'x';
    return out;
}

bool LockGuard::acquire(const fs::path& lock_path) {
    path = lock_path;
    std::error_code ec;
    fs::create_directories(path.parent_path(), ec);
    for (int attempt = 0; attempt < 2; ++attempt) {
        HANDLE created = ::CreateFileW(
            path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW,
            FILE_ATTRIBUTE_NORMAL, nullptr);
        if (created != INVALID_HANDLE_VALUE) {
            handle = created;
            token = make_token();
            char payload[160]{};
            const int len = std::snprintf(
                payload, sizeof(payload), "{\"pid\": %lu, \"token\": \"%s\"}",
                ::GetCurrentProcessId(), token.c_str());
            DWORD written = 0;
            ::WriteFile(handle, payload, static_cast<DWORD>(len), &written,
                        nullptr);
            ::FlushFileBuffers(handle);
            owned = true;
            return true;
        }
        if (owner_alive(path) || attempt == 1) return false;
        fs::remove(path, ec);
    }
    return false;
}

LockGuard::~LockGuard() {
    if (handle != nullptr) {
        ::CloseHandle(handle);
        handle = nullptr;
    }
    if (owned) {
        bool ok = false;
        const std::string text = read_file_text(path, ok);
        if (ok) {
            try {
                jsonlite::JsonValue root =
                    jsonlite::JsonParser(text).parse();
                if (const jsonlite::JsonValue* token_value = root.get("token");
                    token_value != nullptr &&
                    token_value->type == jsonlite::JsonValue::Type::String &&
                    token_value->string == token) {
                    std::error_code ec;
                    fs::remove(path, ec);
                }
            } catch (const jsonlite::JsonError&) {
            }
        }
        owned = false;
    }
}

/* ---------------- 自我身分 ---------------- */
std::string self_account() {
    HANDLE token = nullptr;
    if (!::OpenProcessToken(::GetCurrentProcess(), TOKEN_QUERY, &token))
        return {};
    std::string account;
    DWORD needed = 0;
    ::GetTokenInformation(token, TokenUser, nullptr, 0, &needed);
    std::vector<std::uint8_t> buffer(needed > 0 ? needed : 1);
    if (::GetTokenInformation(token, TokenUser, buffer.data(), needed, &needed)) {
        const TOKEN_USER* user = reinterpret_cast<const TOKEN_USER*>(buffer.data());
        wchar_t name[256]{};
        wchar_t domain[256]{};
        DWORD name_len = static_cast<DWORD>(std::size(name));
        DWORD domain_len = static_cast<DWORD>(std::size(domain));
        SID_NAME_USE use = SidTypeUnknown;
        if (::LookupAccountSidW(nullptr, user->User.Sid, name, &name_len, domain,
                                &domain_len, &use)) {
            account = narrow_str(domain);
            if (!account.empty()) account += "\\";
            account += narrow_str(name);
        }
    }
    ::CloseHandle(token);
    return account;
}

std::set<int> self_tree() {
    std::set<int> tree;
    const DWORD self = ::GetCurrentProcessId();
    tree.insert(static_cast<int>(self));
    HANDLE snap =
        ::CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return tree;
    std::map<DWORD, DWORD> parent_of;
    PROCESSENTRY32W entry{};
    entry.dwSize = sizeof(entry);
    if (::Process32FirstW(snap, &entry)) {
        do {
            parent_of[entry.th32ProcessID] = entry.th32ParentProcessID;
        } while (::Process32NextW(snap, &entry));
    }
    ::CloseHandle(snap);
    DWORD cursor = self;
    for (int depth = 0; depth < 64; ++depth) {
        auto it = parent_of.find(cursor);
        if (it == parent_of.end() || it->second == 0 || it->second == cursor) break;
        tree.insert(static_cast<int>(it->second));
        cursor = it->second;
    }
    return tree;
}

bool env_disabled() {
    wchar_t buffer[16]{};
    DWORD len = ::GetEnvironmentVariableW(L"GPTBRIDGE_GOVERNOR_DISABLE", buffer,
                                          static_cast<DWORD>(std::size(buffer)));
    if (len == 0 || len >= std::size(buffer)) return false;
    std::string value = gptbridge::governor::to_lower(narrow_str(buffer));
    return value == "1" || value == "true" || value == "yes";
}

}  // namespace governor_host

/* governor_lock.h — 行程鎖與自我身分（與 process_lock.py 等價）。
 *
 * 由 main.cpp 依 A185（≤500 effective 行）拆分而來；內容原樣平移，
 * 語義不變：CreateFileW CREATE_NEW 鎖、pid/token 擁有者驗證、自身
 * 行程樹與帳號、環境變數 kill switch。
 */
#pragma once

#include <filesystem>
#include <set>
#include <string>

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

namespace governor_host {

namespace fs = std::filesystem;

bool pid_alive(unsigned long pid);
bool owner_alive(const fs::path& lock_path);
bool lock_is_active(const fs::path& lock_path);

struct LockGuard {
    fs::path path;
    HANDLE handle = nullptr;
    std::string token;
    bool owned = false;

    static std::string make_token();
    bool acquire(const fs::path& lock_path);
    ~LockGuard();
    LockGuard() = default;
    LockGuard(const LockGuard&) = delete;
    LockGuard& operator=(const LockGuard&) = delete;
};

std::string self_account();
std::set<int> self_tree();
bool env_disabled();

}  // namespace governor_host

/* governor_tasks.h — 排程任務／登入啟動註冊與解除。
 *
 * 由 main.cpp 依 A185（≤500 effective 行）拆分而來；內容原樣平移，
 * 語義不變：schtasks XML（UTF-16）註冊、Run 登錄值、watch 啟動參數
 * 建構（feature_args＋--root）。
 */
#pragma once

#include <string>
#include <vector>

#include "governor_cli.h"

namespace governor_host {

/* 命令列安全引號拼接（schtasks/Run 值/CreateProcess 共用）。 */
std::string join_args(std::span<const std::string> args);

/* watch 模式完整啟動參數（含 exe 路徑的版本供 spawn/Run 值使用）。 */
std::vector<std::string> launch_parts(const CliOptions& opt,
                                      const fs::path& rules_path,
                                      const fs::path& default_rules,
                                      const fs::path& root);
std::vector<std::string> spawn_parts(const CliOptions& opt,
                                     const fs::path& rules_path,
                                     const fs::path& default_rules,
                                     const fs::path& root);

int uninstall_task();
int uninstall_logon();
int install_task(const CliOptions& opt, const fs::path& root,
                 const fs::path& rules_path, const fs::path& default_rules);
int install_logon(const CliOptions& opt, const fs::path& root,
                  const fs::path& rules_path, const fs::path& default_rules);

}  // namespace governor_host

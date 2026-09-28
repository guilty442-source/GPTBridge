/* governor_lifecycle.h — stop/status/start 與 --once/--watch 執行迴圈。
 *
 * 由 main.cpp 依 A185（≤500 effective 行）拆分而來；內容原樣平移，
 * 語義不變：鎖檔 pid 終止、狀態 JSON 回讀、detached spawn、週期
 * govern_once 迴圈（try/catch 記 cycle-error、Ctrl+C 優雅停止）。
 */
#pragma once

#include "governor_cli.h"
#include "resource_governor.h"

namespace governor_host {

gptbridge::governor::CycleContext make_cycle_ctx(const fs::path& root);

void print_status(const fs::path& lock_file, const fs::path& state_file,
                  const fs::path& log_file);
int stop_governor(const fs::path& lock_file);
int start_governor(const CliOptions& opt, const fs::path& root,
                   const fs::path& rules_path, const fs::path& default_rules,
                   const fs::path& lock_file, const fs::path& state_file,
                   const fs::path& log_file);
int run_once(const gptbridge::governor::GovernorConfig& config,
             gptbridge::governor::CycleContext& ctx,
             gptbridge::governor::IEngine& engine, const fs::path& rules_path,
             const fs::path& state_file, const fs::path& log_file);
int run_watch(const gptbridge::governor::GovernorConfig& config,
              gptbridge::governor::CycleContext& ctx,
              gptbridge::governor::IEngine& engine, const fs::path& rules_path,
              const fs::path& state_file, const fs::path& log_file,
              const fs::path& lock_file);

}  // namespace governor_host

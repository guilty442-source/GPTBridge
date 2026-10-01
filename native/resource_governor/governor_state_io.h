/* governor_state_io.h — Rules 讀取與狀態/日誌輸出。
 *
 * 由 main.cpp 依 A185（≤500 effective 行）拆分而來；內容原樣平移，
 * 語義不變：rules 檔熱讀取（fail-closed）、jsonl 動作日誌附加
 * at 時間戳、狀態 JSON 原子寫入。
 */
#pragma once

#include <filesystem>
#include <span>
#include <string>
#include <vector>

#include "resource_governor.h"

namespace governor_host {

namespace fs = std::filesystem;

gptbridge::governor::RulesDoc load_rules_file(const fs::path& rules_path);
void emit_logs(const fs::path& log_path,
               std::span<const gptbridge::jsonlite::JsonValue> entries);
void write_state(const fs::path& state_path,
                 const gptbridge::governor::Snapshot& snap,
                 bool stopped = false, const std::string& reason = {});

/* 自動模式顧問持久化（後端 resource_mode.rs 契約）：
 *  - resource-mode-advisor.json：最近一次評估記錄（at/target/reason/
 *    streak/applied…），原子寫入；
 *  - resource-mode-audit.jsonl：模式切換稽核（timestamp 欄位同 Rust
 *    append_audit_record），僅 changed 週期附加。 */
void load_advisor_state(const fs::path& advisor_path,
                        gptbridge::governor::AdvisorState& state);
void write_advisor_state(const fs::path& advisor_path,
                         const gptbridge::governor::Snapshot& snap);
void append_mode_audit(const fs::path& audit_path,
                       const gptbridge::governor::Snapshot& snap);

}  // namespace governor_host

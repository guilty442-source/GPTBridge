/* resource_governor.h — C++23 資源管制器控制律核心（平台無關部分）傘型標頭。
 *
 * 由 scripts/resource-governor.py（Python）完整遷移而來，對應法典 A608
 * RULE_RESOURCE_GOVERNOR_CPP23_V1：language_id=cpp23，memory_strategy=raii。
 * 依 A185（模組 ≤500 effective 行）拆分為內聚子標頭：
 *
 *  - governor_json_utils.h  常數＋JSON/字串小工具
 *  - governor_rules.h       行程平面歸因＋Rules/組態型別（impl: governor_rules.cpp）
 *  - governor_control.h     滯回調節與回應度偵測狀態
 *  - governor_engine.h      IEngine 介面與 ProcSample/SysInfo
 *  - governor_snapshot.h    ProcessRecord/Snapshot＋JSON 契約
 *  - governor_cycle_env.h   治理週期內部環境（僅控制律 cpp 用）
 *
 * 設計要點（C++23）：RAII 值語義狀態、std::expected fail-closed 解析、
 * std::span 零拷貝視圖；僅依賴標準庫＋共用 jsonlite.h。
 *
 * JSON 狀態契約（main-system/runtime/state/resource-governor.json 與
 * resource-governor.jsonl）與 Python 版逐鍵相容，後端
 * tasks/resource_governor_signal.py 無需任何修改即可繼續讀取。
 */
#pragma once

#include <memory>
#include <set>
#include <string>
#include <vector>

#include "governor_control.h"
#include "governor_engine.h"
#include "governor_json_utils.h"
#include "governor_rules.h"
#include "governor_snapshot.h"

namespace gptbridge {
namespace governor {

/* ------------------------------------------------------------------ */
/* 單一治理週期（與 Python govern_once 語義一致）                       */
/* ------------------------------------------------------------------ */
struct CycleContext {
    std::string self_username;
    std::set<int> self_tree;
    std::string project_root_lower;
    std::string system_root_lower = "c:\\windows";
    double now_mono = 0.0;
    bool disabled = false;
};

Snapshot govern_once(const GovernorConfig& config, const RulesDoc& rules,
                     IEngine& engine, RecordMap& records, RegState& regulation,
                     const CycleContext& ctx, std::vector<jsonlite::JsonValue>& logs);

/* Windows 實作（定義於 governor_engine_win32.cpp；非 Windows 平臺回傳 nullptr）。 */
std::unique_ptr<IEngine> make_windows_engine();

}  // namespace governor
}  // namespace gptbridge

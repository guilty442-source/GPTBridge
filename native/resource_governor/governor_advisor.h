/* governor_advisor.h — 自動模式顧問（需求驅動模式選擇，純控制律）。
 *
 * 承接已退役 Python resource_mode_advisor/auto_adjust_mode 的控制律
 * （B167/B38 原生接替；B159 單一管制器：評估與執法同處
 * resource-governor 行程內，不新增平行 watcher）：
 *  - 夜間省電窗口（power_saving_schedule）→ schedule mode，立即生效；
 *  - 回應緊張／整機 CPU≥strain_cpu／RAM≥strain_mem → low，立即生效；
 *  - worker 需求＋整機餘裕 → 升檔（streak_up 連續評估）；
 *  - 其餘 → medium 基線；降檔需 streak_down＋cooldown_s。
 *
 * 使用者可用性上限（2026-10-01 需求）：auto.ceiling 為自動模式可達
 * 最高檔位（預設 medium）——advisor 永不自動升到 ceiling 之上，
 * 保證前景互動保有 CPU/RAM 餘裕；手動選檔（UI）不受此限並會關閉
 * auto_mode（user intent wins）。
 */
#pragma once

#include <map>
#include <set>
#include <string>
#include <string_view>

#include "governor_json_utils.h"

namespace gptbridge {
namespace governor {

/* ------------------------------------------------------------------ */
/* 顧問政策（rules 檔 "auto" 區塊＋"power_saving_schedule"＋auto_mode）  */
/* ------------------------------------------------------------------ */
struct AdvisorPolicy {
    bool enabled = false; /* rules.auto_mode（預設自動由規則檔宣告） */
    std::string ceiling = "medium"; /* 自動模式上限：advisor 最高可選檔位 */
    double eval_interval_s = 60.0;  /* 評估節拍（與退役 Python 60s 一致） */
    int streak_up = 2;              /* 升檔所需連續需求評估數 */
    int streak_down = 3;            /* 降檔所需連續評估數 */
    double cooldown_s = 600.0;      /* 降檔最小間隔（升檔豁免） */
    double strain_cpu_pct = 85.0;   /* 整機負載立即降 low 的門檻 */
    double strain_mem_pct = 90.0;
    double headroom_cpu_pct = 60.0; /* 允許升檔所需的整機餘裕 */
    double headroom_mem_pct = 75.0;
    double demand_factor = 0.8;     /* worker 帳本 ≥ budget*factor 視為需求 */
    /* 夜間省電排程（缺省啟用 22:00-07:00 → sleep；同 Python 預設）。 */
    bool schedule_enabled = true;
    int schedule_start_min = 22 * 60;
    int schedule_end_min = 7 * 60;
    std::string schedule_mode = "sleep";
    std::string schedule_start = "22:00"; /* 狀態檔回顯用原始字串 */
    std::string schedule_end = "07:00";
};

/* 跨週期顧問狀態（內嵌 RegState；另經 resource-mode-advisor.json
 * 跨重啟保存 streak/last_switch/applied，語義同 Python）。 */
struct AdvisorState {
    int streak = 0;
    std::string last_target;
    std::string applied_mode; /* 空 = 尚未接管（有效模式＝rules.mode） */
    double last_switch_unix = 0.0;
    double last_eval_mono = -1.0; /* <0：從未評估（首週期立即評估） */
};

/* 單次評估輸入（全部來自本週期已量測的信號）。 */
struct AdvisorSignals {
    std::string configured_mode;    /* rules.mode（手動基線/未接管值） */
    std::set<std::string> valid_modes; /* modes 區塊鍵集（apply 前驗存在） */
    bool rules_error = false;
    bool strained = false;
    double cpu_load_pct = 0.0;
    double mem_used_pct = 0.0;
    bool admission_hold = false;
    bool reg_pre = false;
    bool reg_active = false;
    double worker_cpu_pct = 0.0;   /* worker 帳本整機 % */
    double worker_ram_pct = 0.0;
    double budget_cpu_pct = 0.0;   /* 本檔 worker 預算 */
    double budget_ram_pct = 0.0;
    int local_minutes = -1;        /* 本地時間分鐘（<0 = 未知） */
    double now_unix = 0.0;         /* 壁鐘秒（cooldown/last_switch） */
    double now_mono = 0.0;         /* 單調秒（eval 節拍） */
};

struct AdvisorDecision {
    bool evaluated = false;   /* 本週期是否到達評估節拍並完成評估 */
    bool changed = false;     /* 有效模式是否切換（caller 寫稽核） */
    bool urgent = false;
    bool schedule_active = false;
    bool demand = false;
    bool headroom = false;
    std::string target;
    std::string current;
    std::string reason;
    int streak = 0;
};

/* 檔位序：sleep<low<medium<high；未知檔位視同 medium（與 Python
 * _TIER_RANK.get(x, 2) 一致）。 */
int mode_rank(std::string_view mode);

/* 夜間窗口判定（跨午夜感知；local_minutes<0 視為不在窗口）。 */
bool in_schedule_window(int local_minutes, int start_min, int end_min);

/* 由 rules 檔 raw 區塊解析顧問政策；異形 fail-closed（*error 設值）。
 * ceiling/schedule.mode 必須是 valid_modes 成員，否則視為規則錯誤。 */
AdvisorPolicy parse_advisor_policy(const jsonlite::JsonValue* auto_obj,
                                   const jsonlite::JsonValue* schedule_obj,
                                   bool auto_mode,
                                   const std::set<std::string>& valid_modes,
                                   std::string& error);

/* 單次評估（純函式＋AdvisorState 滯回狀態；不觸碰任何檔案/行程）。
 * enabled=false 時清除 applied_mode（手動模式接管）。 */
AdvisorDecision evaluate_advisor(const AdvisorPolicy& policy,
                                 const AdvisorSignals& signals,
                                 AdvisorState& state);

}  // namespace governor
}  // namespace gptbridge

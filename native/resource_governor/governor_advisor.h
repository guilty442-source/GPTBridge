/* governor_advisor.h — 自動模式顧問（需求驅動模式選擇，純控制律）。
 *
 * 承接已退役 Python resource_mode_advisor/auto_adjust_mode 的控制律
 * （B167/B38 原生接替；B159 單一管制器：評估與執法同處
 * resource-governor 行程內，不新增平行 watcher）：
 *  - 夜間省電窗口（power_saving_schedule）→ schedule mode，立即生效；
 *  - 回應緊張／整機 CPU≥strain_cpu／RAM≥strain_mem → low，立即生效；
 *  - worker 需求＋整機餘裕 → 升至有效上限檔位（streak_up 連續評估；
 *    使用中目標＝ceiling、閒置＝idle_ceiling，可達 turbo 90% 檔）；
 *  - 其餘 → medium 基線；降檔需 streak_down＋cooldown_s。
 *
 * 使用者可用性上限（2026-10-01 需求）：auto.ceiling 為自動模式可達
 * 最高檔位（預設 medium）——advisor 永不自動升到 ceiling 之上，
 * 保證前景互動保有 CPU/RAM 餘裕；手動選檔（UI）不受此限並會關閉
 * auto_mode（user intent wins）。
 *
 * 閒置全速（2026-10-01 追加）：auto.idle_full_speed=true 且使用者無輸入
 * ≥ idle_after_s 時，有效上限放寬為 idle_ceiling（預設 high）；使用者
 * 一回來（idle < threshold）高於 ceiling 的檔位立即 urgent 降回——
 * 閒置提速，回座即讓。
 *
 * 手動協助（2026-10-03 追加）：auto.manual_assist=true（預設開）時，
 * 手動選檔不再是靜態死檔——手動檔成為上限，advisor 仍依需求在上限
 * 之下動態升降（平靜→降 medium 基線、需求＋餘裕→頂回手動檔、
 * strain→urgent low）；夜間排程與 idle-lift 不介入手動態（使用者
 * 明確意圖優先），manual_assist=false 還原靜態手動檔。
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
    double strain_cpu_pct = 85.0;   /* 整機負載（EMA）降 low 的門檻 */
    double strain_mem_pct = 90.0;
    /* 信號平滑（EMA）：strain/headroom 判定用跨評估指數平均，吸收
     * 單一繁忙取樣窗口（如 20s 編譯尖峰）；signal_alpha=1.0 還原
     * 舊瞬時行為。instant ≥ strain+strain_instant_margin 的極端
     * 尖峰仍即時 urgent，不等待 EMA 收斂。 */
    double signal_alpha = 0.5;
    double strain_instant_margin = 10.0;
    /* 忙碌節拍：緊張/超載/升檔連續評估進行中時用較短評估間隔，
     * 讓緊急反應與平靜復原都更快（≤0 停用＝恆用 eval_interval_s）。 */
    double eval_interval_busy_s = 20.0;
    double headroom_cpu_pct = 60.0; /* 允許升檔所需的整機餘裕（EMA） */
    double headroom_mem_pct = 75.0;
    double demand_factor = 0.8;     /* worker 帳本 ≥ budget*factor 視為需求 */
    /* 手動協助：auto_mode=false 時 advisor 不放手——手動檔＝上限，
     * 需求在上限之下動態升降（schedule/idle-lift 不介入）。 */
    bool manual_assist = true;
    /* 閒置全速：無使用者輸入 ≥ idle_after_s 時上限放寬至 idle_ceiling。 */
    bool idle_full_speed = true;
    double idle_after_s = 300.0;
    std::string idle_ceiling = "high";
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
    /* 手動協助錨點：applied_mode 產生時所依附的 configured_mode；
     * 使用者切檔（anchor != configured）→ 滯回重置、以新檔為起點。 */
    std::string assist_anchor;
    double last_switch_unix = 0.0;
    double last_eval_mono = -1.0; /* <0：從未評估（首週期立即評估） */
    /* 信號 EMA（僅行程內，不落 advisor.json；<0 = 未播種，首次評估
     * 以當下取樣播種——冷啟動時保持既有即時判定）。 */
    double cpu_ema = -1.0;
    double mem_ema = -1.0;
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
    double user_idle_s = -1.0;     /* 使用者無輸入秒數（<0 = 未知→使用中） */
};

struct AdvisorDecision {
    bool evaluated = false;   /* 本週期是否到達評估節拍並完成評估 */
    bool changed = false;     /* 有效模式是否切換（caller 寫稽核） */
    bool urgent = false;
    bool schedule_active = false;
    bool demand = false;
    bool headroom = false;
    bool idle_active = false;      /* 閒置全速生效中（idle_ceiling 接管上限） */
    bool busy_cadence = false;     /* 本次採用 eval_interval_busy_s（熱態節拍） */
    double cpu_load_ema = -1.0;    /* 本次評估後的整機訊號 EMA（觀測輸出） */
    double mem_used_ema = -1.0;
    std::string eff_ceiling;       /* 本次評估實際使用的上限檔位 */
    std::string target;
    std::string current;
    std::string reason;
    int streak = 0;
};

/* 檔位序：sleep<low<medium<high<turbo；未知檔位視同 medium（與 Python
 * _TIER_RANK.get(x, 2) 一致）。turbo（2026-10-03）為自動模式專用
 * 90% 上限檔——idle_ceiling 目標，UI 不提供手動按鈕。 */
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

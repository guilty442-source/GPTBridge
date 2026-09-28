/* governor_budget.h — 全域 Concurrency Budget（工作類別配額控制律）。
 *
 * 法源：A590（main-system 多核心並行：thread budget 中央管理、per-module
 * quotas、workload classes、background 讓 interactive）；A593（C++23
 * resource-governor 為唯一全專案管制器、admission within A116 envelopes、
 * before/after evidence、generation 綁定）；A598（資源優先序：治理與安全
 * 閘門 > 使用者互動與現役推論 > 受管修復 > 背景訓練）；A622（所有元件在
 * 已登錄 envelope 內自適化）；A610 FORBID:second-resource-governor。
 *
 * 設計：executor 不再自行決定 concurrency；管制器每治理週期把整機
 * logical cores 分配為 8 個工作類別的 quota，發佈於狀態契約
 * ``concurrency-budget/v1``。INTERACTIVE 保有固定保留額（延遲優先），
 * 壓力期依 shed order（TRAINING→BATCH→…→MODEL 最後）降速或暫停；
 * generation 僅在配額變動時遞增（A622 generation 綁定），消費端
 * fail-open：無狀態／過期／kill-switch 皆退回各自的靜態上限。
 */
#pragma once

#include <map>
#include <string>
#include <string_view>

#include "governor_json_utils.h"

namespace gptbridge {
namespace governor {

/* ------------------------------------------------------------------ */
/* 工作類別（固定 8 類，鍵名為契約常數）                                 */
/* ------------------------------------------------------------------ */
enum class WorkClass {
    Interactive = 0,
    Model,
    Rag,
    Network,
    Batch,
    Maintenance,
    Training,
    Verification,
};
inline constexpr int kWorkClassCount = 8;

inline constexpr std::string_view kWorkClassNames[kWorkClassCount] = {
    "interactive", "model",       "rag",        "network",
    "batch",       "maintenance", "training",   "verification",
};

inline std::string_view work_class_name(WorkClass cls) {
    return kWorkClassNames[static_cast<int>(cls)];
}

/* 壓力期降速順序（先淘汰者在前）：A598 — TRAINING 最先暫停，
 * INTERACTIVE 永不降於保留額（不列入 shed order）。 */
inline constexpr WorkClass kShedOrder[] = {
    WorkClass::Training,   WorkClass::Batch,       WorkClass::Verification,
    WorkClass::Maintenance, WorkClass::Network,    WorkClass::Rag,
    WorkClass::Model,
};

/* ------------------------------------------------------------------ */
/* 壓力層級：none（正常）→ pre（調節前置）→ active（調節/回應緊張）      */
/* ------------------------------------------------------------------ */
enum class PressureTier { None = 0, Pre, Active };

inline std::string_view pressure_name(PressureTier tier) {
    switch (tier) {
        case PressureTier::Pre: return "pre";
        case PressureTier::Active: return "active";
        case PressureTier::None: break;
    }
    return "none";
}

/* ------------------------------------------------------------------ */
/* 政策（rules defaults 旋鈕；缺省值與常數一致）                          */
/* ------------------------------------------------------------------ */
struct ClassKnobs {
    double weight = 0.0;   /* pool 權重（tier none 基準配額） */
    double pre_scale = 1.0;
    double active_scale = 1.0;
    int floor_none = 1;    /* 各層最低 quota（0 = 允許暫停） */
    int floor_pre = 1;
    int floor_active = 0;
};

struct BudgetPolicy {
    bool enabled = true;
    double interactive_share = 0.25; /* logical 的 INTERACTIVE 保留比例 */
    int interactive_min = 2;         /* 保留額下限 */
    ClassKnobs knobs[kWorkClassCount]{};
};

/* 預設旋鈕表（非 INTERACTIVE 類別；權重相對 pool 歸一後取 floor）。 */
inline ClassKnobs default_knob(WorkClass cls) {
    switch (cls) {
        case WorkClass::Model:
            return {0.30, 1.00, 0.50, 1, 1, 1};
        case WorkClass::Rag:
            return {0.20, 0.75, 0.50, 1, 1, 1};
        case WorkClass::Network:
            return {0.10, 0.75, 0.50, 1, 1, 1};
        case WorkClass::Verification:
            return {0.10, 0.50, 0.50, 1, 1, 1};
        case WorkClass::Batch:
            return {0.10, 0.50, 0.00, 1, 1, 0};
        case WorkClass::Maintenance:
            return {0.10, 0.50, 0.50, 1, 1, 1};
        case WorkClass::Training:
            return {0.10, 0.50, 0.00, 1, 1, 0};
        case WorkClass::Interactive:
            break;
    }
    return {};
}

/* ------------------------------------------------------------------ */
/* 預算結果                                                            */
/* ------------------------------------------------------------------ */
struct ClassBudget {
    int quota = 0;
    int base = 0;      /* tier none 基準配額（throttled 判定用） */
    bool paused = false;
};

struct ConcurrencyBudget {
    int logical_cores = 0;
    int interactive_reserve = 0;
    int total_quota = 0;
    PressureTier pressure = PressureTier::None;
    long long generation = 0;
    ClassBudget classes[kWorkClassCount]{};
};

/* defaults 解析（concurrency_budget 開關 / *_share / *_w_* / *_min_* 旋鈕）。 */
BudgetPolicy resolve_budget_policy(
    const std::map<std::string, jsonlite::JsonValue>& defaults);

/* 計算一週期配額；total_quota ≤ logical_cores 恆成立。 */
ConcurrencyBudget compute_budget(const BudgetPolicy& policy, int logical_cores,
                                 PressureTier tier, long long generation);

/* 變動偵測簽章（層級＋各類 quota/paused 的規範字串）。 */
std::string budget_signature(const ConcurrencyBudget& budget);

/* 狀態契約 JSON（concurrency-budget/v1）。 */
jsonlite::JsonValue budget_to_json(const ConcurrencyBudget& budget);

}  // namespace governor
}  // namespace gptbridge

/* governor_grants.h — 主系統唯一資源權威的 Grant 協議層。
 *
 * 法源：A590/A593（concurrency budget 中央管理、唯一管制器）；A598
 * （資源優先序 shed order）；A610 FORBID:second-resource-governor；
 * 星澄 native-only 架構規格 §4–§8（ResourceRequest → Governor →
 * DENIED/DEFERRED/PARTIAL/GRANTED/REVOKED → ResourceGrant）。
 *
 * 檔案協議（全部位於 state_dir 之下）：
 *   resource-requests/<request_id>.request.json  星澄提交
 *     （star-resource-request/v1）
 *   resource-requests/<request_id>.renew.json    續約標記
 *   resource-requests/<request_id>.release.json  釋放標記
 *   resource-grants/<request_id>.json            回覆
 *     （star-resource-grant/v1 信封）
 *   resource-grants/grant-audit.jsonl            生命週期稽核
 *     （star-resource-audit/v1）
 *
 * 裁決每治理週期重跑一次：quota 縮小 → grant 檔更新（resize），類別
 * 暫停 / grant 過期 → REVOKED。消費端輪詢 grant 檔即得最新上限。
 */
#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "governor_budget.h"
#include "governor_json_utils.h"
#include "governor_rules.h"

namespace fs = std::filesystem;

namespace gptbridge {
namespace governor {

struct Snapshot; /* governor_snapshot.h 前向宣告 */

namespace grants {

/* ------------------------------------------------------------------ */
/* 契約常數                                                            */
/* ------------------------------------------------------------------ */
inline constexpr std::string_view kRequestFormat =
    "star-resource-request/v1";
inline constexpr std::string_view kGrantFormat = "star-resource-grant/v1";
inline constexpr std::string_view kAuditFormat = "star-resource-audit/v1";
inline constexpr std::string_view kUsageReceiptFormat =
    "star-resource-usage-receipt/v1";

/* 回覆詞彙（規格 §7：主系統只能回這五個）。 */
enum class GrantResponse { Denied, Deferred, Partial, Granted, Revoked };

inline std::string_view response_name(GrantResponse response) {
    switch (response) {
        case GrantResponse::Granted: return "GRANTED";
        case GrantResponse::Partial: return "PARTIAL";
        case GrantResponse::Deferred: return "DEFERRED";
        case GrantResponse::Revoked: return "REVOKED";
        case GrantResponse::Denied: break;
    }
    return "DENIED";
}

/* 規格 §19 壓力狀態字串（EMERGENCY 由 kill-switch/disabled 提升）。 */
inline std::string_view grant_pressure_name(PressureTier tier,
                                            bool emergency) {
    if (emergency) return "EMERGENCY";
    switch (tier) {
        case PressureTier::Pre: return "PRE_PRESSURE";
        case PressureTier::Active: return "ACTIVE_PRESSURE";
        case PressureTier::None: break;
    }
    return "NORMAL";
}

/* ------------------------------------------------------------------ */
/* ResourceRequest（規格 §6 欄位）                                      */
/* ------------------------------------------------------------------ */
struct ResourceRequest {
    std::string request_id;
    std::string workload_id;
    std::string candidate_id;
    std::string capability;
    std::string workload_class;
    long long priority = 0;
    int minimum_cpu_threads = 0;
    int preferred_cpu_threads = 0;
    long long minimum_ram_bytes = 0;
    long long preferred_ram_bytes = 0;
    bool gpu_optional = false;
    bool gpu_required = false;
    long long minimum_vram_bytes = 0;
    long long preferred_vram_bytes = 0;
    long long io_read_budget = 0;
    long long io_write_budget = 0;
    double expected_duration_s = 0.0;
    bool checkpointable = false;
    bool preemptible = false;
};

/* workload_class 字串 → WorkClass；"benchmark"（規格 §62 自動調優類）
 * 不在 8 類固定表內，另由 rules knob `benchmark_quota` 裁決。 */
inline constexpr std::string_view kBenchmarkClass = "benchmark";
std::optional<WorkClass> work_class_from_name(std::string_view name);

/* 解析 star-resource-request/v1；缺欄位/型別錯誤回錯誤字串。 */
std::expected<ResourceRequest, std::string> parse_request(
    const jsonlite::JsonValue& doc);

/* ------------------------------------------------------------------ */
/* 裁決環境（由 Snapshot＋RulesDoc 萃取；純資料，可測）                   */
/* ------------------------------------------------------------------ */
struct GrantKnobs {
    double grant_ttl_s = 120.0;            /* rules: grant_ttl_s */
    double ram_share = 0.5;                /* rules: grant_ram_share */
    long long io_read_cap = 0;             /* rules: grant_io_read_bps */
    long long io_write_cap = 0;
    int background_threads_cap = 0;        /* rules: grant_bg_threads_max */
    int benchmark_quota = 0;               /* rules: benchmark_quota */
    /* 壓力回收：ACTIVE_PRESSURE 時 vram_budget_percent 乘此比例（0–1），
     * 既有 grant resize 後服務端輪詢到更小上限即協作釋放顯存；
     * ram_share 同理縮減新授予的 RAM 上限。 */
    double pressure_vram_scale = 0.5;      /* rules: grant_pressure_vram_scale */
    double pressure_ram_scale = 0.5;       /* rules: grant_pressure_ram_scale */
};

struct GrantContext {
    PressureTier pressure = PressureTier::None;
    bool emergency = false;          /* governor disabled/kill-switch */
    bool class_known = false;
    int class_quota = 0;
    bool class_paused = false;
    int class_outstanding = 0;       /* 同類別現存 grant 已佔 threads */
    bool gpu_enabled = false;
    double vram_budget_percent = 0.0;
    long long vram_total_bytes = 0;  /* 0 → 客戶端以 percent×probe 解析 */
    long long ram_available_bytes = 0;
    GrantKnobs knobs;
    long long generation = 0;
    double now_unix = 0.0;
};

/* ------------------------------------------------------------------ */
/* ResourceGrant 裁決結果（規格 §8 欄位；僅 GRANTED/PARTIAL 攜帶）        */
/* ------------------------------------------------------------------ */
struct GrantDecision {
    GrantResponse response = GrantResponse::Denied;
    std::string reason;
    std::string grant_id;
    double decided_at_s = 0.0;
    int cpu_threads_max = 0;
    long long ram_bytes_max = 0;
    long long pinned_ram_bytes_max = 0;
    bool gpu_allowed = false;
    long long vram_bytes_max = 0;    /* -1 = 以 vram_budget_percent 解析 */
    double vram_budget_percent = 0.0;
    double gpu_compute_share = 0.0;
    long long io_read_limit = 0;
    long long io_write_limit = 0;
    int background_threads_max = 0;
    double valid_until_s = 0.0;
    std::string pressure_state = "NORMAL";
};

/* 純邏輯裁決（無 I/O，套件直接可測）。
 * 規則：類別未知→DENIED；類別暫停/零配額或剩餘 < minimum→DEFERRED；
 * 授予 < preferred 或 GPU 被削→PARTIAL；gpu_required 但 GPU 關閉→
 * DENIED；emergency→DENIED（現存 grant 由週期層另以 REVOKED 處理）。 */
GrantDecision adjudicate(const ResourceRequest& request,
                         const GrantContext& ctx);

/* grant 檔 JSON（含 response 信封）。 */
jsonlite::JsonValue decision_to_json(const ResourceRequest& request,
                                     const GrantDecision& decision);

/* 變動偵測（response＋各上限的規範字串；相同→不重寫 grant/審計）。 */
std::string decision_signature(const GrantDecision& decision);

/* ------------------------------------------------------------------ */
/* 週期層：掃描收件匣→裁決→寫回覆→過期撤銷→renew/release→稽核             */
/* ------------------------------------------------------------------ */
struct GrantCycleStats {
    int requests = 0;
    int granted = 0;
    int partial = 0;
    int deferred = 0;
    int denied = 0;
    int revoked = 0;
    int renewed = 0;
    int released = 0;
};

/* 由 Snapshot＋RulesDoc 組出指定 workload_class 的裁決環境。
 * outstanding = 該類別現存 grant 已佔 threads 總和。 */
GrantContext make_context(const Snapshot& snap, const RulesDoc& rules,
                          std::string_view workload_class,
                          int class_outstanding, double now_unix);

/* 治理週期呼叫點：處理整個收件匣。失敗不拋（治理寫入不可被阻斷）。 */
GrantCycleStats run_grant_cycle(const fs::path& state_dir,
                                const Snapshot& snap,
                                const RulesDoc& rules, double now_unix);

}  // namespace grants
}  // namespace governor
}  // namespace gptbridge

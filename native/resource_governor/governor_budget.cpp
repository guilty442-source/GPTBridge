/* governor_budget.cpp — 全域 Concurrency Budget 分配律實作。
 *
 * 不變式（由測試套件鎖定）：
 *   - total_quota ≤ logical_cores（配額和永不超過整機核數）
 *   - INTERACTIVE 恆得保留額、永不暫停
 *   - 壓力期依 kShedOrder 先降 TRAINING/BATCH/…，MODEL 最後
 *   - quota==0 ⇔ paused（消費端統一解讀為「無配額」）
 */
#include "governor_budget.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <utility>
#include <vector>

namespace gptbridge {
namespace governor {
namespace {

/* 非 INTERACTIVE 類別的配額發放優先序（稀缺時先滿足高優先類別）。 */
constexpr WorkClass kFillOrder[] = {
    WorkClass::Model,        WorkClass::Rag,         WorkClass::Network,
    WorkClass::Verification, WorkClass::Maintenance, WorkClass::Batch,
    WorkClass::Training,
};
constexpr int kFillCount = static_cast<int>(
    sizeof(kFillOrder) / sizeof(kFillOrder[0]));

int clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }

/* JSON builder 小工具：governor_snapshot.h 的 detail::j* 依賴 Snapshot
 * 型別會形成循環 include，此處就地複刻最小集合。 */
jsonlite::JsonValue bjstr(std::string_view value) {
    jsonlite::JsonValue out;
    out.type = jsonlite::JsonValue::Type::String;
    out.string = std::string(value);
    return out;
}
jsonlite::JsonValue bjint(long long value) {
    jsonlite::JsonValue out;
    out.type = jsonlite::JsonValue::Type::Number;
    out.number = static_cast<double>(value);
    return out;
}
jsonlite::JsonValue bjobj(
    std::vector<std::pair<std::string, jsonlite::JsonValue>> fields) {
    jsonlite::JsonValue out;
    out.type = jsonlite::JsonValue::Type::Object;
    out.object = std::move(fields);
    return out;
}
jsonlite::JsonValue bjarr(std::vector<jsonlite::JsonValue> items) {
    jsonlite::JsonValue out;
    out.type = jsonlite::JsonValue::Type::Array;
    out.array = std::move(items);
    return out;
}

const jsonlite::JsonValue* find_key(
    const std::map<std::string, jsonlite::JsonValue>& defaults,
    const std::string& key) {
    const auto it = defaults.find(key);
    return it != defaults.end() ? &it->second : nullptr;
}

double knob_or(const std::map<std::string, jsonlite::JsonValue>& defaults,
               WorkClass cls, const char* leaf, double dflt) {
    std::string key = "concurrency_";
    key += leaf;
    key += "_";
    key += work_class_name(cls);
    return json_num_or(find_key(defaults, key), dflt);
}

void alloc_base(ConcurrencyBudget& out, const BudgetPolicy& policy, int pool) {
    int remaining = pool;
    for (int i = 0; i < kFillCount && remaining > 0; ++i) {
        const WorkClass cls = kFillOrder[i];
        const ClassKnobs& k = policy.knobs[static_cast<int>(cls)];
        int want = std::max(k.floor_none,
                            static_cast<int>(std::floor(pool * k.weight)));
        int take = std::min(want, remaining);
        out.classes[static_cast<int>(cls)].base = take;
        remaining -= take;
    }
    for (int i = 0; remaining > 0; i = (i + 1) % kFillCount) {
        ++out.classes[static_cast<int>(kFillOrder[i])].base;
        --remaining;
    }
}

int scaled_quota(const ClassKnobs& k, int base, PressureTier tier) {
    switch (tier) {
        case PressureTier::Pre:
            return std::max(k.floor_pre,
                            static_cast<int>(std::floor(base * k.pre_scale)));
        case PressureTier::Active:
            return std::max(k.floor_active,
                            static_cast<int>(std::floor(base * k.active_scale)));
        case PressureTier::None:
            break;
    }
    return base;
}

}  // namespace

BudgetPolicy resolve_budget_policy(
    const std::map<std::string, jsonlite::JsonValue>& defaults) {
    BudgetPolicy policy;
    const jsonlite::JsonValue* en = find_key(defaults, "concurrency_budget");
    policy.enabled =
        en == nullptr || json_is_true(en) || json_num_or(en, 1.0) != 0.0;
    policy.interactive_share = std::clamp(
        json_num_or(find_key(defaults, "concurrency_interactive_share"),
                    policy.interactive_share),
        0.1, 0.6);
    policy.interactive_min = std::max(
        1, static_cast<int>(json_num_or(
               find_key(defaults, "concurrency_interactive_min"),
               policy.interactive_min)));
    for (int i = 1; i < kWorkClassCount; ++i) {
        const WorkClass cls = static_cast<WorkClass>(i);
        ClassKnobs k = default_knob(cls);
        k.weight = std::clamp(knob_or(defaults, cls, "w", k.weight), 0.0, 1.0);
        k.pre_scale =
            std::clamp(knob_or(defaults, cls, "pre", k.pre_scale), 0.0, 1.0);
        k.active_scale =
            std::clamp(knob_or(defaults, cls, "act", k.active_scale), 0.0, 1.0);
        k.floor_none = clampi(
            static_cast<int>(knob_or(defaults, cls, "min", k.floor_none)),
            0, 64);
        k.floor_pre = clampi(
            static_cast<int>(knob_or(defaults, cls, "minpre", k.floor_pre)),
            0, 64);
        k.floor_active = clampi(
            static_cast<int>(knob_or(defaults, cls, "minact", k.floor_active)),
            0, 64);
        policy.knobs[i] = k;
    }
    return policy;
}

ConcurrencyBudget compute_budget(const BudgetPolicy& policy, int logical_cores,
                                 PressureTier tier, long long generation) {
    ConcurrencyBudget out;
    out.logical_cores = std::max(1, logical_cores);
    out.pressure = tier;
    out.generation = generation;

    int reserve = static_cast<int>(
        std::lround(out.logical_cores * policy.interactive_share));
    reserve = clampi(reserve, policy.interactive_min, out.logical_cores);
    out.interactive_reserve = reserve;
    const int pool = out.logical_cores - reserve;

    alloc_base(out, policy, pool);
    int used = reserve;
    for (int i = 0; i < kFillCount; ++i) {
        const int idx = static_cast<int>(kFillOrder[i]);
        ClassBudget& cls = out.classes[idx];
        int q = std::min(scaled_quota(policy.knobs[idx], cls.base, tier),
                         std::max(0, out.logical_cores - used));
        cls.quota = q;
        cls.paused = (q == 0);
        used += q;
    }
    ClassBudget& inter = out.classes[static_cast<int>(WorkClass::Interactive)];
    inter.quota = reserve;
    inter.base = reserve;
    inter.paused = false;
    out.total_quota = used;
    return out;
}

std::string budget_signature(const ConcurrencyBudget& budget) {
    std::string sig;
    sig.reserve(64);
    sig += std::to_string(static_cast<int>(budget.pressure));
    sig += '|';
    sig += std::to_string(budget.interactive_reserve);
    for (int i = 0; i < kWorkClassCount; ++i) {
        sig += ';';
        sig += std::to_string(budget.classes[i].quota);
    }
    return sig;
}

jsonlite::JsonValue budget_to_json(const ConcurrencyBudget& budget) {
    jsonlite::JsonValue classes = bjobj({});
    for (int i = 0; i < kWorkClassCount; ++i) {
        const ClassBudget& cls = budget.classes[i];
        const char* state = cls.paused ? "paused"
                            : cls.quota < cls.base ? "throttled"
                                                   : "normal";
        classes.object.emplace_back(
            std::string(work_class_name(static_cast<WorkClass>(i))),
            bjobj({{"quota", bjint(cls.quota)},
                   {"state", bjstr(state)}}));
    }
    jsonlite::JsonValue shed = bjarr({});
    for (WorkClass cls : kShedOrder) {
        shed.array.push_back(bjstr(work_class_name(cls)));
    }
    return bjobj(
        {{"contract", bjstr("concurrency-budget/v1")},
         {"generation", bjint(budget.generation)},
         {"logical_cores", bjint(budget.logical_cores)},
         {"interactive_reserve", bjint(budget.interactive_reserve)},
         {"total_quota", bjint(budget.total_quota)},
         {"pressure", bjstr(pressure_name(budget.pressure))},
         {"classes", classes},
         {"shed_order", shed}});
}

}  // namespace governor
}  // namespace gptbridge

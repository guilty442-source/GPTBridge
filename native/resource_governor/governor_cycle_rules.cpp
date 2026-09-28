/* governor_cycle_rules.cpp — 治理週期常駐規則的一次性套用。
 *
 * 由 resource_governor.cpp 的 govern_once 依 A185（函式 ≤50 effective 行）
 * 拆分而來；語義逐行一致：
 *  - apply_rule_lasso：background/EcoQoS/CPU limiter（受 features 閘門；
 *    成功後記入 rule_hold）。
 *  - apply_rule_static：priority/affinity（background 規則抑制 priority）。
 */
#include "governor_cycle_env.h"

namespace gptbridge {
namespace governor {

using namespace detail;

void apply_rule_lasso(CycleEnv& env, const ProcSample& sample,
                      const ProgramRule& rule, const ProcKey& key,
                      ProcessRecord& record) {
    if (rule.background && env.features.background_mode) {
        const bool ok = env.dry_run || env.engine.background(sample.pid, true);
        record.bg_set = true;
        record.rule_hold.insert("bg");
        env.actions.push_back(jobj({{"action", jstr("rule-background-mode")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"ok", jbool(ok)}}));
    }
    if (rule.ecoqos && env.features.ecoqos) {
        const bool ok = env.dry_run || env.engine.ecoqos(sample.pid, true);
        record.eco_set = true;
        record.rule_hold.insert("eco");
        env.actions.push_back(jobj({{"action", jstr("rule-ecoqos")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"ok", jbool(ok)}}));
    }
    if (rule.cpu_limit_percent > 0 && env.features.cpu_limiter) {
        const bool ok =
            env.dry_run ||
            env.engine.cpu_limit(key, sample.pid, rule.cpu_limit_percent, 0, 0);
        record.limit_set = true;
        record.rule_hold.insert("limit");
        env.actions.push_back(jobj({{"action", jstr("rule-cpu-limited")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"limiter_percent", jnum(rule.cpu_limit_percent)},
                                    {"ok", jbool(ok)}}));
    }
}

void apply_rule_static(CycleEnv& env, const ProcSample& sample,
                       const ProgramRule& rule, ProcessRecord& record) {
    if (rule.priority_class.has_value() &&
        !(rule.background && env.features.background_mode)) {
        if (!env.dry_run) env.engine.set_priority(sample.pid, *rule.priority_class);
        record.rule_priority = *rule.priority_class;
        env.actions.push_back(jobj({{"action", jstr("rule-priority")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"priority", jint(*rule.priority_class)}}));
    }
    if (rule.affinity.has_value() && env.config.affinity) {
        if (!env.dry_run) env.engine.set_affinity(sample.pid, *rule.affinity);
        record.rule_aff_set = true;
        record.rule_hold.insert("affinity");
        env.actions.push_back(jobj({{"action", jstr("rule-affinity")},
                                    {"pid", jint(sample.pid)},
                                    {"name", jstr(sample.name)},
                                    {"cpus", jint(static_cast<long long>(
                                                      rule.affinity->size()))}}));
    }
}

}  // namespace governor
}  // namespace gptbridge

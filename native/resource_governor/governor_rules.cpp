/* governor_rules.cpp — Rules 檔解析與組態解析實作。
 *
 * 由 resource_governor.h 依 A185 拆分而來；內容原樣平移，語義不變：
 *  parse_rules / resolve_features / resolve_thresholds（CLI > defaults >
 *  modes preset > 常數 的優先序與 Python 一致；任何異形 fail-closed）。
 */
#include "governor_rules.h"

namespace gptbridge {
namespace governor {

/* 解析 rules JSON 文字；fail-closed：任何異形回傳 error（defaults/programs 為空）。 */
std::expected<RulesDoc, std::string> parse_rules(std::string_view text) {
    using jsonlite::JsonParser;
    using jsonlite::JsonValue;
    using T = JsonValue::Type;
    RulesDoc doc;
    JsonValue root;
    try {
        root = JsonParser(std::string(text)).parse();
    } catch (const jsonlite::JsonError&) {
        doc.error = "JsonError: invalid JSON";
        return doc;
    }
    if (root.type != T::Object) {
        doc.error = "root must be an object";
        return doc;
    }
    if (const JsonValue* raw_defaults = root.get("defaults");
        raw_defaults != nullptr && raw_defaults->type == T::Object) {
        for (const auto& [key, value] : raw_defaults->object)
            doc.defaults.emplace(key, value);
    }
    if (const JsonValue* raw_modes = root.get("modes");
        raw_modes != nullptr && raw_modes->type == T::Object) {
        std::string raw_mode = "medium";
        if (const JsonValue* mode_value = root.get("mode");
            mode_value != nullptr && mode_value->type == T::String &&
            !mode_value->string.empty()) {
            raw_mode = mode_value->string;
        }
        const JsonValue* preset = nullptr;
        if (const JsonValue* found = raw_modes->get(raw_mode); found != nullptr &&
            found->type == T::Object) {
            preset = found;
        }
        if (preset == nullptr) {
            doc.error = "unknown mode '" + raw_mode + "'";
        } else {
            doc.mode = raw_mode;
            doc.has_mode = true;
            for (const auto& [key, value] : preset->object)
                doc.defaults.emplace(key, value);
            /* 顯式 defaults 鍵覆寫 preset（Python {**preset, **defaults}）。 */
            if (const JsonValue* raw_explicit = root.get("defaults");
                raw_explicit != nullptr && raw_explicit->type == T::Object) {
                for (const auto& [key, value] : raw_explicit->object)
                    doc.defaults[key] = value;
            }
        }
    }
    if (const JsonValue* raw_programs = root.get("programs");
        raw_programs != nullptr && raw_programs->type == T::Object) {
        for (const auto& [key, entry] : raw_programs->object) {
            if (key.empty() || entry.type != T::Object) continue;
            ProgramRule rule;
            try {
                rule.exclude = json_is_true(entry.get("exclude"));
                if (const JsonValue* prio = entry.get("priority");
                    prio != nullptr && prio->type == T::String &&
                    !prio->string.empty()) {
                    rule.priority_class = parse_priority_name(prio->string);
                }
                if (const JsonValue* aff = entry.get("affinity");
                    aff != nullptr && aff->type == T::Array && !aff->array.empty()) {
                    std::vector<int> cpus;
                    bool valid = true;
                    for (const JsonValue& item : aff->array) {
                        if (item.type != T::Number || item.number < 0 ||
                            std::floor(item.number) != item.number) {
                            valid = false;
                            break;
                        }
                        cpus.push_back(static_cast<int>(item.number));
                    }
                    if (valid) rule.affinity = std::move(cpus);
                }
                if (const JsonValue* limit = entry.get("cpu_limit_percent");
                    limit != nullptr && limit->type == T::Number &&
                    limit->number > 0) {
                    rule.cpu_limit_percent = std::clamp(limit->number,
                                                        kLimiterMinPercent,
                                                        kLimiterMaxPercent);
                }
                rule.background = json_is_true(entry.get("background"));
                rule.ecoqos = json_is_true(entry.get("ecoqos"));
            } catch (...) {
                doc.error = key + ": invalid rule entry";
                return doc;
            }
            doc.programs.emplace(to_lower(key), std::move(rule));
        }
    }
    return doc;
}

Features resolve_features(
    const GovernorConfig& config,
    const std::map<std::string, jsonlite::JsonValue>& defaults,
    long long total_ram_bytes) {
    Features out;
    auto find = [&](std::string_view key) -> const jsonlite::JsonValue* {
        auto it = defaults.find(std::string(key));
        return it != defaults.end() ? &it->second : nullptr;
    };
    out.probalance = feature_enabled(config.probalance, defaults, "probalance");
    out.cpu_limiter = feature_enabled(config.cpu_limiter, defaults, "cpu_limiter");
    out.background_mode =
        feature_enabled(config.background_mode, defaults, "background_mode");
    out.ecoqos = feature_enabled(config.ecoqos, defaults, "ecoqos");
    const double limiter = config.limiter_percent.has_value()
                               ? *config.limiter_percent
                               : json_num_or(find("limiter_percent"),
                                             kDefaultLimiterPercent);
    out.limiter_percent = std::clamp(limiter, kLimiterMinPercent, kLimiterMaxPercent);
    out.worker_job_cap =
        feature_enabled(config.worker_job_cap, defaults, "worker_job_cap");
    const double job_percent = config.worker_job_percent.has_value()
                                   ? *config.worker_job_percent
                                   : json_num_or(find("worker_job_percent"),
                                                 kWorkerCpuBudgetPct);
    out.worker_job_percent = std::clamp(job_percent, 1.0, 100.0);
    /* worker_job_memory_mb 優先，否則 worker_job_memory_percent（預設 30）。 */
    if (const jsonlite::JsonValue* mb = find("worker_job_memory_mb");
        mb != nullptr && mb->type == jsonlite::JsonValue::Type::Number &&
        mb->number > 0) {
        out.worker_job_memory_bytes =
            static_cast<long long>(mb->number) * 1024LL * 1024LL;
    } else {
        const double pct = json_num_or(find("worker_job_memory_percent"), 30.0);
        if (pct > 0 && total_ram_bytes > 0) {
            out.worker_job_memory_bytes = static_cast<long long>(
                static_cast<double>(total_ram_bytes) * std::min(pct, 100.0) / 100.0);
        }
    }
    const double proc_limit =
        json_num_or(find("worker_job_process_limit"), 96.0);
    out.worker_job_process_limit =
        proc_limit > 0 ? static_cast<int>(proc_limit) : 0;
    const double ratio = config.resp_ratio.has_value()
                             ? *config.resp_ratio
                             : json_num_or(find("resp_strain_ratio"), kRespStrainRatio);
    out.resp_ratio = std::max(1.05, ratio);
    return out;
}

Thresholds resolve_thresholds(
    const GovernorConfig& config,
    const std::map<std::string, jsonlite::JsonValue>& defaults) {
    Thresholds out;
    auto find = [&](std::string_view key) -> const jsonlite::JsonValue* {
        auto it = defaults.find(std::string(key));
        return it != defaults.end() ? &it->second : nullptr;
    };
    out.cpu_busy = config.cpu_busy.has_value()
                       ? *config.cpu_busy
                       : json_num_or(find("cpu_busy"), kCpuBusyPct);
    out.cpu_extreme = config.cpu_extreme.has_value()
                          ? *config.cpu_extreme
                          : json_num_or(find("cpu_extreme"), kCpuExtremePct);
    const double sustain_raw = config.sustain.has_value()
                                   ? static_cast<double>(*config.sustain)
                                   : json_num_or(find("sustain"),
                                                 static_cast<double>(kSustainSamples));
    out.sustain = static_cast<int>(std::max(1.0, sustain_raw));
    const double extreme_default =
        std::max(static_cast<double>(out.sustain) * 2.0,
                 static_cast<double>(kExtremeSamples));
    out.extreme_sustain =
        std::max(1, static_cast<int>(json_num_or(find("extreme_sustain"),
                                                extreme_default)));
    out.mem_trim_mb = config.mem_trim_mb.has_value()
                          ? *config.mem_trim_mb
                          : json_num_or(find("mem_trim_mb"), kMemTrimMb);
    const jsonlite::JsonValue* affinity_value = find("affinity");
    bool rules_affinity = true;
    if (affinity_value != nullptr) {
        /* 與 Python bool(defaults.get("affinity", True)) 一致的真值語義。 */
        using T = jsonlite::JsonValue::Type;
        switch (affinity_value->type) {
            case T::Null: rules_affinity = false; break;
            case T::Bool: rules_affinity = affinity_value->boolean; break;
            case T::Number: rules_affinity = affinity_value->number != 0.0; break;
            case T::String: rules_affinity = !affinity_value->string.empty(); break;
            case T::Array: rules_affinity = !affinity_value->array.empty(); break;
            case T::Object: rules_affinity = !affinity_value->object.empty(); break;
        }
    }
    out.affinity = config.affinity && rules_affinity;
    out.worker_cpu_budget =
        std::max(1.0, json_num_or(find("worker_cpu_budget"), kWorkerCpuBudgetPct));
    out.worker_ram_budget =
        std::max(1.0, json_num_or(find("worker_ram_budget"), kWorkerRamBudgetPct));
    return out;
}

}  // namespace governor
}  // namespace gptbridge

/* governor_grants.cpp — Grant 協議層實作（裁決純邏輯＋檔案週期）。
 *
 * 依 A185 拆分：adjudicate/序列化為純邏輯（套件可測），run_grant_cycle
 * 為唯一 I/O 入口，由 watch_cycle/run_once 在 write_state 之後呼叫。
 */
#include "governor_grants.h"

#include <algorithm>
#include <cmath>
#include <expected>
#include <fstream>
#include <sstream>

#include "governor_snapshot.h"

namespace gptbridge {
namespace governor {
namespace grants {

namespace {
using namespace detail; /* jstr/jnum/jint/jbool/jnull/jobj/jarr */

std::string json_str(const jsonlite::JsonValue* v) {
    return v != nullptr && v->type == jsonlite::JsonValue::Type::String
               ? v->string
               : std::string();
}
long long json_ll(const jsonlite::JsonValue* v, long long fallback) {
    if (v == nullptr) return fallback;
    using T = jsonlite::JsonValue::Type;
    if (v->type == T::Number) return static_cast<long long>(v->number);
    if (v->type == T::Bool) return v->boolean ? 1 : 0;
    return fallback;
}
bool json_bool(const jsonlite::JsonValue* v) {
    return v != nullptr && v->type == jsonlite::JsonValue::Type::Bool &&
           v->boolean;
}

/* JsonValue 僅有唯讀 get；物件寫入以就地替換/追加實作。 */
void obj_set(jsonlite::JsonValue& obj, const std::string& key,
             jsonlite::JsonValue value) {
    if (obj.type != jsonlite::JsonValue::Type::Object) return;
    for (auto& kv : obj.object)
        if (kv.first == key) {
            kv.second = std::move(value);
            return;
        }
    obj.object.emplace_back(key, std::move(value));
}

bool write_text(const fs::path& path, std::string_view text) {
    std::error_code ec;
    fs::create_directories(path.parent_path(), ec);
    const fs::path tmp = path.parent_path() / (path.filename().string() + ".tmp");
    {
        std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
        if (!out) return false;
        out << text;
    }
    fs::rename(tmp, path, ec);
    if (ec) {
        fs::remove(path, ec);
        std::error_code ec2;
        fs::rename(tmp, path, ec2);
        if (ec2) return false;
    }
    return true;
}

void append_audit(const fs::path& state_dir, const std::string& action,
                  const std::string& request_id, const std::string& workload_id,
                  const jsonlite::JsonValue& detail, double now_unix) {
    const fs::path ledger =
        state_dir / "resource-grants" / "grant-audit.jsonl";
    std::error_code ec;
    fs::create_directories(ledger.parent_path(), ec);
    std::ofstream out(ledger, std::ios::binary | std::ios::app);
    if (!out) return;
    const jsonlite::JsonValue line = jobj(
        {{"format", jstr(kAuditFormat)},
         {"action", jstr(action)},
         {"request_id", jstr(request_id)},
         {"workload_id", jstr(workload_id)},
         {"at_unix", jnum(now_unix)},
         {"detail", detail}});
    out << jsonlite::json_serialize(line) << "\n";
}

bool read_json_file(const fs::path& path, jsonlite::JsonValue& out) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return false;
    std::ostringstream ss;
    ss << in.rdbuf();
    try {
        out = jsonlite::JsonParser(ss.str()).parse();
        return true;
    } catch (const jsonlite::JsonError&) {
        return false;
    }
}

/* 規則檔 defaults 內的 grant 旋鈕（缺省=常數）。 */
GrantKnobs resolve_knobs(
    const std::map<std::string, jsonlite::JsonValue>& defaults) {
    GrantKnobs k;
    auto num = [&](std::string_view key, double fb) {
        auto it = defaults.find(std::string(key));
        return it != defaults.end() ? json_num_or(&it->second, fb) : fb;
    };
    k.grant_ttl_s = num("grant_ttl_s", k.grant_ttl_s);
    k.ram_share = num("grant_ram_share", k.ram_share);
    k.io_read_cap = static_cast<long long>(num("grant_io_read_bps", 0));
    k.io_write_cap = static_cast<long long>(num("grant_io_write_bps", 0));
    k.background_threads_cap =
        static_cast<int>(num("grant_bg_threads_max", 0));
    k.benchmark_quota = static_cast<int>(num("benchmark_quota", 0));
    return k;
}

/* 現存 grant 檔 → (request_id → {workload_class, threads, valid_until})；
 * 供 outstanding 計算與過期掃描共用。 */
struct LiveGrant {
    std::string request_id;
    std::string workload_id;
    std::string workload_class;
    int cpu_threads_max = 0;
    double valid_until_s = 0.0;
    std::string signature;
};

std::map<std::string, LiveGrant> load_live_grants(const fs::path& grants_dir) {
    std::map<std::string, LiveGrant> live;
    std::error_code ec;
    if (!fs::exists(grants_dir, ec)) return live;
    for (const auto& entry : fs::directory_iterator(grants_dir, ec)) {
        if (!entry.is_regular_file() ||
            entry.path().extension() != ".json")
            continue;
        jsonlite::JsonValue doc;
        if (!read_json_file(entry.path(), doc)) continue;
        const jsonlite::JsonValue* g = doc.get("grant");
        if (g == nullptr || g->type != jsonlite::JsonValue::Type::Object)
            continue;
        const std::string resp = json_str(doc.get("response"));
        if (resp != "GRANTED" && resp != "PARTIAL") continue;
        LiveGrant lg;
        lg.request_id = json_str(doc.get("request_id"));
        lg.workload_id = json_str(doc.get("workload_id"));
        lg.workload_class = json_str(g->get("workload_class"));
        lg.cpu_threads_max = static_cast<int>(json_ll(g->get("cpu_threads_max"), 0));
        lg.valid_until_s = static_cast<double>(json_ll(g->get("valid_until_s"), 0));
        lg.signature = json_str(doc.get("signature"));
        if (!lg.request_id.empty()) live[lg.request_id] = lg;
    }
    return live;
}

}  // namespace

std::optional<WorkClass> work_class_from_name(std::string_view name) {
    for (int i = 0; i < kWorkClassCount; ++i)
        if (kWorkClassNames[i] == name)
            return static_cast<WorkClass>(i);
    return std::nullopt;
}

std::expected<ResourceRequest, std::string> parse_request(
    const jsonlite::JsonValue& doc) {
    if (doc.type != jsonlite::JsonValue::Type::Object)
        return std::unexpected("request-not-object");
    if (json_str(doc.get("format")) != kRequestFormat)
        return std::unexpected("format-not-star-resource-request/v1");
    ResourceRequest r;
    r.request_id = json_str(doc.get("request_id"));
    r.workload_id = json_str(doc.get("workload_id"));
    r.candidate_id = json_str(doc.get("candidate_id"));
    r.capability = json_str(doc.get("capability"));
    r.workload_class = json_str(doc.get("workload_class"));
    if (r.request_id.empty() || r.workload_id.empty() ||
        r.workload_class.empty())
        return std::unexpected("missing-request-id-workload-id-class");
    r.priority = json_ll(doc.get("priority"), 0);
    r.minimum_cpu_threads =
        static_cast<int>(json_ll(doc.get("minimum_cpu_threads"), 0));
    r.preferred_cpu_threads =
        static_cast<int>(json_ll(doc.get("preferred_cpu_threads"), 0));
    r.minimum_ram_bytes = json_ll(doc.get("minimum_ram_bytes"), 0);
    r.preferred_ram_bytes = json_ll(doc.get("preferred_ram_bytes"), 0);
    r.gpu_optional = json_bool(doc.get("gpu_optional"));
    r.gpu_required = json_bool(doc.get("gpu_required"));
    r.minimum_vram_bytes = json_ll(doc.get("minimum_vram_bytes"), 0);
    r.preferred_vram_bytes = json_ll(doc.get("preferred_vram_bytes"), 0);
    r.io_read_budget = json_ll(doc.get("io_read_budget"), 0);
    r.io_write_budget = json_ll(doc.get("io_write_budget"), 0);
    if (const jsonlite::JsonValue* d = doc.get("expected_duration_s"))
        r.expected_duration_s = json_num_or(d, 0.0);
    r.checkpointable = json_bool(doc.get("checkpointable"));
    r.preemptible = json_bool(doc.get("preemptible"));
    return r;
}

namespace {

/* 早退拒絕鏈：emergency → 未知類別 → 類別暫停/零配額 → GPU 硬性需求。
 * 回傳 false 表示請求仍可裁決。 */
bool deny_early(const ResourceRequest& request, const GrantContext& ctx,
                GrantDecision& d) {
    if (ctx.emergency) {
        d.response = GrantResponse::Denied;
        d.reason = "emergency-pressure";
    } else if (!ctx.class_known) {
        d.response = GrantResponse::Denied;
        d.reason = "unknown-workload-class:" + request.workload_class;
    } else if (ctx.class_paused || ctx.class_quota <= 0) {
        d.response = GrantResponse::Deferred;
        d.reason = "class-paused-or-zero-quota";
    } else if (request.gpu_required && !ctx.gpu_enabled) {
        d.response = GrantResponse::Denied;
        d.reason = "gpu-required-but-disabled-by-mode";
    }
    return d.response != GrantResponse::Denied || !d.reason.empty();
}

/* 規格 §41：effective VRAM = min(driver, grant)；governor 不探 GPU
 * 硬體，有 vram_total_bytes 才給絕對值，否則 -1 由客戶端以
 * vram_budget_percent × probe_total 解析。 */
long long grant_vram_max(const ResourceRequest& request,
                         const GrantContext& ctx) {
    if (ctx.vram_total_bytes <= 0) return -1;
    return static_cast<long long>(ctx.vram_total_bytes *
                                  ctx.vram_budget_percent / 100.0);
}

/* GRANTED/PARTIAL 的 grant 欄位填充（規格 §8）。 */
void fill_grant(const ResourceRequest& request, const GrantContext& ctx,
                GrantDecision& d, int granted_cpu, long long ram_max,
                bool gpu_granted, long long vram_max) {
    d.cpu_threads_max = granted_cpu;
    d.ram_bytes_max = ram_max;
    d.pinned_ram_bytes_max = 0;
    d.gpu_allowed = gpu_granted;
    d.vram_bytes_max = vram_max;
    d.vram_budget_percent = gpu_granted ? ctx.vram_budget_percent : 0.0;
    d.gpu_compute_share = gpu_granted ? 1.0 : 0.0;
    const auto cap_io = [](long long want, long long cap) {
        return cap > 0 ? std::min(want > 0 ? want : cap, cap) : want;
    };
    d.io_read_limit = cap_io(request.io_read_budget, ctx.knobs.io_read_cap);
    d.io_write_limit =
        cap_io(request.io_write_budget, ctx.knobs.io_write_cap);
    d.background_threads_max =
        ctx.knobs.background_threads_cap > 0
            ? std::min(granted_cpu, ctx.knobs.background_threads_cap)
            : granted_cpu / 2;
}

}  // namespace

GrantDecision adjudicate(const ResourceRequest& request,
                         const GrantContext& ctx) {
    GrantDecision d;
    d.pressure_state =
        std::string(grant_pressure_name(ctx.pressure, ctx.emergency));
    d.grant_id = "rg-" + request.request_id;
    d.decided_at_s = ctx.now_unix;
    d.valid_until_s = ctx.now_unix + ctx.knobs.grant_ttl_s;
    if (deny_early(request, ctx, d)) return d;
    const int available = std::max(0, ctx.class_quota - ctx.class_outstanding);
    const int want = request.preferred_cpu_threads > 0
                         ? request.preferred_cpu_threads
                         : request.minimum_cpu_threads;
    const int granted_cpu = std::max(0, std::min(want, available));
    if (granted_cpu < std::max(1, request.minimum_cpu_threads)) {
        d.response = GrantResponse::Deferred;
        d.reason = "below-minimum-cpu-threads";
        return d;
    }
    /* RAM：min(preferred, 可用 × share)；無 preferred → 以 minimum 為
     * 授予值。 */
    const long long ram_cap =
        ctx.ram_available_bytes > 0
            ? static_cast<long long>(ctx.ram_available_bytes *
                                     ctx.knobs.ram_share)
            : request.preferred_ram_bytes;
    const long long ram_max =
        request.preferred_ram_bytes > 0
            ? std::min(request.preferred_ram_bytes, ram_cap)
            : std::min(request.minimum_ram_bytes, ram_cap);
    if (ram_max < request.minimum_ram_bytes) {
        d.response = GrantResponse::Deferred;
        d.reason = "below-minimum-ram";
        return d;
    }
    const bool wants_gpu = request.gpu_required || request.gpu_optional;
    const bool gpu_granted = wants_gpu && ctx.gpu_enabled;
    const long long vram_max =
        gpu_granted ? grant_vram_max(request, ctx) : 0;
    if (request.gpu_required && gpu_granted && vram_max >= 0 &&
        vram_max < request.minimum_vram_bytes) {
        d.response = GrantResponse::Denied;
        d.reason = "vram-budget-below-minimum";
        return d;
    }
    fill_grant(request, ctx, d, granted_cpu, ram_max, gpu_granted, vram_max);
    const bool partial = granted_cpu < want ||
                         (request.gpu_optional && !ctx.gpu_enabled);
    d.response = partial ? GrantResponse::Partial : GrantResponse::Granted;
    d.reason = partial ? "partial-grant" : "granted";
    return d;
}

std::string decision_signature(const GrantDecision& d) {
    std::ostringstream ss;
    ss << response_name(d.response) << '|' << d.cpu_threads_max << '|'
       << d.ram_bytes_max << '|' << d.gpu_allowed << '|' << d.vram_bytes_max
       << '|' << d.vram_budget_percent << '|' << d.io_read_limit << '|'
       << d.io_write_limit << '|' << d.background_threads_max << '|'
       << d.pressure_state;
    return ss.str();
}

jsonlite::JsonValue decision_to_json(const ResourceRequest& request,
                                     const GrantDecision& d) {
    std::vector<std::pair<std::string, jsonlite::JsonValue>> fields;
    fields.emplace_back("format", jstr(kGrantFormat));
    fields.emplace_back("request_id", jstr(request.request_id));
    fields.emplace_back("workload_id", jstr(request.workload_id));
    fields.emplace_back("response", jstr(response_name(d.response)));
    fields.emplace_back("reason", jstr(d.reason));
    fields.emplace_back("decided_at_s", jnum(d.decided_at_s));
    if (d.response == GrantResponse::Granted ||
        d.response == GrantResponse::Partial ||
        d.response == GrantResponse::Revoked) {
        fields.emplace_back(
            "grant",
            jobj({{"grant_id", jstr(d.grant_id)},
                  {"workload_class", jstr(request.workload_class)},
                  {"cpu_threads_max", jint(d.cpu_threads_max)},
                  {"ram_bytes_max", jint(d.ram_bytes_max)},
                  {"pinned_ram_bytes_max", jint(d.pinned_ram_bytes_max)},
                  {"gpu_allowed", jbool(d.gpu_allowed)},
                  {"vram_bytes_max", jint(d.vram_bytes_max)},
                  {"vram_budget_percent", jnum(d.vram_budget_percent)},
                  {"gpu_compute_share", jnum(d.gpu_compute_share)},
                  {"io_read_limit", jint(d.io_read_limit)},
                  {"io_write_limit", jint(d.io_write_limit)},
                  {"background_threads_max", jint(d.background_threads_max)},
                  {"valid_until_s", jnum(d.valid_until_s)},
                  {"pressure_state", jstr(d.pressure_state)}}));
    } else {
        fields.emplace_back("grant", jnull());
    }
    fields.emplace_back("signature", jstr(decision_signature(d)));
    return jobj(std::move(fields));
}

GrantContext make_context(const Snapshot& snap, const RulesDoc& rules,
                          std::string_view workload_class,
                          int class_outstanding, double now_unix) {
    GrantContext ctx;
    ctx.now_unix = now_unix;
    ctx.emergency = snap.disabled || snap.mode == "emergency";
    ctx.knobs = resolve_knobs(rules.defaults);
    ctx.ram_available_bytes =
        static_cast<long long>(snap.mem_avail_mb) * 1024 * 1024;
    if (snap.concurrency_budget.has_value()) {
        const ConcurrencyBudget& b = *snap.concurrency_budget;
        ctx.pressure = b.pressure;
        ctx.generation = b.generation;
        if (workload_class == kBenchmarkClass) {
            ctx.class_known = true;
            ctx.class_quota = ctx.knobs.benchmark_quota;
            ctx.class_paused = ctx.knobs.benchmark_quota <= 0;
        } else if (const auto cls = work_class_from_name(workload_class)) {
            ctx.class_known = true;
            const ClassBudget& cb = b.classes[static_cast<int>(*cls)];
            ctx.class_quota = cb.quota;
            ctx.class_paused = cb.paused;
        }
    }
    ctx.class_outstanding = std::max(0, class_outstanding);
    /* GPU 政策：有效模式的 gpu_enabled / vram_budget_percent。 */
    if (snap.has_mode && !snap.mode.empty()) {
        const auto mode_defaults = rules.defaults_for(snap.mode);
        auto ge = mode_defaults.find("gpu_enabled");
        ctx.gpu_enabled =
            ge != mode_defaults.end() && json_is_true(&ge->second);
        auto vp = mode_defaults.find("vram_budget_percent");
        if (vp != mode_defaults.end())
            ctx.vram_budget_percent =
                std::clamp(json_num_or(&vp->second, 0.0), 0.0, 100.0);
    }
    return ctx;
}

namespace {

/* 過期/緊急撤銷（§58-§59）：valid_until 已過或 emergency → REVOKED。 */
int expire_live_grants(const fs::path& state_dir, const fs::path& grant_dir,
                       std::map<std::string, LiveGrant>& live,
                       const Snapshot& snap, bool emergency,
                       double now_unix) {
    int revoked = 0;
    for (auto& [id, lg] : live) {
        if (!emergency && now_unix <= lg.valid_until_s) continue;
        GrantDecision d;
        d.response = GrantResponse::Revoked;
        d.reason = emergency ? "emergency-revoke" : "grant-expired";
        d.grant_id = "rg-" + id;
        d.pressure_state = std::string(grant_pressure_name(
            snap.concurrency_budget ? snap.concurrency_budget->pressure
                                    : PressureTier::None,
            emergency));
        ResourceRequest stub;
        stub.request_id = id;
        stub.workload_id = lg.workload_id;
        stub.workload_class = lg.workload_class;
        jsonlite::JsonValue out = decision_to_json(stub, d);
        obj_set(out, "grant_expired_at", jnum(lg.valid_until_s));
        write_text(grant_dir / (id + ".json"),
                   jsonlite::json_serialize(out));
        append_audit(state_dir, d.reason, id, lg.workload_id, jnull(),
                     now_unix);
        ++revoked;
        lg.valid_until_s = -1;
    }
    return revoked;
}

/* <file>.release.json → 移除 request＋grant，稽核 release。 */
bool handle_release(const fs::path& state_dir, const fs::path& req_dir,
                    const fs::path& grant_dir, const std::string& req_id,
                    const fs::path& marker,
                    const std::map<std::string, LiveGrant>& live,
                    double now_unix) {
    std::error_code ec;
    fs::remove(marker, ec);
    fs::remove(req_dir / (req_id + ".request.json"), ec);
    fs::remove(grant_dir / (req_id + ".json"), ec);
    auto it = live.find(req_id);
    append_audit(state_dir, "release", req_id,
                 it != live.end() ? it->second.workload_id : "", jnull(),
                 now_unix);
    return true;
}

/* <file>.renew.json → 現存有效 grant 的 valid_until 順延（§59）。 */
bool handle_renew(const fs::path& state_dir, const fs::path& grant_dir,
                  const std::string& req_id, const fs::path& marker,
                  const std::map<std::string, LiveGrant>& live,
                  const RulesDoc& rules, double now_unix) {
    std::error_code ec;
    fs::remove(marker, ec);
    auto it = live.find(req_id);
    if (it == live.end() || it->second.valid_until_s <= now_unix)
        return false;
    jsonlite::JsonValue doc;
    const fs::path grant_path = grant_dir / (req_id + ".json");
    if (!read_json_file(grant_path, doc)) return false;
    const double until =
        now_unix + resolve_knobs(rules.defaults).grant_ttl_s;
    if (const jsonlite::JsonValue* g = doc.get("grant"))
        if (g->type == jsonlite::JsonValue::Type::Object) {
            jsonlite::JsonValue mutable_doc = std::move(doc);
            for (auto& kv : mutable_doc.object)
                if (kv.first == "grant")
                    obj_set(kv.second, "valid_until_s", jnum(until));
            write_text(grant_path, jsonlite::json_serialize(mutable_doc));
        }
    append_audit(state_dir, "renew", req_id, it->second.workload_id,
                 jnull(), now_unix);
    return true;
}

/* 單一 request 檔：解析 → 裁決 → 變動才重寫 grant 檔並稽核（resize 語義：
 * quota 縮小時本函式自動以更小上限重寫既有 grant）。 */
void adjudicate_request_file(const fs::path& state_dir,
                             const fs::path& grant_dir,
                             const fs::path& file, const Snapshot& snap,
                             const RulesDoc& rules,
                             const std::map<std::string, LiveGrant>& live,
                             std::map<std::string, int>& outstanding,
                             double now_unix, GrantCycleStats& stats) {
    ++stats.requests;
    const std::string fname = file.filename().string();
    const std::string req_id = fname.substr(0, fname.size() - 13);
    jsonlite::JsonValue doc;
    GrantDecision decision;
    ResourceRequest request;
    if (!read_json_file(file, doc)) {
        decision.response = GrantResponse::Denied;
        decision.reason = "malformed:unreadable-json";
        decision.grant_id = "rg-" + req_id;
        request.request_id = req_id;
    } else if (const auto parsed = parse_request(doc)) {
        request = *parsed;
        decision = adjudicate(
            request,
            make_context(snap, rules, request.workload_class,
                         outstanding[request.workload_class], now_unix));
        if (decision.response == GrantResponse::Granted ||
            decision.response == GrantResponse::Partial)
            outstanding[request.workload_class] += decision.cpu_threads_max;
    } else {
        decision.response = GrantResponse::Denied;
        decision.reason = "malformed:" + parsed.error();
        decision.grant_id = "rg-" + req_id;
        request.request_id = req_id;
        request.workload_id = json_str(doc.get("workload_id"));
    }
    std::error_code ec;
    const fs::path out_path = grant_dir / (req_id + ".json");
    const std::string sig = decision_signature(decision);
    auto prev = live.find(req_id);
    if ((prev == live.end() || prev->second.signature != sig) ||
        !fs::exists(out_path, ec)) {
        write_text(out_path,
                   jsonlite::json_serialize(decision_to_json(request, decision)));
        append_audit(state_dir, std::string(response_name(decision.response)),
                     request.request_id, request.workload_id,
                     jobj({{"signature", jstr(sig)},
                           {"reason", jstr(decision.reason)}}),
                     now_unix);
    }
    switch (decision.response) {
        case GrantResponse::Granted: ++stats.granted; break;
        case GrantResponse::Partial: ++stats.partial; break;
        case GrantResponse::Deferred: ++stats.deferred; break;
        case GrantResponse::Revoked: ++stats.revoked; break;
        case GrantResponse::Denied: ++stats.denied; break;
    }
}

}  // namespace

GrantCycleStats run_grant_cycle(const fs::path& state_dir,
                                const Snapshot& snap, const RulesDoc& rules,
                                double now_unix) {
    GrantCycleStats stats;
    const fs::path req_dir = state_dir / "resource-requests";
    const fs::path grant_dir = state_dir / "resource-grants";
    std::error_code ec;
    fs::create_directories(grant_dir, ec);
    auto live = load_live_grants(grant_dir);
    const bool emergency = snap.disabled || snap.mode == "emergency";
    stats.revoked = expire_live_grants(state_dir, grant_dir, live, snap,
                                       emergency, now_unix);
    if (!fs::exists(req_dir, ec)) return stats;
    /* 每類別 outstanding 由現存有效 grant 累計（跨 request 不超額）。 */
    std::map<std::string, int> outstanding;
    for (const auto& [id, lg] : live)
        if (lg.valid_until_s > 0)
            outstanding[lg.workload_class] += lg.cpu_threads_max;
    for (const auto& entry : fs::directory_iterator(req_dir, ec)) {
        if (!entry.is_regular_file()) continue;
        const std::string fname = entry.path().filename().string();
        auto ends = [&](std::string_view suffix) {
            return fname.size() > suffix.size() &&
                   fname.compare(fname.size() - suffix.size(),
                                 suffix.size(), suffix) == 0;
        };
        if (ends(".release.json")) {
            if (handle_release(state_dir, req_dir, grant_dir,
                               fname.substr(0, fname.size() - 13),
                               entry.path(), live, now_unix))
                ++stats.released;
        } else if (ends(".renew.json")) {
            if (handle_renew(state_dir, grant_dir,
                             fname.substr(0, fname.size() - 11),
                             entry.path(), live, rules, now_unix))
                ++stats.renewed;
        } else if (ends(".request.json")) {
            adjudicate_request_file(state_dir, grant_dir, entry.path(), snap,
                                    rules, live, outstanding, now_unix,
                                    stats);
        }
    }
    /* 已無 request 檔的孤兒 grant → 稽核 orphan-drop 並刪除。 */
    for (const auto& [id, lg] : live) {
        if (lg.valid_until_s <= 0) continue;
        if (!fs::exists(req_dir / (id + ".request.json"), ec)) {
            fs::remove(grant_dir / (id + ".json"), ec);
            append_audit(state_dir, "orphan-drop", id, lg.workload_id,
                         jnull(), now_unix);
        }
    }
    return stats;
}

}  // namespace grants
}  // namespace governor
}  // namespace gptbridge

/* tool_host_observe.cpp — 觀測面：health_snapshot／metrics_snapshot
 * （GovernedToolRuntime.health_snapshot／_collect_channel_metrics
 * parity）。純唯讀快照路徑；Impl 與線上助手見
 * tool_host_internal.h。非 Windows → 空 TU。
 */
#include "tool_host_internal.h"

#ifdef _WIN32

namespace gptbridge {
namespace toolhost {

using namespace detail;

jsonlite::JsonValue ToolHost::health_snapshot() const {
    /* channel_health：Python ChannelHealth.as_dict 形狀 */
    jl::JsonValue ch;
    ch.type = jl::JsonValue::Type::Object;
    {
        std::lock_guard<std::mutex> lk(impl_->health_mu);
        for (const auto& kv : impl_->ch_health) {
            ch.object.emplace_back(
                kv.first,
                jobj({{"channel_id", jstr(kv.first)},
                      {"last_ok", jbool(kv.second.last_ok)},
                      {"last_request_at",
                       jstr(kv.second.last_request_at)},
                      {"consecutive_failures",
                       jnum(static_cast<double>(
                           kv.second.consecutive_failures))},
                      {"degraded",
                       jbool(gptbridge_gt_health_degraded(
                           static_cast<int32_t>(
                               kv.second.consecutive_failures)) != 0)}}));
        }
    }

    jl::JsonValue channels;
    channels.type = jl::JsonValue::Type::Array;
    jl::JsonValue routes;
    routes.type = jl::JsonValue::Type::Object;
    {
        std::vector<std::string> sorted = impl_->cfg.process_channels;
        std::sort(sorted.begin(), sorted.end());
        for (const auto& c : sorted) {
            jl::JsonValue v;
            v.type = jl::JsonValue::Type::String;
            v.string = c;
            channels.array.push_back(v);
            routes.object.emplace_back(
                c, jstr(c + "-channel/" + impl_->cfg.tool_id));
        }
    }

    jl::JsonValue duty;
    duty.type = jl::JsonValue::Type::Array;
    for (const char* d :
         {"information-delivery-channels", "channel-health",
          "automatic-cleanup", "automatic-repair",
          "automatic-backup-coordination"}) {
        jl::JsonValue v;
        v.type = jl::JsonValue::Type::String;
        v.string = d;
        duty.array.push_back(v);
    }
    jl::JsonValue under;
    under.type = jl::JsonValue::Type::Array;
    for (const char* u : {"system", "maintenance"}) {
        jl::JsonValue v;
        v.type = jl::JsonValue::Type::String;
        v.string = u;
        under.array.push_back(v);
    }

    jl::JsonValue snap = jobj({
        {"ok", jbool(true)},
        {"role", jstr("tool-runtime-sub-sovereign")},
        {"sovereign_id", jstr("system")},
        {"authority",
         jstr("information-management-delivery-channels-and-"
              "channel-health-and-automatic-cleanup-repair-backup")},
        {"scope",
         jstr("all-owned-channel-delivery-health-and-local-"
              "maintenance-duties")},
        {"duty", duty},
        {"subordinate_to", under},
        {"version", jstr(impl_->cfg.version)},
        {"tool_id", jstr(impl_->cfg.tool_id)},
        {"runtime_scope", jstr("independent-tool")},
        {"governance_ready", jbool(true)},
        {"workspace_instance_id",
         jstr(impl_->cfg.workspace_instance_id)},
        {"channels", channels},
        {"channel_routes", routes},
        {"channel_health", ch},
        {"dual_track", jbool(impl_->cfg.dual_track)},
    });
    if (impl_->cfg.self_repair_enabled &&
        impl_->hooks.self_repair_health) {
        snap.object.emplace_back(
            "_self_repair", impl_->hooks.self_repair_health());
    }
    if (impl_->cfg.local_cleanup_enabled) {
        jl::JsonValue lc;
        if (impl_->hooks.cleanup_health) {
            lc = impl_->hooks.cleanup_health();
        } else {
            lc = jobj({{"local_cleanup",
                        jobj({{"enabled", jbool(true)},
                              {"completed", jbool(false)}})}});
        }
        if (lc.type == jl::JsonValue::Type::Object)
            snap.object.emplace_back("_local_cleanup", std::move(lc));
    }
    if (impl_->hooks.health_extras) {
        jl::JsonValue extra = impl_->hooks.health_extras();
        if (extra.type == jl::JsonValue::Type::Object)
            for (auto& kv : extra.object)
                snap.object.push_back(std::move(kv));
    }
    return snap;
}

jsonlite::JsonValue ToolHost::metrics_snapshot() const {
    /* _collect_channel_metrics() parity */
    jl::JsonValue ch;
    ch.type = jl::JsonValue::Type::Object;
    {
        std::lock_guard<std::mutex> lk(impl_->health_mu);
        for (const auto& kv : impl_->ch_health) {
            ch.object.emplace_back(
                kv.first,
                jobj({{"channel_id", jstr(kv.first)},
                      {"last_ok", jbool(kv.second.last_ok)},
                      {"last_request_at",
                       jstr(kv.second.last_request_at)},
                      {"consecutive_failures",
                       jnum(static_cast<double>(
                           kv.second.consecutive_failures))},
                      {"degraded",
                       jbool(gptbridge_gt_health_degraded(
                           static_cast<int32_t>(
                               kv.second.consecutive_failures)) != 0)}}));
        }
    }
    jl::JsonValue processing;
    processing.type = jl::JsonValue::Type::Array;
    for (const auto& c : impl_->cfg.process_channels) {
        jl::JsonValue v;
        v.type = jl::JsonValue::Type::String;
        v.string = c;
        processing.array.push_back(v);
    }
    int64_t waiters = 0;
    {
        std::lock_guard<std::mutex> lk(impl_->waiter_mu);
        waiters = static_cast<int64_t>(impl_->waiters.size());
    }
    int64_t pending = 0;
    {
        std::lock_guard<std::mutex> lk(impl_->pending_mu);
        pending = static_cast<int64_t>(impl_->pending_conns.size());
    }
    return jobj({
        {"channel_health", ch},
        {"worker_queue_size", jnum(static_cast<double>(waiters))},
        {"conn_queue_size", jnum(static_cast<double>(pending))},
        {"conn_workers", jnum(static_cast<double>(impl_->conn_workers))},
        {"conn_rejected",
         jnum(static_cast<double>(impl_->n_conn_rejected.load()))},
        {"processing_channels", processing},
        {"notification_queue_size", jnum(0)},
        {"last_notification", jl::JsonValue{}},
        {"uptime_seconds",
         jnum((impl_->now_ms() - impl_->started_ms.load()) / 1000.0)},
    });
}

} // namespace toolhost
} // namespace gptbridge

#endif /* _WIN32 */

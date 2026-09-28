/* tool_host_claims.cpp — §4 claim/execute/respond 迴圈：通道輪詢
 * 取件、executor 執行、取消輪詢、respond 回推。claim 執行緒為單一
 * 生命週期執行緒（非 per-request）；executor 以 request 為單位的
 * 工作緒由 claim 執行緒逐一驅動（bounded-concurrency/v1：通道
 * claim 本身即天然 admission——一次一件）。Impl 與線上助手見
 * tool_host_internal.h。非 Windows → 空 TU。
 */
#include "tool_host_internal.h"

#ifdef _WIN32

namespace gptbridge {
namespace toolhost {

using namespace detail;

void ToolHost::claim_loop() {
    int64_t idle_ms = GPTBRIDGE_GT_IDLE_INITIAL_MS;
    /* §4 通知加速（Python _listen_for_notifications 的單執行緒對映）：
       佇列空轉等待期間以 notification_stamp 探針輪詢各 process 通道
       ——250ms 週期；store 寫入戳變化即中斷等待、idle 重置 250ms 並
       即刻重取，不枯等 backoff。回 null（central PG 傳輸無本地訊號）
       或探針失敗 → 該輪略過（Python 對 probe 例外吞沒、不計
       channel_health——此處同）。 */
    std::map<std::string, std::pair<int64_t, int64_t>> last_stamps;
    while (!impl_->stop_flag.load()) {
        bool got = false;
        for (const auto& ch : impl_->cfg.process_channels) {
            if (impl_->stop_flag.load()) return;
            tpx::ProxyResponse resp;
            if (!impl_->proxy("claim", tpx::args_channel(ch), &resp)) {
                impl_->n_claim_fail.fetch_add(1);
                impl_->ch_ok(ch, false);
                Sleep(500); /* claim 失敗 0.5s 退避（§4） */
                continue;
            }
            impl_->ch_ok(ch, true);
            if (!resp.ok) {
                impl_->n_claim_fail.fetch_add(1);
                Sleep(500);
                continue;
            }
            /* 線路形狀：result = {"request": row|null}。 */
            const jl::JsonValue* row =
                (resp.result.type == jl::JsonValue::Type::Object)
                    ? resp.result.get("request")
                    : nullptr;
            if (row == nullptr ||
                row->type != jl::JsonValue::Type::Object)
                continue; /* null → 空佇列 */
            got = true;
            impl_->n_claims.fetch_add(1);
            execute_claimed(ch, *row);
        }
        if (got) {
            idle_ms = GPTBRIDGE_GT_IDLE_INITIAL_MS;
            continue;
        }
        /* Python：wait_timeout = max(idle_poll, 0.05) 用**當前**
           idle_poll，逾時才 ×1.5；notify 命中 → 重置 0.25。 */
        const int64_t wait =
            gptbridge_gt_wait_timeout_ms(idle_ms, 0);
        const int64_t deadline = impl_->now_ms() + wait;
        int64_t next_probe = impl_->now_ms();
        bool notified = false;
        while (!impl_->stop_flag.load()) {
            const int64_t now = impl_->now_ms();
            if (now >= deadline) break;
            if (now >= next_probe) {
                bool changed = false;
                for (const auto& ch : impl_->cfg.process_channels) {
                    tpx::ProxyResponse st;
                    if (!impl_->proxy("notification_stamp",
                                      tpx::args_channel(ch), &st) ||
                        !st.ok)
                        continue;
                    /* result 形狀：[int,int]（本地 store 寫入戳）或
                       null（PG 傳輸 → 無本地訊號，跳過）。 */
                    const auto& arr = st.result.array;
                    if (st.result.type != jl::JsonValue::Type::Array ||
                        arr.size() < 2 ||
                        arr[0].type != jl::JsonValue::Type::Number ||
                        arr[1].type != jl::JsonValue::Type::Number) {
                        last_stamps.erase(ch);
                        continue;
                    }
                    const std::pair<int64_t, int64_t> stamp{
                        static_cast<int64_t>(arr[0].number),
                        static_cast<int64_t>(arr[1].number)};
                    auto it = last_stamps.find(ch);
                    if (it == last_stamps.end() || it->second != stamp) {
                        last_stamps[ch] = stamp;
                        changed = true; /* 首次觀測亦視為變化（Python 同） */
                    }
                }
                next_probe = now + 250;
                if (changed) { notified = true; break; }
            }
            const int64_t slice =
                (std::min)(deadline, next_probe) - now;
            if (slice <= 0) continue;
            Sleep(static_cast<DWORD>((std::min)(slice, int64_t(10))));
        }
        idle_ms = gptbridge_gt_idle_next_ms(idle_ms, notified ? 1 : 0);
    }
}

void ToolHost::execute_claimed(const std::string& channel,
                               const jl::JsonValue& row) {
    const jl::JsonValue* rid = get_str(row, "request_id");
    const jl::JsonValue* payload = row.get("payload");
    const std::string request_id = rid ? rid->string : "";
    jl::JsonValue pl =
        (payload && payload->type == jl::JsonValue::Type::Object)
            ? *payload
            : jl::JsonValue{};
    std::string command;
    std::vector<std::pair<std::string, jl::JsonValue>> kept;
    kept.reserve(pl.object.size());
    for (auto& kv : pl.object) {
        if (kv.first == "_governed_command") {
            if (kv.second.type == jl::JsonValue::Type::String)
                command = kv.second.string;
            continue;
        }
        kept.push_back(std::move(kv));
    }
    pl.object = std::move(kept);
    /* Python：payload["_governed_requester_actor"] = requester_actor。 */
    const jl::JsonValue* ra = row.get("requester_actor");
    const std::string requester =
        (ra && ra->type == jl::JsonValue::Type::String) ? ra->string : "";
    pl.object.emplace_back("_governed_requester_actor",
                           jstr(requester));
    const bool is_local_cleanup =
        command == "toolbox_run_local_cleanup";

    /* 前置失敗／LOCAL_CLEANUP 非 governance actor → DENIED result
       （Python except 路徑：respond＋waiter 推送、不進 executor）。 */
    const bool early_denied =
        rid == nullptr || payload == nullptr ||
        payload->type != jl::JsonValue::Type::Object ||
        command.empty() ||
        (is_local_cleanup &&
         (requester != "governance/main-system" ||
          !impl_->hooks.local_cleanup));
    if (early_denied) {
        jl::JsonValue denied = jobj({
            {"ok", jbool(false)},
            {"tool_id", jstr(impl_->cfg.tool_id)},
            {"request_id", jstr(request_id)},
            {"error_code", jstr("PERMISSION_DENIED")},
            {"message", jstr("PERMISSION_DENIED")}});
        tpx::ProxyResponse resp;
        impl_->proxy("respond",
                     tpx::args_respond(channel, request_id,
                                       jl::json_serialize(denied)),
                     &resp);
        impl_->push_waiter_result(request_id, command, denied);
        return;
    }

    auto flag = std::make_shared<std::atomic<bool>>(false);
    {
        std::lock_guard<std::mutex> lk(impl_->exec_mu);
        impl_->exec_flags[request_id] = flag;
    }

    /* §4：executor 跑於獨立執行緒；本執行緒每 100ms 輪詢
       request_cancelled——命中即立旗標＋呼叫 cancellation、
       **不 respond**（Python 同款）。 */
    std::atomic<bool> done{false};
    jl::JsonValue result;
    std::thread worker([this, &command, &pl, &request_id, flag, &done,
                        &result] {
        if (command == "toolbox_run_local_cleanup" &&
            impl_->hooks.local_cleanup) {
            result = impl_->hooks.local_cleanup();
        } else {
            result = impl_->hooks.executor(command, pl, request_id,
                                           *flag);
        }
        done.store(true);
    });

    bool cancelled = false;
    while (!impl_->stop_flag.load()) {
        /* 旗標先於 done 檢查：取消命中於執行收尾窗口（executor 因旗標
           提前結束）仍須判 cancelled——ABI §4：執行中取消不 respond。
           Python asyncio 以 request_cancelled 輪詢命中為準；本機旗標
           是 toolbox_cancel_tool_run 的等價本機通道。 */
        if (flag->load()) { cancelled = true; break; }
        if (done.load()) break;
        tpx::ProxyResponse resp;
        /* 線路形狀：result 為純 bool。 */
        if (impl_->proxy("request_cancelled",
                         tpx::args_request_id(channel, request_id),
                         &resp) &&
            resp.ok &&
            resp.result.type == jl::JsonValue::Type::Bool &&
            resp.result.boolean) {
            flag->store(true);
            if (impl_->hooks.cancellation)
                impl_->hooks.cancellation(request_id);
            cancelled = true;
            break;
        }
        const int64_t until = impl_->now_ms() + 100;
        while (!done.load() && !impl_->stop_flag.load() &&
               impl_->now_ms() < until)
            Sleep(5);
    }
    if (worker.joinable()) worker.join();
    {
        std::lock_guard<std::mutex> lk(impl_->exec_mu);
        impl_->exec_flags.erase(request_id);
    }

    if (cancelled) {
        impl_->n_cancelled.fetch_add(1);
        std::lock_guard<std::mutex> lk(impl_->waiter_mu);
        impl_->waiters.erase(request_id); /* 不回推 _result */
        return;
    }
    impl_->n_executed.fetch_add(1);

    /* respond（僅 claimed 成功——代理/傳輸層保證語義）。 */
    jl::JsonValue response = result;
    if (response.type != jl::JsonValue::Type::Object)
        response = jobj({{"value", response}});
    /* Python result["request_id"] = …：覆寫而非重複鍵。 */
    bool replaced = false;
    for (auto& kv : response.object) {
        if (kv.first == "request_id") {
            kv.second = jstr(request_id); replaced = true;
        }
    }
    if (!replaced)
        response.object.emplace_back("request_id", jstr(request_id));
    tpx::ProxyResponse resp;
    if (impl_->proxy("respond",
                     tpx::args_respond(channel, request_id,
                                       jl::json_serialize(response)),
                     &resp) &&
        resp.ok) {
        impl_->n_responded.fetch_add(1);
        impl_->ch_ok(channel, true);
    } else {
        impl_->n_claim_fail.fetch_add(1);
        impl_->ch_ok(channel, false);
        /* respond 失敗 → PERMISSION_DENIED 形式（Python except 路徑）。 */
        response = jobj({
            {"ok", jbool(false)},
            {"tool_id", jstr(impl_->cfg.tool_id)},
            {"request_id", jstr(request_id)},
            {"error_code", jstr("PERMISSION_DENIED")},
            {"message", jstr("PERMISSION_DENIED")}});
    }
    impl_->push_waiter_result(request_id, command, response);
}

} // namespace toolhost
} // namespace gptbridge

#endif /* _WIN32 */

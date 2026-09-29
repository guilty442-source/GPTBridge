/* logic.cpp — serial run queue + event routing (egui queue.rs /
 * runs.rs / profiles.rs semantics, Win32-free). */
#include "state.h"

#include <windows.h>
#include <shellapi.h>

#include "parse.h"
#include "protocol.h"
#include "strings.h"

namespace fsui {

using namespace tr;
namespace P = proto;

static constexpr const char* kToolId = "file-sorter";
static constexpr unsigned kRunTimeout = 30 * 60;
static constexpr unsigned kShortTimeout = 20;
static constexpr unsigned kFolderScanTimeout = 120;

static bool file_exists(const std::string& path) {
    DWORD attr = GetFileAttributesA(path.c_str());
    return attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY);
}

static bool dir_exists(const std::string& path) {
    DWORD attr = GetFileAttributesA(path.c_str());
    return attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY);
}

void AppState::enqueue(RunKind kind, std::vector<std::string> args,
                       unsigned timeout_s, const char* running,
                       const char* success) {
    queue.push_back(QueuedRun{kind, std::move(args), timeout_s, running, success});
}

void AppState::pump_queue() {
    if (has_active || !connected() || queue.empty()) return;
    QueuedRun run = queue.front();
    queue.pop_front();
    std::string rid = P::next_request_id();
    std::string frame = P::encode_run_tool(rid, kToolId, run.args, run.timeout_s);
    if (!backend.send_text(frame)) {
        fail_run(run, kMsgNoBackend);
        return;
    }
    if (run.kind == RunKind::Cleanup) {
        cleanup_state = RunState::Running;
        cleanup_message = run.running_label;
    } else {
        run_state = RunState::Running;
        message = run.running_label;
    }
    active.request_id = rid;
    active.kind = run.kind;
    active.desired = (run.kind == RunKind::SetProfileEnabled ||
                      run.kind == RunKind::SetDuplicateTrash) &&
                             !run.args.empty() && run.args.back() == "true"
                         ? 1
                         : (run.kind == RunKind::SetProfileEnabled ||
                                    run.kind == RunKind::SetDuplicateTrash
                                ? 0
                                : -1);
    has_active = true;
    pending_running_label = run.running_label;
    pending_success_label = run.success_label;
}

void AppState::fail_run(const QueuedRun& run, const std::string& msg) {
    if (run.kind == RunKind::Cleanup) {
        cleanup_state = RunState::Error;
        cleanup_message = msg;
    } else {
        run_state = RunState::Error;
        message = msg;
    }
}

void AppState::reset_workspace() {
    target_validated = false;
    auto_organize = false;
    dup_trash = false;
    keyword_folder.clear();
    destination_folders.clear();
    folder_scan_status.clear();
    auto_organize_status.clear();
    dup_trash_status.clear();
    migration_review = false;
    folders_loaded = false;
    profile_loaded = false;
    run_state = RunState::Idle;
    message = kMsgReady;
    output.clear();
    cleanup_state = RunState::Idle;
    cleanup_message = kMsgCleanupReady;
    cleanup_progress = JsonValue{};
    has_cleanup_progress = false;
    cleanup_files.clear();
    cleanup_output.clear();
    sort_plan = JsonValue{};
    has_plan = false;
    plan_confirmed = false;
    history_open = false;
    history_output.clear();
}

void AppState::validate_target() {
    std::string candidate = fsp::trim(target_dir);
    if (candidate.empty() || !dir_exists(candidate)) {
        target_validated = false;
        folder_scan_status = kTargetBad;
        return;
    }
    if (target_validated && candidate == target_dir) return;
    target_dir = candidate;
    reset_workspace();
    target_validated = true;
    enqueue(RunKind::SelectScanTarget, {candidate, "--select-scan-target"},
            kShortTimeout, kMsgSelectTarget, kMsgTargetDone);
    enqueue(RunKind::Profiles, {candidate, "--profiles-json"},
            kShortTimeout, kMsgProfiles, kMsgProfilesDone);
    if (auto_scan_folders) enqueue_folder_scan();
}

void AppState::enqueue_folder_scan() {
    std::string target = fsp::trim(target_dir);
    if (target.empty()) return;
    folder_scan_status = kMsgScanningFld;
    enqueue(RunKind::ListFolders, {target, "--list-folders"},
            kFolderScanTimeout, kMsgScanFolders, kMsgFoldersDone);
}

void AppState::append_history(const char* action, bool ok,
                              const std::string& detail) {
    history_entries.insert(history_entries.begin(),
                           HistoryEntry{ok, action, detail});
    if (history_entries.size() > 20) history_entries.resize(20);
}

void AppState::request_stop_cleanup() {
    if (cleanup_state != RunState::Running || cleanup_stop_requested) return;
    cleanup_stop_requested = true;
    cleanup_message = kMsgCleanupStop;
    if (!has_active || active.kind != RunKind::Cleanup) return;
    backend.send_text(P::encode_cancel_run(kToolId, active.request_id));
}

std::vector<std::string> AppState::build_cleanup_args() const {
    std::vector<std::string> args = {
        fsp::trim(target_dir), "--cleanup-scan", "--json"};
    if (cleanup_image_issues) args.push_back("--image-cleanup");
    if (cleanup_similar_images) args.push_back("--similar-image-analysis");
    if (cleanup_video_issues) args.push_back("--video-cleanup");
    if (cleanup_similar_videos) args.push_back("--similar-video-analysis");
    args.push_back("--similar-video-threshold");
    args.push_back(std::to_string((unsigned long long)cleanup_threshold));
    args.push_back("--analysis-speed");
    args.push_back(std::to_string((unsigned long long)cleanup_speed));
    args.push_back("--model-temperature");
    args.push_back(std::to_string(model_temperature));
    args.push_back("--model-top-p");
    args.push_back(std::to_string(model_top_p));
    args.push_back("--model-context-window");
    args.push_back(std::to_string(model_context_window));
    args.push_back("--model-max-output-tokens");
    args.push_back(std::to_string(model_max_tokens));
    if (!cleanup_parallel) args.push_back("--no-parallel-analysis");
    return args;
}

void AppState::start_cleanup() {
    if (!can_run()) return;
    cleanup_state = RunState::Running;
    JsonValue progress;
    progress.type = JsonValue::Type::Object;
    JsonValue ph; ph.type = JsonValue::Type::String; ph.string = "scan_started";
    JsonValue msg; msg.type = JsonValue::Type::String; msg.string = kMsgScanStarted;
    progress.object.emplace_back("phase", ph);
    progress.object.emplace_back("message", msg);
    cleanup_progress = progress;
    has_cleanup_progress = true;
    cleanup_files.clear();
    cleanup_output.clear();
    cleanup_stop_requested = false;
    enqueue(RunKind::Cleanup, build_cleanup_args(), kRunTimeout,
            kMsgCleanupRun, kMsgCleanupDone);
}

void AppState::handle_event(const std::string& event, const JsonValue& payload) {
    std::string rid = P::jstr(payload, "request_id");
    bool matches = has_active && active.request_id == rid;
    if (event == "toolbox_run_tool_progress" && matches &&
        active.kind == RunKind::Cleanup) {
        cleanup_progress = payload;
        has_cleanup_progress = true;
        cleanup_message = fsp::progress_text(payload);
        return;
    }
    if (event == "toolbox_run_tool_result" && matches) {
        ActiveRun run = active;
        has_active = false;
        finish_run(run, payload);
        return;
    }
    if (event == "error" && matches) {
        ActiveRun run = active;
        has_active = false;
        std::string msg = P::jstr(payload, "message");
        finish_run_error(run, msg.empty() ? kMsgBackendErr : msg);
        return;
    }
}

void AppState::on_socket_closed() {
    if (has_active) {
        ActiveRun run = active;
        has_active = false;
        finish_run_error(run, kMsgDisconnected);
    }
}

void AppState::finish_run_error(const ActiveRun& run, const std::string& msg) {
    switch (run.kind) {
        case RunKind::Cleanup:
            if (cleanup_stop_requested) {
                cleanup_state = RunState::Idle;
                cleanup_message = kMsgCleanupStopped;
            } else {
                cleanup_state = RunState::Error;
                cleanup_message = msg;
            }
            break;
        case RunKind::ListFolders: folder_scan_status = msg; break;
        case RunKind::Profiles:
        case RunKind::SelectScanTarget:
        case RunKind::SetProfileEnabled: auto_organize_status = msg; break;
        case RunKind::SetDuplicateTrash: dup_trash_status = msg; break;
        default:
            run_state = RunState::Error;
            message = msg;
            break;
    }
}

void AppState::finish_run(const ActiveRun& run, const JsonValue& payload) {
    bool ok = P::jbool(payload, "ok");
    bool cancelled = P::jbool(payload, "cancelled");
    std::string so = P::jstr(payload, "stdout");
    std::string msg = P::jstr(payload, "message");
    std::string out = fsp::format_run_output(payload);
    if (!ok) {
        finish_run_error(run, msg.empty() ? kMsgRunFailed : msg);
        return;
    }
    switch (run.kind) {
        case RunKind::ListFolders: {
            auto folders = fsp::parse_destination_folders(so);
            bool keep = false;
            for (const auto& f : folders)
                if (f == keyword_folder) keep = true;
            if (!keep) keyword_folder.clear();
            folder_scan_status = folders.empty()
                ? kMsgNoFolders
                : "已找到 " + std::to_string(folders.size()) + " 個目的地資料夾";
            destination_folders = folders;
            folders_loaded = true;
            break;
        }
        case RunKind::Profiles:
        case RunKind::SelectScanTarget: {
            JsonValue profile;
            if (fsp::parse_profile(so, target_dir, &profile)) {
                const JsonValue* en = profile.get("enabled");
                if (en && en->type == JsonValue::Type::Bool)
                    apply_profile(profile, en->boolean);
            } else {
                migration_review = false;
                auto_organize = false;
                dup_trash = false;
                auto_organize_status = kProfileOff;
                dup_trash_status = kDupTrashOffInit;
            }
            profile_loaded = true;
            break;
        }
        case RunKind::SetProfileEnabled: {
            bool enabled = run.desired == 1;
            auto_organize = enabled;
            if (enabled) migration_review = false;
            auto_organize_status = enabled ? kProfileOn : kProfileOffDone;
            break;
        }
        case RunKind::SetDuplicateTrash: {
            bool enabled = run.desired == 1;
            dup_trash = enabled;
            dup_trash_status = enabled ? kDupTrashOn : kDupTrashOff;
            break;
        }
        case RunKind::Preview: {
            run_state = RunState::Success;
            message = kMsgPreviewDone;
            output = out;
            JsonValue plan;
            /* usable when ok is absent-or-true and a plan_id exists */
            bool usable = fsp::parse_sort_plan(so, &plan) &&
                          P::jbool(plan, "ok", true) &&
                          !fsp::plan_id(plan).empty();
            if (usable) {
                append_history("預覽", true,
                               std::to_string(fsp::plan_action_count(plan)) +
                                   " 個動作，plan " + fsp::plan_id(plan));
                sort_plan = plan;
                has_plan = true;
                plan_confirmed = false;
            } else {
                run_state = RunState::Error;
                message = kMsgNoPlanId;
                append_history("預覽", false, "缺少可套用的 plan_id");
            }
            break;
        }
        case RunKind::ApplyPlan: {
            run_state = RunState::Success;
            message = kMsgApplyDone;
            output = out;
            append_history("套用計畫", true,
                           "plan " + (has_plan ? fsp::plan_id(sort_plan) : ""));
            sort_plan = JsonValue{};
            has_plan = false;
            plan_confirmed = false;
            break;
        }
        case RunKind::Undo: {
            run_state = RunState::Success;
            message = kMsgUndoDone;
            output = out;
            append_history("復原", true, "最近一次整理");
            break;
        }
        case RunKind::History: {
            run_state = RunState::Success;
            message = kMsgHistoryDone;
            history_output = so.empty() ? msg : so;
            break;
        }
        case RunKind::ListKeywords:
        case RunKind::MutateKeywords: {
            run_state = RunState::Success;
            message = pending_success_label;
            output = out;
            break;
        }
        case RunKind::Cleanup: {
            if (cancelled || cleanup_stop_requested) {
                cleanup_state = RunState::Idle;
                cleanup_message = kMsgCleanupStopped;
                return;
            }
            JsonValue report;
            if (fsp::parse_tool_json(so, &report)) {
                std::vector<JsonValue> files;
                if (const JsonValue* f = report.get("found_files");
                    f && f->type == JsonValue::Type::Array)
                    files = f->array;
                unsigned long long folder_count = 0;
                if (const JsonValue* c = report.get("scan_folder_count");
                    c && c->type == JsonValue::Type::Number)
                    folder_count = (unsigned long long)c->number;
                JsonValue progress;
                progress.type = JsonValue::Type::Object;
                auto put = [&](const char* k, JsonValue v) {
                    progress.object.emplace_back(k, std::move(v));
                };
                JsonValue ph; ph.type = JsonValue::Type::String;
                ph.string = "scan_completed"; put("phase", ph);
                JsonValue m; m.type = JsonValue::Type::String;
                m.string = kMsgCleanupDone; put("message", m);
                JsonValue fc; fc.type = JsonValue::Type::Number;
                fc.number = (double)folder_count;
                put("folder_current", fc); put("folder_total", fc);
                if (const JsonValue* sc = report.get("source_file_count"))
                    put("source_file_count", *sc);
                JsonValue nf; nf.type = JsonValue::Type::Number;
                nf.number = (double)files.size(); put("found_file_count", nf);
                cleanup_progress = progress;
                has_cleanup_progress = true;
                cleanup_state = RunState::Success;
                cleanup_message = files.empty()
                    ? "清理掃描完成，沒有找到候選項目"
                    : "清理掃描完成，找到 " + std::to_string(files.size()) +
                          " 個候選項目";
                cleanup_files = files;
                cleanup_output = out;
            } else {
                cleanup_state = RunState::Error;
                cleanup_message = kMsgCleanupBad;
                cleanup_output = out;
            }
            break;
        }
    }
}

void AppState::apply_profile(const JsonValue& profile, bool enabled) {
    bool review = P::jbool(profile, "migration_required_review");
    migration_review = review;
    auto_organize = enabled && !review;
    dup_trash = P::jbool(profile, "duplicate_trash_enabled");
    dup_trash_status = dup_trash ? kDupTrashOn : kDupTrashOff;
    if (review) {
        unsigned long long rejected = 0;
        if (const JsonValue* r = profile.get("migration_rejected_rule_count");
            r && r->type == JsonValue::Type::Number)
            rejected = (unsigned long long)r->number;
        auto_organize_status = rejected > 0
            ? "舊規則已完整保留並隔離（" + std::to_string(rejected) +
                  " 筆需確認）；確認後才能重新啟用自動分類"
            : kMigrateReview0;
    } else {
        auto_organize_status = enabled ? kProfileOn : kProfileClosed;
    }
}

void AppState::set_profile_enabled(bool enabled) {
    if (!connected() || fsp::trim(target_dir).empty()) {
        auto_organize_status = kProfileNeedConn;
        return;
    }
    if (enabled && migration_review && !ui_confirm(kConfirmMigrate)) return;
    migration_review = false;
    commit_profile_enabled(enabled);
}

void AppState::commit_profile_enabled(bool enabled) {
    auto_organize_status = enabled ? kProfileEnabling : kProfileDisabling;
    enqueue(RunKind::SetProfileEnabled,
            {fsp::trim(target_dir), "--set-profile-enabled",
             enabled ? "true" : "false"},
            kShortTimeout, kProfileChanging, kProfileChanged);
}

void AppState::set_dup_trash(bool enabled) {
    if (!connected() || fsp::trim(target_dir).empty()) {
        dup_trash_status = fsp::trim(target_dir).empty()
                               ? kNeedTarget
                               : kDupTrashNeedConn;
        return;
    }
    if (enabled && !ui_confirm(kConfirmDupTrash)) return;
    commit_dup_trash(enabled);
}

void AppState::commit_dup_trash(bool enabled) {
    dup_trash_status = enabled ? kDupTrashEnabling : kDupTrashDisabling;
    enqueue(RunKind::SetDuplicateTrash,
            {fsp::trim(target_dir), "--set-duplicate-trash-enabled",
             enabled ? "true" : "false"},
            kShortTimeout, kDupTrashChanging, kDupTrashChanged);
}

void AppState::reveal_path(const std::string& raw) const {
    std::string abs = raw;
    bool rooted = raw.size() > 2 && raw[1] == ':' &&
                  (raw[2] == '\\' || raw[2] == '/');
    if (!rooted) abs = fsp::trim(target_dir) + "\\" + raw;
    std::string arg = file_exists(abs)
        ? "/select,\"" + abs + "\""
        : "/select,\"" + abs.substr(0, abs.find_last_of("\\/")) + "\"";
    ShellExecuteA(nullptr, "explore", "explorer.exe", arg.c_str(), nullptr,
                  SW_SHOWNORMAL);
}

} // namespace fsui

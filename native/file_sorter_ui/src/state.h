/* state.h — file-sorter window state (egui tool_file_sorter port).
 * Pure data + logic; all Win32 access lives in app.cpp. */
#ifndef GPTBRIDGE_FSUI_STATE_H
#define GPTBRIDGE_FSUI_STATE_H

#include <deque>
#include <string>
#include <vector>

#include "jsonlite.h"
#include "parse.h"
#include "ws_client.h"

namespace fsui {

using gptbridge::jsonlite::JsonValue;

enum class RunKind {
    ListFolders, Profiles, SelectScanTarget, SetProfileEnabled,
    SetDuplicateTrash, Preview, ApplyPlan, Undo, History,
    ListKeywords, MutateKeywords, Cleanup,
};

enum class RunState { Idle, Running, Success, Error };

struct QueuedRun {
    RunKind kind;
    std::vector<std::string> args;
    unsigned timeout_s;
    std::string running_label;
    std::string success_label;
};

struct ActiveRun {
    std::string request_id;
    RunKind kind;
    int desired = -1; /* -1 none, else 0/1 for toggle runs */
};

struct HistoryEntry {
    bool ok;
    std::string action;
    std::string detail;
};

struct AppState {
    WsClient backend;
    std::deque<QueuedRun> queue;
    bool has_active = false;
    ActiveRun active;

    std::string ws_url;
    std::string target_dir;
    bool target_validated = false;
    bool auto_organize = false;
    std::string auto_organize_status;
    bool dup_trash = false;
    std::string dup_trash_status;
    bool migration_review = false;
    std::vector<std::string> destination_folders;
    bool auto_scan_folders = true;
    std::string folder_scan_status;
    bool folders_loaded = false;
    bool profile_loaded = false;

    std::string keyword_input;
    std::string keyword_folder;
    std::string current_keyword;
    std::string updated_keyword;
    std::vector<fsp::KeywordRule> keyword_rules; /* last --list-keywords */

    RunState run_state = RunState::Idle;
    std::string message;
    std::string output;
    JsonValue sort_plan; /* Null when none */
    bool has_plan = false;
    bool plan_confirmed = false;
    bool history_open = false;
    std::vector<HistoryEntry> history_entries;
    std::string history_output;

    RunState cleanup_state = RunState::Idle;
    std::string cleanup_message;
    std::string cleanup_output;
    JsonValue cleanup_progress; /* Null when none */
    bool has_cleanup_progress = false;
    std::vector<JsonValue> cleanup_files;
    bool cleanup_stop_requested = false;
    bool cleanup_image_issues = false;
    bool cleanup_similar_images = false;
    bool cleanup_video_issues = true;
    bool cleanup_similar_videos = false;
    bool cleanup_parallel = true;
    double cleanup_threshold = 96.0;
    double cleanup_speed = 50.0;
    double model_temperature = 0.0;
    double model_top_p = 0.9;
    unsigned model_context_window = 8192;
    unsigned model_max_tokens = 512;

    std::string pending_running_label;
    std::string pending_success_label;

    /* ---- logic (logic.cpp) ---- */
    bool connected() const { return backend.state() == WsClient::State::Connected; }
    bool busy() const {
        return run_state == RunState::Running || cleanup_state == RunState::Running;
    }
    bool can_run() const {
        return !busy() && target_validated && !target_dir.empty() && connected();
    }
    void enqueue(RunKind kind, std::vector<std::string> args, unsigned timeout_s,
                 const char* running, const char* success);
    void pump_queue();
    void fail_run(const QueuedRun& run, const std::string& message);
    void reset_workspace();
    void validate_target();
    void enqueue_folder_scan();
    void append_history(const char* action, bool ok, const std::string& detail);
    void request_stop_cleanup();
    std::vector<std::string> build_cleanup_args() const;
    void start_cleanup();
    void handle_event(const std::string& event, const JsonValue& payload);
    void on_socket_closed();
    void finish_run_error(const ActiveRun& run, const std::string& message);
    void finish_run(const ActiveRun& run, const JsonValue& payload);
    void apply_profile(const JsonValue& profile, bool enabled);
    void set_profile_enabled(bool enabled); /* confirm-gated */
    void commit_profile_enabled(bool enabled);
    void set_dup_trash(bool enabled);       /* confirm-gated */
    void commit_dup_trash(bool enabled);
    void reveal_path(const std::string& raw) const;
};

/* ui.cpp calls these to run confirm flow; defined in app.cpp as
 * modal (MessageBox) — logic returns true when user approved. */
bool ui_confirm(const std::string& text);

} // namespace fsui
#endif /* GPTBRIDGE_FSUI_STATE_H */

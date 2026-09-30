/* sync.cpp — reflect AppState onto the Win32 controls. */
#include "app.h"

#include <commctrl.h>

#include "parse.h"
#include "protocol.h"
#include "strings.h"
#include "utf8.h"

namespace fsui {

namespace P = proto;

namespace {

void show(HWND h, bool visible) {
    ShowWindow(h, visible ? SW_SHOW : SW_HIDE);
}

void enable(HWND h, bool on) { EnableWindow(h, on ? TRUE : FALSE); }

void set_check(HWND h, bool on) {
    SendMessageW(h, BM_SETCHECK, on ? BST_CHECKED : BST_UNCHECKED, 0);
}

/* Replace listbox rows only when contents changed (join compare). */
void sync_list(HWND h, const std::vector<std::string>& rows,
               std::string* cache) {
    std::string joined;
    for (const auto& r : rows) { joined += r; joined += '\x1f'; }
    if (joined == *cache) return;
    *cache = joined;
    SendMessageW(h, WM_SETREDRAW, FALSE, 0);
    SendMessageW(h, LB_RESETCONTENT, 0, 0);
    for (const auto& r : rows) {
        std::wstring w = widen(r);
        SendMessageW(h, LB_ADDSTRING, 0, (LPARAM)w.c_str());
    }
    SendMessageW(h, WM_SETREDRAW, TRUE, 0);
    InvalidateRect(h, nullptr, TRUE);
}

std::string g_files_cache, g_combo_cache;
std::string g_rules_cache;

} // namespace

void sync_ui() {
    App& a = g_app;
    AppState& s = a.st;
    Ui& u = a.ui;
    bool can = s.can_run();
    bool busy = s.busy();
    bool connected = s.connected();

    /* header */
    set_text(u.conn_text, connected ? tr::kConnected : tr::kConnecting);
    set_text(u.ws_path, s.target_dir.empty() ? tr::kNoWorkspace : s.target_dir);

    /* workspace */
    set_text(u.folder_status, s.folder_scan_status);
    set_text(u.ws_state, s.target_validated ? tr::kAutoInFlow : tr::kWaitWorkspace);
    set_check(u.chk_auto, s.auto_organize);
    set_text(u.auto_status, s.auto_organize_status);
    set_check(u.chk_dup, s.dup_trash);
    set_text(u.dup_status, s.dup_trash_status);
    enable(u.chk_auto, can);
    enable(u.chk_dup, can);

    /* keyword rules */
    enable(u.kw_edit, !busy);
    std::vector<std::string> folders = s.destination_folders;
    std::string joined;
    for (const auto& f : folders) { joined += f; joined += '\x1f'; }
    if (joined != g_combo_cache) {
        g_combo_cache = joined;
        SendMessageW(u.dest_combo, CB_RESETCONTENT, 0, 0);
        for (const auto& f : folders) {
            std::wstring w = widen(f);
            SendMessageW(u.dest_combo, CB_ADDSTRING, 0, (LPARAM)w.c_str());
        }
    }
    int sel = -1;
    for (size_t i = 0; i < folders.size(); ++i)
        if (folders[i] == s.keyword_folder) sel = (int)i;
    if ((int)SendMessageW(u.dest_combo, CB_GETCURSEL, 0, 0) != sel)
        SendMessageW(u.dest_combo, CB_SETCURSEL, sel, 0);
    if (s.keyword_folder.empty())
        set_text(u.dest_combo, tr::kDestEmpty);
    enable(u.dest_combo, can);
    enable(u.btn_scan, can);
    enable(u.rules_list, !busy);
    set_check(u.chk_autoscan, s.auto_scan_folders);
    enable(u.chk_autoscan, !busy);

    /* visible rules list: `[程式碼] kw → folder` rows */
    std::vector<std::string> rule_rows;
    for (const auto& r : s.keyword_rules) {
        std::string row = "[" +
            std::string(r.source == "folder" ? "資料夾" : "程式碼") +
            "]  " + r.keyword + "  →  " + r.folder;
        rule_rows.push_back(row);
    }
    if (rule_rows.empty()) rule_rows.push_back(tr::kRulesEmpty);
    sync_list(u.rules_list, rule_rows, &g_rules_cache);

    auto kws = fsp::parse_keywords(s.keyword_input);
    bool has_dest = fsp::is_direct_child_folder_name(s.keyword_folder) &&
                    sel >= 0;
    enable(u.btn_list_rules, can);
    enable(u.btn_upsert, can && !kws.empty() && has_dest);
    bool mod_ok = can && !fsp::trim(s.current_keyword).empty() &&
                  !fsp::trim(s.updated_keyword).empty() &&
                  (fsp::trim(s.keyword_folder).empty() || has_dest);
    enable(u.btn_modify, mod_ok);

    /* cleanup */
    bool cleanup_on = s.cleanup_image_issues || s.cleanup_similar_images ||
                      s.cleanup_video_issues || s.cleanup_similar_videos;
    bool scanning = s.cleanup_state == RunState::Running;
    enable(u.btn_cleanup, can && cleanup_on && !scanning);
    show(u.btn_stopscan, scanning);
    enable(u.btn_stopscan, scanning && !s.cleanup_stop_requested);
    if (s.has_cleanup_progress) {
        show(u.progress, true);
        double total = P::jnum(s.cleanup_progress, "folder_total");
        double cur = P::jnum(s.cleanup_progress, "folder_current");
        int pct = total > 0 ? (int)(cur / total * 100.0)
                  : s.cleanup_state == RunState::Success ? 100 : 0;
        SendMessageW(u.progress, PBM_SETPOS, pct > 100 ? 100 : pct, 0);
    } else {
        show(u.progress, false);
    }
    set_text(u.cleanup_msg, s.cleanup_message);
    char cand[64];
    std::snprintf(cand, sizeof(cand), tr::kCandidates, (int)s.cleanup_files.size());
    set_text(u.cand_label, s.cleanup_files.empty() ? "" : cand);
    std::vector<std::string> file_rows;
    for (const auto& f : s.cleanup_files) {
        std::string path = P::jstr(f, "path");
        file_rows.push_back(path + "    " + fsp::cleanup_file_summary(f));
    }
    sync_list(u.files_list, file_rows, &g_files_cache);
    show(u.files_list, !s.cleanup_files.empty());
    show(u.btn_reveal, !s.cleanup_files.empty());
    set_text(u.scanout_edit, s.cleanup_output);
    show(u.scanout_label, !s.cleanup_output.empty());
    show(u.scanout_edit, !s.cleanup_output.empty());
}

} // namespace fsui

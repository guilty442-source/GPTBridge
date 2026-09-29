/* app.h — shared app surface: control handles, fonts, globals. */
#ifndef GPTBRIDGE_FSUI_APP_H
#define GPTBRIDGE_FSUI_APP_H

#include <vector>
#include <windows.h>

#include "state.h"

namespace fsui {

/* Control ids. */
enum {
    IDC_TARGET_EDIT = 100, IDC_BTN_BROWSE,
    IDC_FOLDER_STATUS, IDC_WS_STATE,
    IDC_CHK_AUTO_ORGANIZE, IDC_AUTO_STATUS,
    IDC_CHK_DUP_TRASH, IDC_DUP_STATUS,
    IDC_BTN_PREVIEW, IDC_BTN_UNDO, IDC_BTN_HISTORY,
    IDC_SORT_MSG, IDC_OUTPUT_EDIT,
    IDC_PLAN_TITLE, IDC_PLAN_SUMMARY, IDC_PLAN_WARN, IDC_PLAN_LIST,
    IDC_PLAN_CONFIRM, IDC_BTN_APPLY,
    IDC_HISTORY_TITLE, IDC_HISTORY_LIST, IDC_HISTORY_EDIT,
    IDC_KW_EDIT, IDC_DEST_COMBO, IDC_BTN_SCAN, IDC_CHK_AUTOSCAN,
    IDC_BTN_LIST_RULES, IDC_BTN_UPSERT,
    IDC_KW_CUR, IDC_KW_NEW, IDC_BTN_MODIFY,
    IDC_CHK_IMG, IDC_CHK_SIMIMG, IDC_CHK_VID, IDC_CHK_SIMVID,
    IDC_TB_THRESHOLD, IDC_LBL_THRESHOLD,
    IDC_TB_SPEED, IDC_LBL_SPEED,
    IDC_TB_TEMP, IDC_LBL_TEMP,
    IDC_TB_TOPP, IDC_LBL_TOPP,
    IDC_CTX_EDIT, IDC_TOK_EDIT, IDC_CHK_PARALLEL,
    IDC_BTN_CLEANUP, IDC_BTN_STOPSCAN,
    IDC_PROGRESS, IDC_CLEANUP_MSG,
    IDC_CAND_LABEL, IDC_FILES_LIST, IDC_BTN_REVEAL,
    IDC_SCANOUT_LABEL, IDC_SCANOUT_EDIT,
    IDC_CONN_DOT, IDC_CONN_TEXT, IDC_WS_PATH,
};

struct Ui {
    HWND target_edit, btn_browse, folder_status, ws_state;
    HWND chk_auto, auto_status, chk_dup, dup_status;
    HWND btn_preview, btn_undo, btn_history;
    HWND sort_msg, output_edit;
    HWND plan_title, plan_summary, plan_warn, plan_list, plan_confirm, btn_apply;
    HWND hist_title, hist_list, hist_edit;
    HWND kw_edit, dest_combo, btn_scan, chk_autoscan;
    HWND btn_list_rules, btn_upsert;
    HWND kw_cur, kw_new, btn_modify;
    HWND chk_img, chk_simimg, chk_vid, chk_simvid;
    HWND tb_threshold, lbl_threshold, tb_speed, lbl_speed;
    HWND tb_temp, lbl_temp, tb_topp, lbl_topp;
    HWND ctx_edit, tok_edit, chk_parallel;
    HWND btn_cleanup, btn_stopscan;
    HWND progress, cleanup_msg;
    HWND cand_label, files_list, btn_reveal;
    HWND scanout_label, scanout_edit;
    HWND conn_dot, conn_text, ws_path;
};

struct App {
    AppState st;
    Ui ui;
    HWND hwnd = nullptr;
    HWND content = nullptr;
    HWND scroll = nullptr;
    int content_h = 0;
    int scroll_y = 0;
    HFONT font = nullptr;
    HFONT font_bold = nullptr;
    HFONT font_mono = nullptr;
    HFONT font_heading = nullptr;
    HFONT font_small = nullptr;
    bool editing_target = false;
    /* card rects painted by the content window's WM_PAINT */
    std::vector<RECT> cards;
};

extern App g_app;
extern HWND g_confirm_parent;

/* layout.cpp — build all controls inside the content child window. */
void build_layout(HWND content);

/* sync.cpp — reflect AppState onto the controls. */
void sync_ui();

/* app.cpp */
void on_socket_frame(const std::string& frame);
void on_backend_state(WsClient::State s);

constexpr UINT WM_APP_SOCKET = WM_APP + 1;
constexpr UINT_PTR TIMER_TICK = 1;
constexpr UINT TICK_MS = 200;
constexpr UINT RECONNECT_MS = 500;

} // namespace fsui
#endif /* GPTBRIDGE_FSUI_APP_H */

/* layout.cpp — build the control tree inside the scrolling content
 * child window.  Win32 common controls; all text is zh-TW UTF-8,
 * converted at creation; font = Microsoft JhengHei UI. */
#include "app.h"

#include <commctrl.h>

#include "strings.h"
#include "utf8.h"

namespace fsui {

namespace {

struct Lay {
    HWND parent;
    int y = 10;
    static constexpr int x = 14;
    static constexpr int w = 900; /* content width */

    HWND mk(const wchar_t* cls, int id, DWORD style, int cx, int cy,
            int cw, int ch) {
        HWND h = CreateWindowExW(0, cls, L"", style | WS_CHILD | WS_VISIBLE,
                                 cx, cy, cw, ch, parent, (HMENU)(INT_PTR)id,
                                 GetModuleHandleW(nullptr), nullptr);
        SendMessageW(h, WM_SETFONT, (WPARAM)g_app.font, TRUE);
        return h;
    }
    HWND label(const char* text, int cx, int cy, int cw, int ch = 20,
               HFONT font = nullptr) {
        HWND h = mk(L"STATIC", -1, SS_LEFTNOWORDWRAP, cx, cy, cw, ch);
        set_text(h, text);
        SendMessageW(h, WM_SETFONT, (WPARAM)(font ? font : g_app.font), TRUE);
        return h;
    }
    HWND heading(const char* text, int cy_extra = 6) {
        y += cy_extra;
        HWND h = mk(L"STATIC", -1, SS_LEFTNOWORDWRAP, x, y, w, 26);
        set_text(h, text);
        SendMessageW(h, WM_SETFONT, (WPARAM)g_app.font_heading, TRUE);
        y += 30;
        return h;
    }
    HWND hint(const char* text, int ch = 18) {
        HWND h = mk(L"STATIC", -1, SS_LEFT, x, y, w, ch);
        set_text(h, text);
        y += ch + 4;
        return h;
    }
    HWND button(int id, const char* text, int cx, int cw, int ch = 26) {
        HWND h = mk(L"BUTTON", id, BS_PUSHBUTTON | WS_TABSTOP, cx, y, cw, ch);
        set_text(h, text);
        return h;
    }
    HWND check(int id, const char* text, int cx, int cy, int cw) {
        HWND h = mk(L"BUTTON", id, BS_AUTOCHECKBOX | WS_TABSTOP, cx, cy, cw, 20);
        set_text(h, text);
        return h;
    }
    HWND edit(int id, int cx, int cy, int cw, int ch, DWORD extra = 0,
              const char* cue = nullptr) {
        HWND h = mk(L"EDIT", id,
                    ES_AUTOHSCROLL | WS_BORDER | WS_TABSTOP | extra,
                    cx, cy, cw, ch);
        if (cue) {
            std::wstring w = widen(cue);
            SendMessageW(h, EM_SETCUEBANNER, TRUE, (LPARAM)w.c_str());
        }
        return h;
    }
    HWND trackbar(int id, int cx, int cy, int cw, int lo, int hi, int pos) {
        HWND h = mk(TRACKBAR_CLASSW, id,
                    TBS_AUTOTICKS | WS_TABSTOP, cx, cy, cw, 28);
        SendMessageW(h, TBM_SETRANGE, TRUE, MAKELPARAM(lo, hi));
        SendMessageW(h, TBM_SETPOS, TRUE, pos);
        SendMessageW(h, TBM_SETTICFREQ, (hi - lo) / 10 + 1, 0);
        return h;
    }
    HWND listbox(int id, int cx, int cy, int cw, int ch) {
        return mk(L"LISTBOX", id,
                  LBS_NOTIFY | WS_VSCROLL | WS_BORDER | WS_TABSTOP,
                  cx, cy, cw, ch);
    }
    HWND group(const char* text, int cy, int ch) {
        HWND h = mk(L"BUTTON", -1, BS_GROUPBOX, x, cy, w, ch);
        set_text(h, text);
        return h;
    }
    HWND status(const char* text = "", int cw = -1) {
        HWND h = mk(L"STATIC", -1, SS_LEFTNOWORDWRAP, x, y, cw < 0 ? w : cw, 18);
        set_text(h, text);
        y += 20;
        return h;
    }
};

} // namespace

void build_layout(HWND content) {
    Lay L{content};
    Ui& u = g_app.ui;
    int y0;

    /* ---- header ---- */
    L.label(tr::kKicker, Lay::x, L.y, Lay::w, 14); L.y += 16;
    HWND t = L.label(tr::kTitle, Lay::x, L.y, 420, 30, g_app.font_heading);
    (void)t;
    u.conn_dot = L.label("\xE2\x97\x8F", 640, L.y + 4, 22, 22); /* ● */
    u.conn_text = L.label(tr::kConnecting, 664, L.y + 6, 150, 20);
    L.y += 36;
    L.label(tr::kWorkspaceLbl, Lay::x, L.y, 90, 18);
    u.ws_path = L.mk(L"STATIC", IDC_WS_PATH, SS_LEFTNOWORDWRAP, 106, L.y, 790, 18);
    SendMessageW(u.ws_path, WM_SETFONT, (WPARAM)g_app.font_mono, TRUE);
    L.y += 26;

    /* ---- 工作區與自動分類 ---- */
    y0 = L.y;
    L.y += 18;
    L.hint(tr::kWorkspaceHint);
    L.label(tr::kTargetLabel, Lay::x, L.y, 200, 16); L.y += 18;
    u.target_edit = L.edit(IDC_TARGET_EDIT, Lay::x, L.y, Lay::w, 24, 0,
                           tr::kTargetHint);
    L.y += 28;
    u.folder_status = L.mk(L"STATIC", IDC_FOLDER_STATUS, SS_LEFTNOWORDWRAP,
                           Lay::x, L.y, Lay::w, 18); L.y += 20;
    u.ws_state = L.mk(L"STATIC", IDC_WS_STATE, SS_LEFTNOWORDWRAP,
                      Lay::x, L.y, Lay::w, 18);
    set_text(u.ws_state, tr::kWaitWorkspace); L.y += 24;
    u.chk_auto = L.check(IDC_CHK_AUTO_ORGANIZE, tr::kAutoOrganize,
                         Lay::x, L.y, 260); L.y += 24;
    u.auto_status = L.mk(L"STATIC", IDC_AUTO_STATUS, SS_LEFTNOWORDWRAP,
                         Lay::x + 20, L.y, Lay::w - 20, 18); L.y += 22;
    u.chk_dup = L.check(IDC_CHK_DUP_TRASH, tr::kDupTrash, Lay::x, L.y, 380);
    L.y += 24;
    L.hint(tr::kDupTrashHint, 34);
    u.dup_status = L.mk(L"STATIC", IDC_DUP_STATUS, SS_LEFTNOWORDWRAP,
                        Lay::x + 20, L.y, Lay::w - 20, 18); L.y += 22;
    L.group(tr::kSecWorkspace, y0, L.y - y0 + 8);
    L.y += 16;

    /* ---- 安全整理工作流程 ---- */
    y0 = L.y;
    L.y += 18;
    L.hint(tr::kSortHint);
    u.btn_preview = L.button(IDC_BTN_PREVIEW, tr::kBtnPreview, Lay::x, 170);
    u.btn_undo = L.button(IDC_BTN_UNDO, tr::kBtnUndo, 194, 110);
    u.btn_history = L.button(IDC_BTN_HISTORY, tr::kBtnHistory, 312, 110);
    L.y += 32;
    u.sort_msg = L.mk(L"STATIC", IDC_SORT_MSG, SS_LEFTNOWORDWRAP,
                      Lay::x, L.y, Lay::w, 18);
    set_text(u.sort_msg, tr::kMsgReady); L.y += 22;
    u.output_edit = L.edit(IDC_OUTPUT_EDIT, Lay::x, L.y, Lay::w, 96,
                           ES_MULTILINE | ES_READONLY | WS_VSCROLL);
    SendMessageW(u.output_edit, WM_SETFONT, (WPARAM)g_app.font_mono, TRUE);
    L.y += 102;

    /* plan panel */
    u.plan_title = L.mk(L"STATIC", IDC_PLAN_TITLE, SS_LEFTNOWORDWRAP,
                        Lay::x, L.y, Lay::w, 20);
    SendMessageW(u.plan_title, WM_SETFONT, (WPARAM)g_app.font_bold, TRUE);
    L.y += 22;
    u.plan_summary = L.mk(L"STATIC", IDC_PLAN_SUMMARY, SS_LEFTNOWORDWRAP,
                          Lay::x, L.y, Lay::w, 18); L.y += 20;
    u.plan_warn = L.mk(L"STATIC", IDC_PLAN_WARN, SS_LEFTNOWORDWRAP,
                       Lay::x, L.y, Lay::w, 18); L.y += 20;
    u.plan_list = L.listbox(IDC_PLAN_LIST, Lay::x, L.y, Lay::w, 96);
    L.y += 102;
    u.plan_confirm = L.check(IDC_PLAN_CONFIRM, "", Lay::x, L.y, 640);
    L.y += 24;
    u.btn_apply = L.button(IDC_BTN_APPLY, tr::kBtnApply, Lay::x, 120);
    L.y += 34;

    /* history panel */
    u.hist_title = L.mk(L"STATIC", IDC_HISTORY_TITLE, SS_LEFTNOWORDWRAP,
                        Lay::x, L.y, Lay::w, 20);
    set_text(u.hist_title, tr::kHistoryTitle);
    SendMessageW(u.hist_title, WM_SETFONT, (WPARAM)g_app.font_bold, TRUE);
    L.y += 22;
    u.hist_list = L.listbox(IDC_HISTORY_LIST, Lay::x, L.y, Lay::w, 78);
    L.y += 84;
    u.hist_edit = L.edit(IDC_HISTORY_EDIT, Lay::x, L.y, Lay::w, 60,
                         ES_MULTILINE | ES_READONLY | WS_VSCROLL);
    SendMessageW(u.hist_edit, WM_SETFONT, (WPARAM)g_app.font_mono, TRUE);
    L.y += 66;
    L.group(tr::kSecSort, y0, L.y - y0 + 8);
    L.y += 16;

    /* ---- 關鍵字分類規則 ---- */
    y0 = L.y;
    L.y += 18;
    L.hint(tr::kKeywordHint);
    L.label(tr::kKeywordLbl, Lay::x, L.y, 200, 16); L.y += 18;
    u.kw_edit = L.edit(IDC_KW_EDIT, Lay::x, L.y, Lay::w, 40,
                       ES_MULTILINE | ES_WANTRETURN, tr::kKeywordHintTxt);
    L.y += 46;
    L.label(tr::kDestLabel, Lay::x, L.y, 400, 16); L.y += 18;
    u.dest_combo = L.mk(L"COMBOBOX", IDC_DEST_COMBO,
                        CBS_DROPDOWNLIST | WS_TABSTOP, Lay::x, L.y, 400, 200);
    u.btn_scan = L.button(IDC_BTN_SCAN, tr::kBtnScan, 424, 70);
    u.chk_autoscan = L.check(IDC_CHK_AUTOSCAN, tr::kAutoScan, 504, L.y, 220);
    SendMessageW(u.chk_autoscan, BM_SETCHECK, BST_CHECKED, 0);
    L.y += 30;
    u.btn_list_rules = L.button(IDC_BTN_LIST_RULES, tr::kBtnListRules,
                                Lay::x, 110);
    u.btn_upsert = L.button(IDC_BTN_UPSERT, tr::kBtnUpsert, 132, 150);
    L.y += 34;
    HWND m = L.mk(L"STATIC", -1, SS_LEFTNOWORDWRAP, Lay::x, L.y, 200, 18);
    set_text(m, tr::kModifyLbl);
    SendMessageW(m, WM_SETFONT, (WPARAM)g_app.font_bold, TRUE);
    L.y += 22;
    u.kw_cur = L.edit(IDC_KW_CUR, Lay::x, L.y, 180, 24, 0, tr::kCurKeyword);
    u.kw_new = L.edit(IDC_KW_NEW, 204, L.y, 180, 24, 0, tr::kNewKeyword);
    u.btn_modify = L.button(IDC_BTN_MODIFY, tr::kBtnModify, 394, 80);
    L.y += 32;
    L.group(tr::kSecKeywords, y0, L.y - y0 + 8);
    L.y += 16;

    /* ---- 清理掃描 ---- */
    y0 = L.y;
    L.y += 18;
    L.hint(tr::kCleanupHint);
    u.chk_img = L.check(IDC_CHK_IMG, tr::kImgIssues, Lay::x, L.y, 110);
    u.chk_simimg = L.check(IDC_CHK_SIMIMG, tr::kSimilarImgs, 130, L.y, 110);
    u.chk_vid = L.check(IDC_CHK_VID, tr::kVidIssues, 246, L.y, 110);
    u.chk_simvid = L.check(IDC_CHK_SIMVID, tr::kSimilarVids, 362, L.y, 110);
    SendMessageW(u.chk_vid, BM_SETCHECK, BST_CHECKED, 0);
    L.y += 28;
    L.label(tr::kThresholdLbl, Lay::x, L.y + 4, 70, 18);
    u.tb_threshold = L.trackbar(IDC_TB_THRESHOLD, 88, L.y, 200, 50, 100, 96);
    u.lbl_threshold = L.label("96", 294, L.y + 4, 40, 18);
    L.label(tr::kSpeedLbl, 344, L.y + 4, 70, 18);
    u.tb_speed = L.trackbar(IDC_TB_SPEED, 418, L.y, 200, 1, 100, 50);
    u.lbl_speed = L.label("50", 624, L.y + 4, 40, 18);
    L.y += 36;
    L.label(tr::kTempLbl, Lay::x, L.y + 4, 50, 18);
    u.tb_temp = L.trackbar(IDC_TB_TEMP, 68, L.y, 160, 0, 200, 0);
    u.lbl_temp = L.label("0.00", 234, L.y + 4, 50, 18);
    L.label(tr::kTopPLbl, 294, L.y + 4, 50, 18);
    u.tb_topp = L.trackbar(IDC_TB_TOPP, 348, L.y, 160, 0, 100, 90);
    u.lbl_topp = L.label("0.90", 514, L.y + 4, 50, 18);
    L.y += 36;
    L.label(tr::kCtxLbl, Lay::x, L.y + 4, 70, 18);
    u.ctx_edit = L.edit(IDC_CTX_EDIT, 88, L.y, 90, 22, ES_NUMBER);
    set_text(u.ctx_edit, "8192");
    L.label(tr::kMaxTokLbl, 194, L.y + 4, 70, 18);
    u.tok_edit = L.edit(IDC_TOK_EDIT, 268, L.y, 90, 22, ES_NUMBER);
    set_text(u.tok_edit, "512");
    u.chk_parallel = L.check(IDC_CHK_PARALLEL, tr::kParallel, 374, L.y, 110);
    SendMessageW(u.chk_parallel, BM_SETCHECK, BST_CHECKED, 0);
    L.y += 32;
    u.btn_cleanup = L.button(IDC_BTN_CLEANUP, tr::kBtnCleanup, Lay::x, 130);
    u.btn_stopscan = L.button(IDC_BTN_STOPSCAN, tr::kBtnStopScan, 152, 120);
    L.y += 34;
    u.progress = L.mk(PROGRESS_CLASSW, IDC_PROGRESS, PBS_SMOOTH,
                      Lay::x, L.y, Lay::w, 20);
    L.y += 26;
    u.cleanup_msg = L.mk(L"STATIC", IDC_CLEANUP_MSG, SS_LEFTNOWORDWRAP,
                         Lay::x, L.y, Lay::w, 18);
    set_text(u.cleanup_msg, tr::kMsgCleanupReady); L.y += 24;
    u.cand_label = L.mk(L"STATIC", IDC_CAND_LABEL, SS_LEFTNOWORDWRAP,
                        Lay::x, L.y, Lay::w, 20);
    SendMessageW(u.cand_label, WM_SETFONT, (WPARAM)g_app.font_bold, TRUE);
    L.y += 22;
    u.files_list = L.listbox(IDC_FILES_LIST, Lay::x, L.y, Lay::w - 110, 110);
    u.btn_reveal = L.button(IDC_BTN_REVEAL, tr::kBtnReveal, Lay::w - 90, 96);
    L.y += 116;
    u.scanout_label = L.mk(L"STATIC", IDC_SCANOUT_LABEL, SS_LEFTNOWORDWRAP,
                           Lay::x, L.y, Lay::w, 18);
    set_text(u.scanout_label, tr::kScanOutput); L.y += 20;
    u.scanout_edit = L.edit(IDC_SCANOUT_EDIT, Lay::x, L.y, Lay::w, 80,
                            ES_MULTILINE | ES_READONLY | WS_VSCROLL);
    SendMessageW(u.scanout_edit, WM_SETFONT, (WPARAM)g_app.font_mono, TRUE);
    L.y += 86;
    L.group(tr::kSecCleanup, y0, L.y - y0 + 8);
    L.y += 12;

    g_app.content_h = L.y;
}

} // namespace fsui

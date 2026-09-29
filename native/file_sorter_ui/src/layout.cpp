/* layout.cpp ??modern card-based layout inside the scrolling content
 * child window.  Cards are painted by the content WM_PAINT; controls
 * are owner-drawn (buttons/checks) or themed widgets (slider/progress/
 * scroll).  All text zh-TW UTF-8; font = Microsoft JhengHei UI. */
#include "app.h"

#include <commctrl.h>

#include "strings.h"
#include "theme.h"
#include "utf8.h"
#include "widgets.h"

namespace fsui {

namespace {

struct Lay {
    HWND parent;
    int y = 14;
    static constexpr int x = 16;
    static constexpr int w = 896;
    bool on_card = false;

    HWND mk(const wchar_t* cls, int id, DWORD style, int cx, int cy,
            int cw, int ch, HFONT font = nullptr) {
        HWND h = CreateWindowExW(0, cls, L"", style | WS_CHILD | WS_VISIBLE,
                                 cx, cy, cw, ch, parent, (HMENU)(INT_PTR)id,
                                 GetModuleHandleW(nullptr), nullptr);
        SendMessageW(h, WM_SETFONT,
                     (WPARAM)(font ? font : g_app.font), TRUE);
        return h;
    }
    HWND label(const char* text, int cx, int cy, int cw, int ch = 20,
               HFONT font = nullptr, bool muted = false) {
        HWND h = mk(L"STATIC", -1, SS_LEFTNOWORDWRAP, cx, cy, cw, ch, font);
        set_text(h, text);
        if (muted) theme::mark_muted(h);
        if (on_card) SetPropW(h, theme::kPropOnCard, (HANDLE)1);
        return h;
    }
    HWND heading(const char* text) {
        HWND h = label(text, x + 30, y, w - 60, 22, g_app.font_bold);
        (void)h;
        y += 30;
        return h;
    }
    HWND hint(const char* text, int ch = 18) {
        HWND h = mk(L"STATIC", -1, SS_LEFT, x + 18, y, w - 36, ch,
                    g_app.font_small);
        set_text(h, text);
        theme::mark_muted(h);
        if (on_card) SetPropW(h, theme::kPropOnCard, (HANDLE)1);
        y += ch + 4;
        return h;
    }
    HWND button(int id, const char* text, int cx, int cw, bool accent = false,
                int ch = 30) {
        HWND h = mk(L"BUTTON", id, BS_OWNERDRAW | WS_TABSTOP, cx, y, cw, ch);
        set_text(h, text);
        if (accent) theme::mark_accent(h);
        widgets::install_hover(h);
        return h;
    }
    HWND check(int id, const char* text, int cx, int cy, int cw) {
        HWND h = mk(L"BUTTON", id,
                    BS_AUTOCHECKBOX | BS_OWNERDRAW | WS_TABSTOP,
                    cx, cy, cw, 22);
        set_text(h, text);
        SetPropW(h, L"fsui.check", (HANDLE)1);
        widgets::install_hover(h);
        return h;
    }
    HWND stat_(int id, const char* text, int cx, int cw, int ch = 18,
               bool muted = true, HFONT font = nullptr) {
        HWND h = mk(L"STATIC", id, SS_LEFTNOWORDWRAP, cx, y, cw, ch,
                    font ? font : g_app.font_small);
        set_text(h, text);
        if (muted) theme::mark_muted(h);
        SetPropW(h, theme::kPropOnCard, (HANDLE)1);
        y += ch + 4;
        return h;
    }
    HWND edit(int id, int cx, int cy, int cw, int ch, DWORD extra = 0,
              const char* cue = nullptr) {
        HWND h = mk(L"EDIT", id, ES_AUTOHSCROLL | WS_TABSTOP | extra,
                    cx, cy, cw, ch);
        theme::dark_chrome(h);
        if (cue) {
            std::wstring w = widen(cue);
            SendMessageW(h, EM_SETCUEBANNER, TRUE, (LPARAM)w.c_str());
        }
        return h;
    }
    HWND listbox(int id, int cx, int cy, int cw, int ch) {
        HWND h = mk(L"LISTBOX", id,
                    LBS_NOTIFY | LBS_OWNERDRAWFIXED | LBS_HASSTRINGS |
                        WS_VSCROLL | WS_TABSTOP,
                    cx, cy, cw, ch);
        theme::dark_chrome(h);
        return h;
    }
    /* card: begin at current y; close with end_card() */
    int begin_card(const char* title) {
        int y0 = y;
        on_card = true;
        y += 10;
        heading(title);
        return y0;
    }
    void end_card(int y0) {
        int y1 = y + 6;
        g_app.cards.push_back(RECT{x - 4, y0, x + w + 4, y1});
        on_card = false;
        y = y1 + 14;
    }
};

} // namespace

void build_layout(HWND content) {
    Lay L{content};
    Ui& u = g_app.ui;
    const int ix = Lay::x + 18;        /* inner x inside cards */
    const int iw = Lay::w - 36;        /* inner width */
    int y0;

    /* ---- header (on bg, no card) ---- */
    HWND kicker = L.label(tr::kKicker, Lay::x + 2, L.y, 400, 14,
                          g_app.font_mono);
    theme::mark_cyan(kicker);
    L.y += 18;
    L.label(tr::kTitle, Lay::x + 2, L.y, 460, 32, g_app.font_heading);
    u.conn_dot = L.label("\xE2\x97\x8F", 720, L.y + 6, 20, 22); /* ??*/
    u.conn_text = L.label(tr::kConnecting, 744, L.y + 8, 150, 20);
    L.y += 42;
    L.label(tr::kWorkspaceLbl, Lay::x + 2, L.y, 90, 18, g_app.font_small, true);
    u.ws_path = L.mk(L"STATIC", IDC_WS_PATH, SS_LEFTNOWORDWRAP, 110, L.y,
                     700, 18, g_app.font_mono);
    L.y += 30;

    /* ---- 工�??�?�自?��?�?---- */
    y0 = L.begin_card(tr::kSecWorkspace);
    L.hint(tr::kWorkspaceHint);
    L.label(tr::kTargetLabel, ix, L.y, 200, 16, g_app.font_small);
    L.y += 18;
    u.target_edit = L.edit(IDC_TARGET_EDIT, ix, L.y, iw, 28, 0,
                           tr::kTargetHint);
    SetPropW(u.target_edit, theme::kPropOnCard, (HANDLE)1);
    L.y += 32;
    u.folder_status = L.stat_(IDC_FOLDER_STATUS, "", ix, iw);
    u.ws_state = L.stat_(IDC_WS_STATE, tr::kWaitWorkspace, ix, iw);
    u.chk_auto = L.check(IDC_CHK_AUTO_ORGANIZE, tr::kAutoOrganize, ix, L.y, 280);
    L.y += 26;
    u.auto_status = L.stat_(IDC_AUTO_STATUS, "", ix + 22, iw - 22);
    u.chk_dup = L.check(IDC_CHK_DUP_TRASH, tr::kDupTrash, ix, L.y, 400);
    L.y += 26;
    L.hint(tr::kDupTrashHint, 30);
    u.dup_status = L.stat_(IDC_DUP_STATUS, "", ix + 22, iw - 22);
    L.end_card(y0);

    /* ---- 安全?��?工�?流�? ---- */
    y0 = L.begin_card(tr::kSecSort);
    L.hint(tr::kSortHint);
    u.btn_preview = L.button(IDC_BTN_PREVIEW, tr::kBtnPreview, ix, 170, true);
    u.btn_undo = L.button(IDC_BTN_UNDO, tr::kBtnUndo, ix + 182, 110);
    u.btn_history = L.button(IDC_BTN_HISTORY, tr::kBtnHistory, ix + 304, 110);
    L.y += 38;
    u.sort_msg = L.stat_(IDC_SORT_MSG, tr::kMsgReady, ix, iw);
    u.output_edit = L.edit(IDC_OUTPUT_EDIT, ix, L.y, iw, 96,
                           ES_MULTILINE | ES_READONLY | WS_VSCROLL,
                           nullptr);
    SendMessageW(u.output_edit, WM_SETFONT, (WPARAM)g_app.font_mono, TRUE);
    L.y += 102;

    u.plan_title = L.mk(L"STATIC", IDC_PLAN_TITLE, SS_LEFTNOWORDWRAP,
                        ix, L.y, iw, 20, g_app.font_bold);
    SetPropW(u.plan_title, theme::kPropOnCard, (HANDLE)1);
    L.y += 22;
    u.plan_summary = L.stat_(IDC_PLAN_SUMMARY, "", ix, iw);
    u.plan_warn = L.stat_(IDC_PLAN_WARN, "", ix, iw);
    u.plan_list = L.listbox(IDC_PLAN_LIST, ix, L.y, iw, 96);
    SetPropW(u.plan_list, theme::kPropOnCard, (HANDLE)1);
    L.y += 102;
    u.plan_confirm = L.check(IDC_PLAN_CONFIRM, "", ix, L.y, 660);
    SetPropW(u.plan_confirm, theme::kPropOnCard, (HANDLE)1);
    L.y += 26;
    u.btn_apply = L.button(IDC_BTN_APPLY, tr::kBtnApply, ix, 120, true);
    L.y += 38;

    u.hist_title = L.mk(L"STATIC", IDC_HISTORY_TITLE, SS_LEFTNOWORDWRAP,
                        ix, L.y, iw, 20, g_app.font_bold);
    set_text(u.hist_title, tr::kHistoryTitle);
    SetPropW(u.hist_title, theme::kPropOnCard, (HANDLE)1);
    L.y += 22;
    u.hist_list = L.listbox(IDC_HISTORY_LIST, ix, L.y, iw, 78);
    SetPropW(u.hist_list, theme::kPropOnCard, (HANDLE)1);
    L.y += 84;
    u.hist_edit = L.edit(IDC_HISTORY_EDIT, ix, L.y, iw, 60,
                         ES_MULTILINE | ES_READONLY | WS_VSCROLL);
    SendMessageW(u.hist_edit, WM_SETFONT, (WPARAM)g_app.font_mono, TRUE);
    L.y += 66;
    L.end_card(y0);

    /* ---- ?�鍵字�?類�???---- */
    y0 = L.begin_card(tr::kSecKeywords);
    L.hint(tr::kKeywordHint);
    L.label(tr::kKeywordLbl, ix, L.y, 200, 16, g_app.font_small);
    L.y += 18;
    u.kw_edit = L.edit(IDC_KW_EDIT, ix, L.y, iw, 42,
                       ES_MULTILINE | ES_WANTRETURN, tr::kKeywordHintTxt);
    SetPropW(u.kw_edit, theme::kPropOnCard, (HANDLE)1);
    L.y += 48;
    L.label(tr::kDestLabel, ix, L.y, 420, 16, g_app.font_small);
    L.y += 18;
    u.dest_combo = L.mk(L"COMBOBOX", IDC_DEST_COMBO,
                        CBS_DROPDOWNLIST | CBS_OWNERDRAWFIXED |
                            CBS_HASSTRINGS | WS_TABSTOP,
                        ix, L.y, 420, 200);
    theme::dark_chrome(u.dest_combo);
    u.btn_scan = L.button(IDC_BTN_SCAN, tr::kBtnScan, ix + 432, 76);
    u.chk_autoscan = L.check(IDC_CHK_AUTOSCAN, tr::kAutoScan, ix + 520,
                             L.y + 4, 220);
    SetPropW(u.chk_autoscan, theme::kPropOnCard, (HANDLE)1);
    L.y += 36;
    u.btn_list_rules = L.button(IDC_BTN_LIST_RULES, tr::kBtnListRules, ix, 110);
    u.btn_upsert = L.button(IDC_BTN_UPSERT, tr::kBtnUpsert, ix + 122, 150);
    L.y += 40;
    L.label(tr::kModifyLbl, ix, L.y, 200, 18, g_app.font_bold);
    L.y += 22;
    u.kw_cur = L.edit(IDC_KW_CUR, ix, L.y, 190, 26, 0, tr::kCurKeyword);
    u.kw_new = L.edit(IDC_KW_NEW, ix + 200, L.y, 190, 26, 0, tr::kNewKeyword);
    u.btn_modify = L.button(IDC_BTN_MODIFY, tr::kBtnModify, ix + 400, 80);
    L.y += 34;
    L.end_card(y0);

    /* ---- 清�??��? ---- */
    y0 = L.begin_card(tr::kSecCleanup);
    L.hint(tr::kCleanupHint);
    u.chk_img = L.check(IDC_CHK_IMG, tr::kImgIssues, ix, L.y, 110);
    u.chk_simimg = L.check(IDC_CHK_SIMIMG, tr::kSimilarImgs, ix + 120, L.y, 110);
    u.chk_vid = L.check(IDC_CHK_VID, tr::kVidIssues, ix + 240, L.y, 110);
    u.chk_simvid = L.check(IDC_CHK_SIMVID, tr::kSimilarVids, ix + 360, L.y, 110);
    SendMessageW(u.chk_vid, BM_SETCHECK, BST_CHECKED, 0);
    L.y += 30;
    L.label(tr::kThresholdLbl, ix, L.y + 6, 70, 18, g_app.font_small);
    u.tb_threshold = widgets::create_slider(content, IDC_TB_THRESHOLD,
                                            ix + 74, L.y, 210, 50, 100, 96);
    SendMessageW(u.tb_threshold, WM_SETFONT, (WPARAM)g_app.font, TRUE);
    u.lbl_threshold = L.label("96", ix + 292, L.y + 6, 40, 18,
                              g_app.font_small);
    L.label(tr::kSpeedLbl, ix + 340, L.y + 6, 70, 18, g_app.font_small);
    u.tb_speed = widgets::create_slider(content, IDC_TB_SPEED,
                                        ix + 414, L.y, 210, 1, 100, 50);
    u.lbl_speed = L.label("50", ix + 632, L.y + 6, 40, 18, g_app.font_small);
    L.y += 38;
    L.label(tr::kTempLbl, ix, L.y + 6, 50, 18, g_app.font_small);
    u.tb_temp = widgets::create_slider(content, IDC_TB_TEMP,
                                       ix + 54, L.y, 170, 0, 200, 0);
    u.lbl_temp = L.label("0.00", ix + 232, L.y + 6, 50, 18, g_app.font_small);
    L.label(tr::kTopPLbl, ix + 290, L.y + 6, 50, 18, g_app.font_small);
    u.tb_topp = widgets::create_slider(content, IDC_TB_TOPP,
                                       ix + 344, L.y, 170, 0, 100, 90);
    u.lbl_topp = L.label("0.90", ix + 522, L.y + 6, 50, 18, g_app.font_small);
    L.y += 38;
    L.label(tr::kCtxLbl, ix, L.y + 4, 70, 18, g_app.font_small);
    u.ctx_edit = L.edit(IDC_CTX_EDIT, ix + 74, L.y, 90, 24, ES_NUMBER);
    set_text(u.ctx_edit, "8192");
    SetPropW(u.ctx_edit, theme::kPropOnCard, (HANDLE)1);
    L.label(tr::kMaxTokLbl, ix + 180, L.y + 4, 70, 18, g_app.font_small);
    u.tok_edit = L.edit(IDC_TOK_EDIT, ix + 254, L.y, 90, 24, ES_NUMBER);
    set_text(u.tok_edit, "512");
    SetPropW(u.tok_edit, theme::kPropOnCard, (HANDLE)1);
    u.chk_parallel = L.check(IDC_CHK_PARALLEL, tr::kParallel, ix + 360, L.y, 110);
    SendMessageW(u.chk_parallel, BM_SETCHECK, BST_CHECKED, 0);
    SetPropW(u.chk_parallel, theme::kPropOnCard, (HANDLE)1);
    L.y += 34;
    u.btn_cleanup = L.button(IDC_BTN_CLEANUP, tr::kBtnCleanup, ix, 130, true);
    u.btn_stopscan = L.button(IDC_BTN_STOPSCAN, tr::kBtnStopScan,
                              ix + 142, 120);
    L.y += 40;
    u.progress = widgets::create_progress(content, IDC_PROGRESS,
                                          ix, L.y, iw, 16);
    SendMessageW(u.progress, WM_SETFONT, (WPARAM)g_app.font, TRUE);
    L.y += 24;
    u.cleanup_msg = L.stat_(IDC_CLEANUP_MSG, tr::kMsgCleanupReady, ix, iw);
    u.cand_label = L.mk(L"STATIC", IDC_CAND_LABEL, SS_LEFTNOWORDWRAP,
                        ix, L.y, iw, 20, g_app.font_bold);
    SetPropW(u.cand_label, theme::kPropOnCard, (HANDLE)1);
    L.y += 22;
    u.files_list = L.listbox(IDC_FILES_LIST, ix, L.y, iw - 110, 110);
    SetPropW(u.files_list, theme::kPropOnCard, (HANDLE)1);
    u.btn_reveal = L.button(IDC_BTN_REVEAL, tr::kBtnReveal, ix + iw - 98, 96);
    L.y += 118;
    u.scanout_label = L.stat_(IDC_SCANOUT_LABEL, tr::kScanOutput, ix, iw);
    u.scanout_edit = L.edit(IDC_SCANOUT_EDIT, ix, L.y, iw, 80,
                            ES_MULTILINE | ES_READONLY | WS_VSCROLL);
    SendMessageW(u.scanout_edit, WM_SETFONT, (WPARAM)g_app.font_mono, TRUE);
    L.y += 86;
    L.end_card(y0);

    g_app.content_h = L.y;
}

} // namespace fsui

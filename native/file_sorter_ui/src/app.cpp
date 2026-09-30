/* app.cpp — file-sorter native tool window (Win32 + WinSock2).
 *
 * Launch contract (same as the egui surface): spawned with
 *   --tool-window --tool-id=file-sorter
 * and the GPTBRIDGE_SOURCE_UI_* environment block; speaks
 * toolbox_run_tool JSON frames over the loopback backend WS. */
#include "app.h"

#include <commctrl.h>
#include <dwmapi.h>
#include <shellapi.h>
#include <shobjidl.h>

#include <cstdio>
#include <cstdlib>
#include <string>

#include "parse.h"
#include "protocol.h"
#include "strings.h"
#include "theme.h"
#include "utf8.h"
#include "widgets.h"

#pragma comment(lib, "dwmapi.lib")

#pragma comment(linker, \
    "\"/manifestdependency:type='win32' "\
    "name='Microsoft.Windows.Common-Controls' version='6.0.0.0' "\
    "processorArchitecture='*' publicKeyToken='6595b64144ccf1df' "\
    "language='*'\"")

namespace fsui {

namespace P = proto;

App g_app;
HWND g_confirm_parent = nullptr;
int g_scan_phase = 0;
bool g_blink = false;

namespace {

constexpr wchar_t kMainClass[] = L"GPTBridgeFileSorterUI";
constexpr wchar_t kContentClass[] = L"GPTBridgeFileSorterContent";

std::string env(const char* key) {
    char buf[2048];
    DWORD n = GetEnvironmentVariableA(key, buf, sizeof(buf));
    return n > 0 && n < sizeof(buf) ? std::string(buf, n) : "";
}

HWND find_ctrl(int id) { return GetDlgItem(g_app.content, id); }

constexpr unsigned kShortTimeoutG() { return 20; }
constexpr unsigned kRunTimeoutG() { return 30 * 60; }

void browse_target_folder() {
    AppState& s = g_app.st;
    IFileOpenDialog* dlg = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_FileOpenDialog, nullptr,
                                  CLSCTX_ALL, IID_PPV_ARGS(&dlg));
    if (FAILED(hr) || !dlg) return;
    DWORD opts = 0;
    dlg->GetOptions(&opts);
    dlg->SetOptions(opts | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM |
                          FOS_DONTADDTORECENT);
    dlg->SetTitle(widen(tr::kBrowseTitle).c_str());
    if (SUCCEEDED(dlg->Show(g_app.hwnd))) {
        IShellItem* item = nullptr;
        if (SUCCEEDED(dlg->GetResult(&item)) && item) {
            PWSTR path = nullptr;
            if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &path)) &&
                path) {
                s.target_dir = narrow(path);
                set_text(g_app.ui.target_edit, s.target_dir);
                s.validate_target();
                CoTaskMemFree(path);
            }
            item->Release();
        }
    }
    dlg->Release();
}

/* repaint the accent underline zone under an edit on focus change */
void invalidate_focus_line(int id) {
    HWND c = find_ctrl(id);
    if (!c) return;
    RECT r;
    GetWindowRect(c, &r);
    MapWindowPoints(nullptr, g_app.content, (POINT*)&r, 2);
    r.bottom += 4;
    InvalidateRect(g_app.content, &r, FALSE);
}

void handle_command(int id, int code) {
    AppState& s = g_app.st;
    Ui& u = g_app.ui;
    if (code == EN_SETFOCUS || code == EN_KILLFOCUS)
        invalidate_focus_line(id);
    switch (id) {
        case IDC_TARGET_EDIT:
            if (code == EN_KILLFOCUS) {
                s.target_dir = ctrl_text(u.target_edit);
                s.validate_target();
            }
            return;
        case IDC_KW_EDIT:
            if (code == EN_CHANGE) s.keyword_input = ctrl_text(u.kw_edit);
            return;
        case IDC_KW_CUR:
            if (code == EN_CHANGE) s.current_keyword = ctrl_text(u.kw_cur);
            return;
        case IDC_KW_NEW:
            if (code == EN_CHANGE) s.updated_keyword = ctrl_text(u.kw_new);
            return;
        case IDC_CTX_EDIT:
            if (code == EN_CHANGE) {
                s.model_context_window =
                    (unsigned)strtoul(ctrl_text(u.ctx_edit).c_str(), nullptr, 10);
            }
            return;
        case IDC_TOK_EDIT:
            if (code == EN_CHANGE) {
                s.model_max_tokens =
                    (unsigned)strtoul(ctrl_text(u.tok_edit).c_str(), nullptr, 10);
            }
            return;
        case IDC_DEST_COMBO:
            if (code == CBN_SELCHANGE) {
                int sel = (int)SendMessageW(u.dest_combo, CB_GETCURSEL, 0, 0);
                if (sel >= 0 && sel < (int)s.destination_folders.size())
                    s.keyword_folder = s.destination_folders[sel];
            }
            return;
        case IDC_RULES_LIST:
            if (code == LBN_SELCHANGE) {
                int sel = (int)SendMessageW(u.rules_list, LB_GETCURSEL, 0, 0);
                if (sel >= 0 && sel < (int)s.keyword_rules.size()) {
                    const auto& rule = s.keyword_rules[sel];
                    s.current_keyword = rule.keyword;
                    set_text(u.kw_cur, rule.keyword);
                    if (!rule.folder.empty())
                        s.keyword_folder = rule.folder;
                    sync_ui();
                }
            }
            return;
        default: break;
    }
    if (code != BN_CLICKED) return;
    std::string target = fsp::trim(s.target_dir);
    switch (id) {
        case IDC_CHK_AUTO_ORGANIZE:
            s.set_profile_enabled(
                SendMessageW(u.chk_auto, BM_GETCHECK, 0, 0) == BST_CHECKED);
            break;
        case IDC_CHK_DUP_TRASH:
            s.set_dup_trash(
                SendMessageW(u.chk_dup, BM_GETCHECK, 0, 0) == BST_CHECKED);
            break;
        case IDC_BTN_BROWSE:
            browse_target_folder();
            break;
        case IDC_BTN_SCAN:
            s.enqueue_folder_scan();
            break;
        case IDC_CHK_AUTOSCAN:
            s.auto_scan_folders =
                SendMessageW(u.chk_autoscan, BM_GETCHECK, 0, 0) == BST_CHECKED;
            if (s.auto_scan_folders && s.target_validated)
                s.enqueue_folder_scan();
            break;
        case IDC_BTN_LIST_RULES:
            s.enqueue(RunKind::ListKeywords, {target, "--list-keywords"},
                      kShortTimeoutG(), tr::kMsgListKw, tr::kMsgListKwDone);
            break;
        case IDC_BTN_UPSERT: {
            std::vector<std::string> args{target};
            for (const auto& kw : fsp::parse_keywords(s.keyword_input)) {
                args.push_back("--upsert-keyword");
                args.push_back(kw);
            }
            args.push_back("--folder");
            args.push_back(fsp::trim(s.keyword_folder));
            s.enqueue(RunKind::MutateKeywords, args, kRunTimeoutG(),
                      tr::kMsgUpsertKw, tr::kMsgUpsertDone);
            break;
        }
        case IDC_BTN_MODIFY: {
            std::vector<std::string> args{
                target, "--update-keyword", fsp::trim(s.current_keyword),
                "--new-keyword", fsp::trim(s.updated_keyword)};
            if (!fsp::trim(s.keyword_folder).empty()) {
                args.push_back("--folder");
                args.push_back(fsp::trim(s.keyword_folder));
            }
            s.enqueue(RunKind::MutateKeywords, args, kRunTimeoutG(),
                      tr::kMsgModKw, tr::kMsgModKwDone);
            break;
        }
        case IDC_CHK_IMG:
            s.cleanup_image_issues =
                SendMessageW(u.chk_img, BM_GETCHECK, 0, 0) == BST_CHECKED;
            break;
        case IDC_CHK_SIMIMG:
            s.cleanup_similar_images =
                SendMessageW(u.chk_simimg, BM_GETCHECK, 0, 0) == BST_CHECKED;
            break;
        case IDC_CHK_VID:
            s.cleanup_video_issues =
                SendMessageW(u.chk_vid, BM_GETCHECK, 0, 0) == BST_CHECKED;
            break;
        case IDC_CHK_SIMVID:
            s.cleanup_similar_videos =
                SendMessageW(u.chk_simvid, BM_GETCHECK, 0, 0) == BST_CHECKED;
            break;
        case IDC_CHK_PARALLEL:
            s.cleanup_parallel =
                SendMessageW(u.chk_parallel, BM_GETCHECK, 0, 0) == BST_CHECKED;
            break;
        case IDC_BTN_CLEANUP: s.start_cleanup(); break;
        case IDC_BTN_STOPSCAN: s.request_stop_cleanup(); break;
        case IDC_BTN_REVEAL: {
            int sel = (int)SendMessageW(u.files_list, LB_GETCURSEL, 0, 0);
            if (sel >= 0 && sel < (int)s.cleanup_files.size())
                s.reveal_path(P::jstr(s.cleanup_files[sel], "path"));
            break;
        }
        default: return;
    }
    sync_ui();
    s.pump_queue();
}

void handle_hscroll(HWND bar) {
    AppState& s = g_app.st;
    Ui& u = g_app.ui;
    int pos = (int)SendMessageW(bar, TBM_GETPOS, 0, 0);
    if (bar == u.tb_threshold) {
        s.cleanup_threshold = pos;
        set_text(u.lbl_threshold, std::to_string(pos));
    } else if (bar == u.tb_speed) {
        s.cleanup_speed = pos;
        set_text(u.lbl_speed, std::to_string(pos));
    } else if (bar == u.tb_temp) {
        s.model_temperature = pos / 100.0;
        char b[16];
        std::snprintf(b, sizeof(b), "%.2f", s.model_temperature);
        set_text(u.lbl_temp, b);
    } else if (bar == u.tb_topp) {
        s.model_top_p = pos / 100.0;
        char b[16];
        std::snprintf(b, sizeof(b), "%.2f", s.model_top_p);
        set_text(u.lbl_topp, b);
    }
    sync_ui();
}

void update_scrollbar() {
    RECT rc;
    GetClientRect(g_app.hwnd, &rc);
    int max_pos = (int)(g_app.content_h - rc.bottom);
    if (max_pos < 0) max_pos = 0;
    if (g_app.scroll_y > max_pos) g_app.scroll_y = max_pos;
    SetWindowPos(g_app.content, nullptr, 0, -g_app.scroll_y,
                 rc.right, g_app.content_h, SWP_NOZORDER);
    if (g_app.scroll) {
        SetWindowPos(g_app.scroll, nullptr, rc.right - 10, 0, 10, rc.bottom,
                     SWP_NOZORDER | SWP_SHOWWINDOW);
        widgets::scroll_set(g_app.scroll, g_app.content_h, rc.bottom);
        widgets::scroll_set_pos(g_app.scroll, g_app.scroll_y);
    }
}

void scroll_content(int delta) {
    g_app.scroll_y += delta;
    RECT rc;
    GetClientRect(g_app.hwnd, &rc);
    int max_pos = (int)(g_app.content_h - rc.bottom);
    if (max_pos < 0) max_pos = 0;
    if (g_app.scroll_y < 0) g_app.scroll_y = 0;
    if (g_app.scroll_y > max_pos) g_app.scroll_y = max_pos;
    SetWindowPos(g_app.content, nullptr, 0, -g_app.scroll_y, rc.right,
                 g_app.content_h, SWP_NOZORDER);
    if (g_app.scroll) widgets::scroll_set_pos(g_app.scroll, g_app.scroll_y);
}

/* ---------- owner-draw painters ---------- */

void draw_button(const DRAWITEMSTRUCT* dis) {
    bool accent = theme::is_accent(dis->hwndItem);
    bool hot = GetPropW(dis->hwndItem, theme::kPropHot) != nullptr;
    bool down = (dis->itemState & ODS_SELECTED) != 0;
    bool disabled = (dis->itemState & ODS_DISABLED) != 0;
    COLORREF fill = accent ? (down ? theme::kAccentDn
                                  : hot ? theme::kAccentHot : theme::kAccent)
                           : (down ? theme::kCardEdge
                                   : hot ? theme::kSecHot
                                         : theme::kSecondary);
    if (disabled) fill = theme::kDisabled;
    theme::fill_chamfer(dis->hDC, dis->rcItem, 8, fill,
                        accent ? theme::kAccentDn
                               : hot ? theme::kAccent : theme::kFieldEdge);
    /* neon slash across the cut corner — reticle detail */
    if (accent && !disabled) {
        HPEN pen = CreatePen(PS_SOLID, 1, theme::kOnAccent);
        HGDIOBJ op = SelectObject(dis->hDC, pen);
        MoveToEx(dis->hDC, dis->rcItem.left + 1, dis->rcItem.top + 12, nullptr);
        LineTo(dis->hDC, dis->rcItem.left + 12, dis->rcItem.top + 1);
        SelectObject(dis->hDC, op);
        DeleteObject(pen);
    }
    wchar_t buf[128];
    GetWindowTextW(dis->hwndItem, buf, 128);
    RECT tr = dis->rcItem;
    COLORREF fg = disabled ? theme::kMuted
                  : accent ? theme::kOnAccent
                  : hot    ? theme::kAccentHot
                           : theme::kText;
    theme::text(dis->hDC, narrow(buf), tr, fg, g_app.font,
                DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS);
    if (dis->itemState & ODS_FOCUS) {
        RECT fr = dis->rcItem;
        InflateRect(&fr, -3, -3);
        DrawFocusRect(dis->hDC, &fr);
    }
}

void draw_check(const DRAWITEMSTRUCT* dis) {
    bool disabled = (dis->itemState & ODS_DISABLED) != 0;
    bool checked =
        SendMessageW(dis->hwndItem, BM_GETCHECK, 0, 0) == BST_CHECKED;
    theme::fill_round(dis->hDC, dis->rcItem, theme::kCard, 4);
    RECT box{dis->rcItem.left, dis->rcItem.top + 2,
             dis->rcItem.left + 18, dis->rcItem.top + 20};
    theme::fill_chamfer(dis->hDC, box, 5,
                        checked ? theme::kAccent : theme::kField,
                        checked ? theme::kAccentHot : theme::kFieldEdge);
    if (checked) {
        /* diamond pip — reticle-style confirm marker */
        int cx = box.left + 9, cy = box.top + 9;
        POINT d[4]{{cx, cy - 5}, {cx + 5, cy}, {cx, cy + 5}, {cx - 5, cy}};
        HGDIOBJ ob = SelectObject(dis->hDC, theme::brush(theme::kOnAccent));
        HGDIOBJ op = SelectObject(dis->hDC, GetStockObject(NULL_PEN));
        Polygon(dis->hDC, d, 4);
        SelectObject(dis->hDC, ob);
        SelectObject(dis->hDC, op);
    }
    wchar_t buf[160];
    GetWindowTextW(dis->hwndItem, buf, 160);
    RECT tr{box.right + 8, dis->rcItem.top, dis->rcItem.right,
            dis->rcItem.bottom};
    theme::text(dis->hDC, narrow(buf), tr,
                disabled ? theme::kMuted : theme::kText, g_app.font);
}

void draw_list_item(const DRAWITEMSTRUCT* dis) {
    if (dis->itemID == (UINT)-1) return;
    bool sel = (dis->itemState & ODS_SELECTED) != 0;
    bool hot = widgets::list_hot_item(dis->hwndItem) == (int)dis->itemID;
    theme::fill_round(dis->hDC, dis->rcItem,
                      sel ? theme::kSelBg
                          : hot ? theme::kSecHot : theme::kField, 4);
    wchar_t buf[512];
    buf[0] = 0;
    SendMessageW(dis->hwndItem, LB_GETTEXT, dis->itemID, (LPARAM)buf);
    RECT tr{dis->rcItem.left + 8, dis->rcItem.top, dis->rcItem.right - 8,
            dis->rcItem.bottom};
    theme::text(dis->hDC, narrow(buf), tr,
                sel ? theme::kText : RGB(0xC9, 0xCE, 0xD6), g_app.font);
    if (sel) {
        RECT bar{dis->rcItem.left, dis->rcItem.top + 3,
                 dis->rcItem.left + 3, dis->rcItem.bottom - 3};
        theme::fill_round(dis->hDC, bar, theme::kAccent, 3);
    }
}

void draw_combo_item(const DRAWITEMSTRUCT* dis) {
    theme::fill_round(dis->hDC, dis->rcItem, theme::kField, 4);
    wchar_t buf[256];
    buf[0] = 0;
    std::string text = tr::kDestEmpty;
    COLORREF color = theme::kMuted;
    int sel = (int)SendMessageW(dis->hwndItem, CB_GETCURSEL, 0, 0);
    if (dis->itemID != (UINT)-1) {
        SendMessageW(dis->hwndItem, CB_GETLBTEXT, dis->itemID, (LPARAM)buf);
        color = theme::kText;
        text = narrow(buf);
    } else if (sel >= 0) {
        SendMessageW(dis->hwndItem, CB_GETLBTEXT, sel, (LPARAM)buf);
        color = theme::kText;
        text = narrow(buf);
    }
    RECT tr{dis->rcItem.left + 8, dis->rcItem.top, dis->rcItem.right - 24,
            dis->rcItem.bottom};
    theme::text(dis->hDC, text, tr, color, g_app.font);
    /* chevron on the selection field */
    if (dis->itemID == (UINT)-1 || !(dis->itemState & ODS_COMBOBOXEDIT)) {
        if (dis->itemID == (UINT)-1) {
            int cx = dis->rcItem.right - 16, cy =
                (dis->rcItem.top + dis->rcItem.bottom) / 2;
            HPEN pen = CreatePen(PS_SOLID, 1, theme::kMuted);
            HGDIOBJ op = SelectObject(dis->hDC, pen);
            MoveToEx(dis->hDC, cx - 3, cy - 1, nullptr);
            LineTo(dis->hDC, cx, cy + 2);
            LineTo(dis->hDC, cx + 3, cy - 1);
            SelectObject(dis->hDC, op);
            DeleteObject(pen);
        }
    }
}

COLORREF conn_color() {
    switch (g_app.st.backend.state()) {
        case WsClient::State::Connected:
            return g_blink ? theme::kGreen : theme::kGreenDim;
        case WsClient::State::Connecting:
        case WsClient::State::Handshaking:
            return g_blink ? theme::kAmber : theme::kMuted;
        default: return theme::kRed;
    }
}

LRESULT CALLBACK content_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_COMMAND:
            handle_command(LOWORD(wp), HIWORD(wp));
            return 0;
        case WM_HSCROLL:
            handle_hscroll((HWND)lp);
            return 0;
        case WM_ERASEBKGND: {
            RECT rc;
            GetClientRect(hwnd, &rc);
            FillRect((HDC)wp, &rc, theme::grid_brush());
            return 1;
        }
        case WM_PAINT: {
            PAINTSTRUCT ps;
            HDC dc = BeginPaint(hwnd, &ps);
            RECT cr;
            GetClientRect(hwnd, &cr);
            const int cw = cr.right;
            /* header band: gradient + accent underline (HUD strip) */
            RECT band{0, 0, cw, 118};
            theme::vgrad(dc, band, theme::kHeaderHi, theme::kBg);
            /* diagonal tech hatch on the right half of the band */
            RECT hatch{cw - 220, 0, cw, 104};
            theme::diag_hatch(dc, hatch, 12, 26, theme::kAccentDim);
            /* travelling scan sliver on the accent rule */
            int scan_w = cw + 160;
            int sx = (g_scan_phase % scan_w) - 160;
            theme::accent_rule(dc, 18, 108, 200);
            theme::accent_rule(dc, 218, 108, 60, theme::kAccentDim);
            theme::accent_rule(dc, sx, 108, 120, theme::kAccentDim);
            theme::accent_rule(dc, sx + 110, 108, 10, theme::kAccentHot);
            /* hex unit emblem inside the hatch zone */
            theme::hex_badge(dc, cw - 42, 34, 12,
                             theme::kAccentDim, theme::kAccent);
            /* connection badge chip */
            {
                auto st = g_app.st.backend.state();
                const char* label =
                    st == WsClient::State::Connected ? "LINK"
                    : st == WsClient::State::Disconnected ? "OFFLINE"
                                                        : "SYNC";
                COLORREF cc = conn_color();
                RECT chip{cw - 148, 68, cw - 58, 92};
                theme::fill_chamfer(dc, chip, 8, theme::kCard, cc);
                theme::text(dc, label, chip, cc, g_app.font_tech,
                            DT_CENTER | DT_VCENTER | DT_SINGLELINE);
            }
            /* accent underline under the focused edit field */
            if (HWND f = GetFocus(); f && GetParent(f) == hwnd) {
                wchar_t cls[16]{};
                GetClassNameW(f, cls, 16);
                if (wcscmp(cls, L"Edit") == 0) {
                    RECT er;
                    GetWindowRect(f, &er);
                    MapWindowPoints(nullptr, hwnd, (POINT*)&er, 2);
                    theme::accent_rule(dc, er.left + 2, er.bottom,
                                       er.right - er.left - 4,
                                       theme::kAccent, 2);
                }
            }
            int sec = 1;
            for (const RECT& c : g_app.cards) {
                theme::card(dc, c);
                char tag[16];
                std::snprintf(tag, sizeof(tag), "SEC.0%d", sec++);
                RECT tr{c.right - 92, c.top + 8, c.right - 14, c.top + 24};
                theme::text(dc, tag, tr, theme::kTag, g_app.font_tech,
                            DT_RIGHT | DT_VCENTER | DT_SINGLELINE |
                                DT_NOPREFIX);
            }
            EndPaint(hwnd, &ps);
            return 0;
        }
        case WM_MEASUREITEM: {
            auto* mi = (MEASUREITEMSTRUCT*)lp;
            mi->itemHeight = 24;
            return TRUE;
        }
        case WM_DRAWITEM: {
            auto* dis = (const DRAWITEMSTRUCT*)lp;
            if (dis->CtlType == ODT_BUTTON) {
                if (GetPropW(dis->hwndItem, L"fsui.check"))
                    draw_check(dis);
                else
                    draw_button(dis);
            } else if (dis->CtlType == ODT_LISTBOX) {
                draw_list_item(dis);
            } else if (dis->CtlType == ODT_COMBOBOX) {
                draw_combo_item(dis);
            }
            return TRUE;
        }
        case WM_CTLCOLORSTATIC: {
            HDC dc = (HDC)wp;
            HWND ctl = (HWND)lp;
            SetBkMode(dc, OPAQUE);
            SetBkColor(dc, GetPropW(ctl, theme::kPropOnCard)
                               ? theme::kCard
                               : theme::kBg);
            if (ctl == g_app.ui.conn_dot)
                SetTextColor(dc, conn_color());
            else if (theme::is_cyan(ctl))
                SetTextColor(dc, theme::kAccent);
            else
                SetTextColor(dc, theme::is_muted(ctl) ? theme::kMuted
                                                    : theme::kText);
            return (LRESULT)theme::brush(GetPropW(ctl, theme::kPropOnCard)
                                             ? theme::kCard
                                             : theme::kBg);
        }
        case WM_CTLCOLOREDIT: {
            HDC dc = (HDC)wp;
            SetBkColor(dc, theme::kField);
            SetTextColor(dc, theme::kText);
            return (LRESULT)theme::brush(theme::kField);
        }
        case WM_CTLCOLORLISTBOX: {
            HDC dc = (HDC)wp;
            SetBkColor(dc, theme::kField);
            SetTextColor(dc, theme::kText);
            return (LRESULT)theme::brush(theme::kField);
        }
        case WM_CTLCOLORBTN:
            return (LRESULT)theme::brush(theme::kCard);
        case WM_MOUSEWHEEL:
            scroll_content(GET_WHEEL_DELTA_WPARAM(wp) > 0 ? -48 : 48);
            return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

void on_tick() {
    AppState& s = g_app.st;
    static unsigned long long disconnected_since = 0;
    if (s.backend.state() == WsClient::State::Disconnected) {
        unsigned long long now = GetTickCount64();
        if (disconnected_since == 0) disconnected_since = now;
        if (now - disconnected_since >= RECONNECT_MS && !s.ws_url.empty()) {
            disconnected_since = now;
            s.backend.connect(s.ws_url);
        }
    } else {
        disconnected_since = 0;
    }
    s.pump_queue();
    sync_ui();
    /* HUD animation: scan sliver sweep + status-dot breathing */
    g_scan_phase += 16;
    if (g_scan_phase > 100000) g_scan_phase = 0;
    static int blink_tick = 0;
    if (++blink_tick >= 3) {
        blink_tick = 0;
        g_blink = !g_blink;
        InvalidateRect(g_app.ui.conn_dot, nullptr, TRUE);
    }
    RECT band{0, 0, 2000, 112};
    InvalidateRect(g_app.content, &band, FALSE);
}

LRESULT CALLBACK main_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_APP_SOCKET:
            g_app.st.backend.on_socket_event(wp, lp);
            sync_ui();
            g_app.st.pump_queue();
            return 0;
        case WM_TIMER:
            if (wp == TIMER_TICK) on_tick();
            return 0;
        case WM_SIZE: {
            RECT rc;
            GetClientRect(hwnd, &rc);
            if (g_app.content && rc.right > 0 &&
                rc.right != g_app.content_w) {
                /* responsive relayout: rebuild controls at new width */
                g_app.content_w = rc.right;
                SetWindowPos(g_app.content, nullptr, 0, -g_app.scroll_y,
                             rc.right, g_app.content_h, SWP_NOZORDER);
                EnumChildWindows(g_app.content,
                                 [](HWND c, LPARAM) -> BOOL {
                                     DestroyWindow(c);
                                     return TRUE;
                                 },
                                 0);
                g_app.cards.clear();
                build_layout(g_app.content);
            }
            update_scrollbar();
            sync_ui();
            return 0;
        }
        case WM_VSCROLL: {
            int code = LOWORD(wp);
            if (code == SB_LINEUP) scroll_content(-40);
            else if (code == SB_LINEDOWN) scroll_content(40);
            else if (code == SB_PAGEUP) scroll_content(-240);
            else if (code == SB_PAGEDOWN) scroll_content(240);
            else if (code == SB_THUMBTRACK || code == SB_THUMBPOSITION) {
                g_app.scroll_y = HIWORD(wp);
                scroll_content(0);
            }
            return 0;
        }
        case WM_MOUSEWHEEL:
            scroll_content(GET_WHEEL_DELTA_WPARAM(wp) > 0 ? -48 : 48);
            return 0;
        case WM_DESTROY:
            PostQuitMessage(0);
            return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

HFONT make_font(int height, int weight, const wchar_t* face) {
    return CreateFontW(-height, 0, 0, 0, weight, FALSE, FALSE, FALSE,
                       DEFAULT_CHARSET, OUT_DEFAULT_PRECIS,
                       CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
                       DEFAULT_PITCH | FF_DONTCARE, face);
}

} // namespace

void on_socket_frame(const std::string& frame) {
    JsonValue payload;
    std::string event = P::decode_event(frame, &payload);
    if (event.empty()) return;
    g_app.st.handle_event(event, payload);
}

void on_backend_state(WsClient::State s) {
    if (s == WsClient::State::Disconnected) g_app.st.on_socket_closed();
}

bool ui_confirm(const std::string& text) {
    return MessageBoxW(g_confirm_parent, widen(text).c_str(),
                       widen(tr::kConfirmTitle).c_str(),
                       MB_OKCANCEL | MB_ICONWARNING | MB_SETFOREGROUND) == IDOK;
}

} // namespace fsui

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE, PWSTR, int show) {
    using namespace fsui;
    INITCOMMONCONTROLSEX icc{sizeof(icc), ICC_BAR_CLASSES | ICC_PROGRESS_CLASS |
                                          ICC_STANDARD_CLASSES | ICC_UPDOWN_CLASS};
    InitCommonControlsEx(&icc);
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);

    g_app.hwnd = nullptr;
    g_confirm_parent = nullptr;
    g_app.font = make_font(15, FW_NORMAL, L"Microsoft JhengHei UI");
    g_app.font_bold = make_font(15, FW_SEMIBOLD, L"Microsoft JhengHei UI");
    g_app.font_mono = make_font(14, FW_NORMAL, L"Consolas");
    g_app.font_heading = make_font(20, FW_SEMIBOLD, L"Microsoft JhengHei UI");
    g_app.font_small = make_font(13, FW_NORMAL, L"Microsoft JhengHei UI");
    g_app.font_tech = make_font(14, FW_SEMIBOLD, L"Bahnschrift SemiBold");

    widgets::register_classes(inst);

    WNDCLASSEXW wc{sizeof(wc)};
    wc.lpfnWndProc = main_proc;
    wc.hInstance = inst;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hbrBackground = theme::brush(theme::kBg);
    wc.lpszClassName = kMainClass;
    RegisterClassExW(&wc);

    WNDCLASSEXW cc{sizeof(cc)};
    cc.lpfnWndProc = content_proc;
    cc.hInstance = inst;
    cc.hbrBackground = theme::brush(theme::kBg);
    cc.lpszClassName = kContentClass;
    RegisterClassExW(&cc);

    std::string title = env("GPTBRIDGE_SOURCE_UI_TITLE");
    if (title.empty()) title = tr::kTitle;
    std::string full = "GPTBridge \xC2\xB7 " + title; /* "· " */

    int w = (int)strtol(env("GPTBRIDGE_SOURCE_UI_WIDTH").c_str(), nullptr, 10);
    int h = (int)strtol(env("GPTBRIDGE_SOURCE_UI_HEIGHT").c_str(), nullptr, 10);
    if (w < 940) w = 940;
    if (h < 720) h = 720;

    g_app.hwnd = CreateWindowExW(
        0, kMainClass, widen(full).c_str(),
        WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN,
        CW_USEDEFAULT, CW_USEDEFAULT, w, h,
        nullptr, nullptr, inst, nullptr);
    if (!g_app.hwnd) return 2;
    g_confirm_parent = g_app.hwnd;

    /* Fluent-style dark title bar (Win10 20H1+; harmless no-op earlier) */
    BOOL dark = TRUE;
    DwmSetWindowAttribute(g_app.hwnd, 20 /* DWMWA_USE_IMMERSIVE_DARK_MODE */,
                          &dark, sizeof(dark));

    g_app.content = CreateWindowExW(
        0, kContentClass, L"", WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN,
        0, 0, w, 1400, g_app.hwnd, nullptr, inst, nullptr);
    build_layout(g_app.content);
    {
        RECT cwr;
        GetClientRect(g_app.content, &cwr);
        g_app.content_w = cwr.right;
    }
    g_app.scroll = widgets::create_scroll(g_app.hwnd, w - 10, 0, 10, h);

    g_app.st.ws_url = env("GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL");
    g_app.st.cleanup_message = tr::kMsgCleanupReady;
    g_app.st.backend.init(
        g_app.hwnd, WM_APP_SOCKET,
        [](const std::string& f) { on_socket_frame(f); },
        [](WsClient::State s) { on_backend_state(s); });
    if (!g_app.st.ws_url.empty()) g_app.st.backend.connect(g_app.st.ws_url);

    SetTimer(g_app.hwnd, TIMER_TICK, TICK_MS, nullptr);
    update_scrollbar();
    sync_ui();
    ShowWindow(g_app.hwnd, show);
    UpdateWindow(g_app.hwnd);

    MSG msg;
    while (GetMessageW(&msg, nullptr, 0, 0)) {
        if (!IsDialogMessageW(g_app.content, &msg) &&
            !IsDialogMessageW(g_app.hwnd, &msg)) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
    g_app.st.backend.close();
    CoUninitialize();
    return 0;
}

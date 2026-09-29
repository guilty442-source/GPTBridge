/* app.cpp — file-sorter native tool window (Win32 + WinSock2).
 *
 * Launch contract (same as the egui surface): spawned with
 *   --tool-window --tool-id=file-sorter
 * and the GPTBRIDGE_SOURCE_UI_* environment block; speaks
 * toolbox_run_tool JSON frames over the loopback backend WS. */
#include "app.h"

#include <commctrl.h>
#include <shellapi.h>

#include <cstdio>
#include <cstdlib>
#include <string>

#include "parse.h"
#include "protocol.h"
#include "strings.h"
#include "utf8.h"

#pragma comment(linker, \
    "\"/manifestdependency:type='win32' "\
    "name='Microsoft.Windows.Common-Controls' version='6.0.0.0' "\
    "processorArchitecture='*' publicKeyToken='6595b64144ccf1df' "\
    "language='*'\"")

namespace fsui {

namespace P = proto;

App g_app;
HWND g_confirm_parent = nullptr;

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

void handle_command(int id, int code) {
    AppState& s = g_app.st;
    Ui& u = g_app.ui;
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
        case IDC_BTN_PREVIEW:
            s.has_plan = false;
            s.sort_plan = JsonValue{};
            s.plan_confirmed = false;
            s.enqueue(RunKind::Preview, {target, "--preview-json"},
                      kShortTimeoutG(), tr::kMsgPreview, tr::kMsgPreviewDone);
            break;
        case IDC_BTN_UNDO:
            if (ui_confirm(tr::kConfirmUndo))
                s.enqueue(RunKind::Undo, {target, "--undo-last"},
                          kRunTimeoutG(), tr::kMsgUndo, tr::kMsgUndoDone);
            break;
        case IDC_BTN_HISTORY:
            s.history_open = !s.history_open;
            if (s.history_open && s.can_run())
                s.enqueue(RunKind::History, {target, "--history-json"},
                          kShortTimeoutG(), tr::kMsgHistory, tr::kMsgHistoryDone);
            break;
        case IDC_PLAN_CONFIRM:
            s.plan_confirmed =
                SendMessageW(u.plan_confirm, BM_GETCHECK, 0, 0) == BST_CHECKED;
            break;
        case IDC_BTN_APPLY:
            if (s.has_plan)
                s.enqueue(RunKind::ApplyPlan,
                          {target, "--apply-plan", fsp::plan_id(s.sort_plan)},
                          kRunTimeoutG(), tr::kMsgApply, tr::kMsgApplyDone);
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
    SCROLLINFO si{};
    si.cbSize = sizeof(si);
    si.fMask = SIF_RANGE | SIF_PAGE | SIF_POS;
    si.nMin = 0;
    si.nMax = g_app.content_h;
    si.nPage = rc.bottom;
    si.nPos = g_app.scroll_y;
    SetScrollInfo(g_app.hwnd, SB_VERT, &si, TRUE);
    int max_pos = (int)(g_app.content_h - rc.bottom);
    if (max_pos < 0) max_pos = 0;
    if (g_app.scroll_y > max_pos) g_app.scroll_y = max_pos;
    SetWindowPos(g_app.content, nullptr, 0, -g_app.scroll_y, rc.right,
                 g_app.content_h, SWP_NOZORDER);
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
    SetScrollPos(g_app.hwnd, SB_VERT, g_app.scroll_y, TRUE);
}

LRESULT CALLBACK content_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_COMMAND:
            handle_command(LOWORD(wp), HIWORD(wp));
            return 0;
        case WM_HSCROLL:
            handle_hscroll((HWND)lp);
            return 0;
        case WM_CTLCOLORSTATIC:
            SetBkMode((HDC)wp, TRANSPARENT);
            return (LRESULT)GetSysColorBrush(COLOR_WINDOW);
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
        case WM_SIZE:
            update_scrollbar();
            return 0;
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

    g_app.hwnd = nullptr;
    g_confirm_parent = nullptr;
    g_app.font = make_font(15, FW_NORMAL, L"Microsoft JhengHei UI");
    g_app.font_bold = make_font(15, FW_SEMIBOLD, L"Microsoft JhengHei UI");
    g_app.font_mono = make_font(14, FW_NORMAL, L"Consolas");
    g_app.font_heading = make_font(19, FW_SEMIBOLD, L"Microsoft JhengHei UI");

    WNDCLASSEXW wc{sizeof(wc)};
    wc.lpfnWndProc = main_proc;
    wc.hInstance = inst;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    wc.lpszClassName = kMainClass;
    RegisterClassExW(&wc);

    WNDCLASSEXW cc{sizeof(cc)};
    cc.lpfnWndProc = content_proc;
    cc.hInstance = inst;
    cc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
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
        WS_OVERLAPPEDWINDOW | WS_VSCROLL | WS_CLIPCHILDREN,
        CW_USEDEFAULT, CW_USEDEFAULT, w, h,
        nullptr, nullptr, inst, nullptr);
    if (!g_app.hwnd) return 2;
    g_confirm_parent = g_app.hwnd;

    g_app.content = CreateWindowExW(
        0, kContentClass, L"", WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN,
        0, 0, w, 1400, g_app.hwnd, nullptr, inst, nullptr);
    build_layout(g_app.content);

    g_app.st.ws_url = env("GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL");
    g_app.st.message = tr::kMsgReady;
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
    return 0;
}

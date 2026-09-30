/* widgets.cpp — modern-drawn control implementations. */
#include "widgets.h"

#include <commctrl.h>

#include "theme.h"

namespace fsui {
namespace widgets {

namespace {

/* ---------------- shared hover subclass ---------------- */

LRESULT CALLBACK hover_proc(HWND h, UINT msg, WPARAM wp, LPARAM lp,
                            UINT_PTR, DWORD_PTR) {
    switch (msg) {
        case WM_MOUSEMOVE:
            if (!GetPropW(h, theme::kPropHot)) {
                SetPropW(h, theme::kPropHot, (HANDLE)1);
                TRACKMOUSEEVENT tme{sizeof(tme), TME_LEAVE, h, 0};
                TrackMouseEvent(&tme);
                InvalidateRect(h, nullptr, FALSE);
            }
            break;
        case WM_MOUSELEAVE:
            SetPropW(h, theme::kPropHot, (HANDLE)0);
            InvalidateRect(h, nullptr, FALSE);
            break;
        case WM_LBUTTONDOWN:
        case WM_LBUTTONUP:
            InvalidateRect(h, nullptr, FALSE);
            break;
        case WM_NCDESTROY:
            RemovePropW(h, theme::kPropHot);
            break;
    }
    return DefSubclassProc(h, msg, wp, lp);
}

/* ---------------- FsuiSlider ---------------- */

struct SliderState {
    int lo = 0, hi = 100, pos = 0;
    bool dragging = false;
};

SliderState* sl(HWND h) {
    return (SliderState*)GetPropW(h, L"fsui.sl");
}

void sl_notify(HWND h) {
    HWND parent = GetParent(h);
    if (parent)
        SendMessageW(parent, WM_HSCROLL,
                     MAKEWPARAM(SB_THUMBTRACK, sl(h)->pos), (LPARAM)h);
}

int sl_pos_from_x(HWND h, int x) {
    SliderState* s = sl(h);
    RECT rc;
    GetClientRect(h, &rc);
    int track_l = 8, track_r = rc.right - 8;
    if (x < track_l) x = track_l;
    if (x > track_r) x = track_r;
    int range = s->hi - s->lo;
    double t = (double)(x - track_l) / (track_r - track_l);
    return s->lo + (int)(t * range + 0.5);
}

LRESULT CALLBACK slider_proc(HWND h, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_NCCREATE: {
            auto* s = new SliderState();
            SetPropW(h, L"fsui.sl", s);
            return TRUE;
        }
        case WM_NCDESTROY:
            delete sl(h);
            RemovePropW(h, L"fsui.sl");
            return 0;
        case TBM_SETRANGE:
            sl(h)->lo = LOWORD(lp);
            sl(h)->hi = HIWORD(lp);
            return TRUE;
        case TBM_SETTICFREQ:
            return TRUE;
        case TBM_SETPOS:
            sl(h)->pos = (int)lp;
            InvalidateRect(h, nullptr, FALSE);
            return TRUE;
        case TBM_GETPOS:
            return sl(h)->pos;
        case WM_LBUTTONDOWN: {
            SetCapture(h);
            sl(h)->dragging = true;
            sl(h)->pos = sl_pos_from_x(h, (int)(short)LOWORD(lp));
            InvalidateRect(h, nullptr, FALSE);
            sl_notify(h);
            return 0;
        }
        case WM_MOUSEMOVE:
            if (sl(h)->dragging) {
                sl(h)->pos = sl_pos_from_x(h, (int)(short)LOWORD(lp));
                InvalidateRect(h, nullptr, FALSE);
                sl_notify(h);
            }
            return 0;
        case WM_LBUTTONUP:
            if (sl(h)->dragging) {
                sl(h)->dragging = false;
                ReleaseCapture();
                sl_notify(h);
            }
            return 0;
        case WM_ERASEBKGND:
            return 1;
        case WM_PAINT: {
            PAINTSTRUCT ps;
            HDC dc = BeginPaint(h, &ps);
            RECT rc;
            GetClientRect(h, &rc);
            SliderState* s = sl(h);
            /* track */
            int cy = rc.bottom / 2;
            RECT track{8, cy - 3, rc.right - 8, cy + 3};
            theme::fill_round(dc, track, theme::kCardEdge, 6);
            int range = s->hi - s->lo;
            double t = range > 0 ? (double)(s->pos - s->lo) / range : 0.0;
            int fillw = 8 + (int)(t * (rc.right - 16));
            RECT filled{8, cy - 3, fillw, cy + 3};
            if (filled.right > filled.left) {
                theme::fill_round(dc, filled, theme::kAccent, 6);
                /* glowing leading cap */
                RECT cap{fillw - 3, cy - 3, fillw, cy + 3};
                theme::fill_round(dc, cap, theme::kAccentHot, 4);
            }
            /* thumb: chamfered hexagon — dark core, neon edge */
            int tx = 8 + (int)(t * (rc.right - 16));
            POINT hex[6]{{tx - 4, cy - 8}, {tx + 4, cy - 8},
                         {tx + 8, cy},     {tx + 4, cy + 8},
                         {tx - 4, cy + 8}, {tx - 8, cy}};
            HGDIOBJ ob = SelectObject(dc, theme::brush(theme::kField));
            HPEN ring = CreatePen(PS_SOLID, 2, theme::kAccentHot);
            HGDIOBJ op = SelectObject(dc, ring);
            Polygon(dc, hex, 6);
            /* inner core dot */
            SelectObject(dc, GetStockObject(NULL_PEN));
            SelectObject(dc, theme::brush(theme::kAccent));
            Ellipse(dc, tx - 2, cy - 2, tx + 2, cy + 2);
            SelectObject(dc, ob);
            SelectObject(dc, op);
            DeleteObject(ring);
            EndPaint(h, &ps);
            return 0;
        }
    }
    return DefWindowProcW(h, msg, wp, lp);
}

/* ---------------- FsuiProgress ---------------- */

LRESULT CALLBACK progress_proc(HWND h, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_NCCREATE:
            SetPropW(h, L"fsui.pct", (HANDLE)0);
            return TRUE;
        case WM_NCDESTROY:
            RemovePropW(h, L"fsui.pct");
            return 0;
        case PBM_SETPOS:
            SetPropW(h, L"fsui.pct", (HANDLE)(INT_PTR)wp);
            InvalidateRect(h, nullptr, FALSE);
            return 0;
        case PBM_GETPOS:
            return (LRESULT)GetPropW(h, L"fsui.pct");
        case WM_ERASEBKGND:
            return 1;
        case WM_PAINT: {
            PAINTSTRUCT ps;
            HDC dc = BeginPaint(h, &ps);
            RECT rc;
            GetClientRect(h, &rc);
            theme::fill_round(dc, rc, theme::kField, rc.bottom, theme::kCardEdge);
            int pct = (int)(INT_PTR)GetPropW(h, L"fsui.pct");
            if (pct > 0) {
                RECT fill{rc.left + 1, rc.top + 1,
                          rc.left + (int)((rc.right - 2) * pct / 100.0),
                          rc.bottom - 1};
                theme::fill_round(dc, fill, theme::kAccentDn, rc.bottom - 2);
                /* diagonal energy stripes inside the fill */
                int saved = SaveDC(dc);
                IntersectClipRect(dc, fill.left, fill.top, fill.right,
                                  fill.bottom);
                static int stripe_phase = 0;
                stripe_phase = (stripe_phase + 2) % 14;
                HPEN pen = CreatePen(PS_SOLID, 2, theme::kAccent);
                HGDIOBJ op = SelectObject(dc, pen);
                for (int x = fill.left - 12 + stripe_phase; x < fill.right;
                     x += 14) {
                    MoveToEx(dc, x, fill.bottom, nullptr);
                    LineTo(dc, x + 8, fill.top);
                }
                SelectObject(dc, op);
                DeleteObject(pen);
                RestoreDC(dc, saved);
                /* neon cap */
                RECT cap{fill.right - 3, fill.top, fill.right,
                         fill.bottom};
                if (cap.right > cap.left)
                    theme::fill_round(dc, cap, theme::kAccentHot,
                                      rc.bottom - 2);
            }
            EndPaint(h, &ps);
            return 0;
        }
    }
    return DefWindowProcW(h, msg, wp, lp);
}

/* ---------------- FsuiScroll (overlay vertical bar) ---------------- */

struct ScrollState {
    int max = 0, page = 0, pos = 0;
    bool dragging = false;
    int grab = 0;
};

ScrollState* sc(HWND h) { return (ScrollState*)GetPropW(h, L"fsui.sc"); }

void sc_notify(HWND h, int code) {
    HWND parent = GetParent(h);
    if (parent)
        SendMessageW(parent, WM_VSCROLL,
                     MAKEWPARAM(code, sc(h)->pos), (LPARAM)h);
}

void sc_clamp(ScrollState* s) {
    int maxPos = s->max - s->page;
    if (maxPos < 0) maxPos = 0;
    if (s->pos < 0) s->pos = 0;
    if (s->pos > maxPos) s->pos = maxPos;
}

LRESULT CALLBACK scroll_proc(HWND h, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_NCCREATE:
            SetPropW(h, L"fsui.sc", new ScrollState());
            return TRUE;
        case WM_NCDESTROY:
            delete sc(h);
            RemovePropW(h, L"fsui.sc");
            return 0;
        case WM_LBUTTONDOWN: {
            ScrollState* s = sc(h);
            RECT rc;
            GetClientRect(h, &rc);
            int maxPos = s->max - s->page;
            if (maxPos <= 0) return 0;
            int thumb_h = (int)((double)rc.bottom * s->page / s->max);
            if (thumb_h < 28) thumb_h = 28;
            int track = rc.bottom - thumb_h;
            int ty = track > 0 ? (int)((double)s->pos / maxPos * track) : 0;
            int my = (int)(short)HIWORD(lp);
            if (my >= ty && my <= ty + thumb_h) {
                s->dragging = true;
                s->grab = my - ty;
                SetCapture(h);
            } else {
                s->pos += my < ty ? -s->page : s->page;
                sc_clamp(s);
                InvalidateRect(h, nullptr, FALSE);
                sc_notify(h, SB_THUMBPOSITION);
            }
            return 0;
        }
        case WM_MOUSEMOVE: {
            ScrollState* s = sc(h);
            if (!s->dragging) return 0;
            RECT rc;
            GetClientRect(h, &rc);
            int maxPos = s->max - s->page;
            int thumb_h = (int)((double)rc.bottom * s->page / s->max);
            if (thumb_h < 28) thumb_h = 28;
            int track = rc.bottom - thumb_h;
            if (track <= 0 || maxPos <= 0) return 0;
            int my = (int)(short)HIWORD(lp) - s->grab;
            s->pos = (int)((double)my / track * maxPos + 0.5);
            sc_clamp(s);
            InvalidateRect(h, nullptr, FALSE);
            sc_notify(h, SB_THUMBTRACK);
            return 0;
        }
        case WM_LBUTTONUP:
            if (sc(h)->dragging) {
                sc(h)->dragging = false;
                ReleaseCapture();
            }
            return 0;
        case WM_ERASEBKGND:
            return 1;
        case WM_PAINT: {
            PAINTSTRUCT ps;
            HDC dc = BeginPaint(h, &ps);
            RECT rc;
            GetClientRect(h, &rc);
            ScrollState* s = sc(h);
            int maxPos = s->max - s->page;
            if (maxPos <= 0) { /* nothing to scroll */ }
            else {
                int thumb_h = (int)((double)rc.bottom * s->page / s->max);
                if (thumb_h < 28) thumb_h = 28;
                int track = rc.bottom - thumb_h;
                int ty = (int)((double)s->pos / maxPos * track);
                RECT thumb{rc.left + 2, ty, rc.right - 2, ty + thumb_h};
                theme::fill_round(dc, thumb,
                                  s->dragging ? theme::kAccent
                                              : theme::kAccentDim,
                                  8);
            }
            EndPaint(h, &ps);
            return 0;
        }
    }
    return DefWindowProcW(h, msg, wp, lp);
}

} // namespace

void register_classes(HINSTANCE inst) {
    auto reg = [&](const wchar_t* name, WNDPROC proc) {
        WNDCLASSEXW wc{sizeof(wc)};
        wc.lpfnWndProc = proc;
        wc.hInstance = inst;
        wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        wc.lpszClassName = name;
        RegisterClassExW(&wc);
    };
    reg(kClsSlider, slider_proc);
    reg(kClsProgress, progress_proc);
    reg(kClsScroll, scroll_proc);
}

void install_hover(HWND h) {
    SetWindowSubclass(h, hover_proc, 1, 0);
}

void scroll_set(HWND scroll, int max, int page) {
    ScrollState* s = sc(scroll);
    if (!s) return;
    s->max = max;
    s->page = page;
    sc_clamp(s);
    InvalidateRect(scroll, nullptr, FALSE);
}

void scroll_set_pos(HWND scroll, int pos) {
    ScrollState* s = sc(scroll);
    if (!s) return;
    s->pos = pos;
    sc_clamp(s);
    InvalidateRect(scroll, nullptr, FALSE);
}

HWND create_slider(HWND parent, int id, int x, int y, int w,
                   int lo, int hi, int pos) {
    HWND h = CreateWindowExW(0, kClsSlider, L"", WS_CHILD | WS_VISIBLE,
                             x, y, w, 28, parent, (HMENU)(INT_PTR)id,
                             GetModuleHandleW(nullptr), nullptr);
    SendMessageW(h, TBM_SETRANGE, TRUE, MAKELPARAM(lo, hi));
    SendMessageW(h, TBM_SETPOS, TRUE, pos);
    return h;
}

HWND create_progress(HWND parent, int id, int x, int y, int w, int hh) {
    return CreateWindowExW(0, kClsProgress, L"", WS_CHILD | WS_VISIBLE,
                           x, y, w, hh, parent, (HMENU)(INT_PTR)id,
                           GetModuleHandleW(nullptr), nullptr);
}

HWND create_scroll(HWND parent, int x, int y, int w, int h) {
    return CreateWindowExW(0, kClsScroll, L"", WS_CHILD | WS_VISIBLE,
                           x, y, w, h, parent, nullptr,
                           GetModuleHandleW(nullptr), nullptr);
}

} // namespace widgets
} // namespace fsui

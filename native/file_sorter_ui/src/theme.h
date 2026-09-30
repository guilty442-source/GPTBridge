/* theme.h — cyber/HUD palette + GDI draw helpers for the file-sorter
 * tool window (game-style dark tech look; pure Win32/GDI). */
#ifndef GPTBRIDGE_FSUI_THEME_H
#define GPTBRIDGE_FSUI_THEME_H

#include <windows.h>
#include <uxtheme.h>

#include <cmath>
#include <string>

#include "utf8.h"

namespace fsui {
namespace theme {

/* Palette — deep-space base, neon-cyan accent. */
constexpr COLORREF kBg        = RGB(0x0A, 0x0E, 0x14);
constexpr COLORREF kHeaderHi  = RGB(0x0F, 0x1A, 0x26);
constexpr COLORREF kCard      = RGB(0x11, 0x17, 0x20);
constexpr COLORREF kCardEdge  = RGB(0x1E, 0x2D, 0x3C);
constexpr COLORREF kField     = RGB(0x0C, 0x12, 0x19);
constexpr COLORREF kFieldEdge = RGB(0x2A, 0x3E, 0x50);
constexpr COLORREF kText      = RGB(0xDD, 0xE6, 0xF0);
constexpr COLORREF kMuted     = RGB(0x74, 0x84, 0x94);
constexpr COLORREF kAccent    = RGB(0x00, 0xD8, 0xF0);
constexpr COLORREF kAccentHot = RGB(0x3C, 0xEA, 0xFF);
constexpr COLORREF kAccentDn  = RGB(0x00, 0xB4, 0xCC);
constexpr COLORREF kAccentDim = RGB(0x0A, 0x3A, 0x44);
constexpr COLORREF kSecondary = RGB(0x18, 0x22, 0x2E);
constexpr COLORREF kSecHot    = RGB(0x1E, 0x2E, 0x3C);
constexpr COLORREF kDisabled  = RGB(0x24, 0x2C, 0x36);
constexpr COLORREF kGreen     = RGB(0x35, 0xF0, 0xA0);
constexpr COLORREF kAmber     = RGB(0xFF, 0xB8, 0x4D);
constexpr COLORREF kRed       = RGB(0xFF, 0x5C, 0x7A);
constexpr COLORREF kSelBg     = RGB(0x0C, 0x2E, 0x3C);
constexpr COLORREF kOnAccent  = RGB(0x04, 0x10, 0x14);
constexpr COLORREF kGrid      = RGB(0x15, 0x1E, 0x28);
constexpr COLORREF kTag       = RGB(0x2E, 0x5A, 0x66);
constexpr COLORREF kGreenDim  = RGB(0x16, 0x4A, 0x38);

inline HBRUSH brush(COLORREF c) {
    static HBRUSH cache[24];
    static COLORREF colors[24];
    static int n = 0;
    for (int i = 0; i < n; ++i)
        if (colors[i] == c) return cache[i];
    HBRUSH b = CreateSolidBrush(c);
    if (n < 24) { colors[n] = c; cache[n] = b; ++n; }
    return b;
}

inline void fill_round(HDC dc, RECT rc, COLORREF fill, int radius = 8,
                       COLORREF edge = (COLORREF)-1) {
    HBRUSH fb = brush(fill);
    HPEN pen = edge == (COLORREF)-1
        ? (HPEN)GetStockObject(NULL_PEN)
        : CreatePen(PS_SOLID, 1, edge);
    HGDIOBJ ob = SelectObject(dc, fb);
    HGDIOBJ op = SelectObject(dc, pen);
    RoundRect(dc, rc.left, rc.top, rc.right, rc.bottom, radius, radius);
    SelectObject(dc, ob);
    SelectObject(dc, op);
    if (edge != (COLORREF)-1) DeleteObject(pen);
}

/* Chamfered (cut-corner) panel — top-left and bottom-right corners are
 * clipped, the signature sci-fi HUD silhouette. */
inline void chamfer_pts(RECT rc, int cut, POINT* p) {
    p[0] = {rc.left + cut, rc.top};
    p[1] = {rc.right, rc.top};
    p[2] = {rc.right, rc.bottom - cut};
    p[3] = {rc.right - cut, rc.bottom};
    p[4] = {rc.left, rc.bottom};
    p[5] = {rc.left, rc.top + cut};
}

inline void fill_chamfer(HDC dc, RECT rc, int cut, COLORREF fill,
                         COLORREF edge = (COLORREF)-1) {
    POINT p[6];
    chamfer_pts(rc, cut, p);
    HGDIOBJ ob = SelectObject(dc, brush(fill));
    HPEN pen = edge == (COLORREF)-1
        ? (HPEN)GetStockObject(NULL_PEN)
        : CreatePen(PS_SOLID, 1, edge);
    HGDIOBJ op = SelectObject(dc, pen);
    Polygon(dc, p, 6);
    SelectObject(dc, ob);
    SelectObject(dc, op);
    if (edge != (COLORREF)-1) DeleteObject(pen);
}

/* small L-shaped corner tick */
inline void corner_tick(HDC dc, int x, int y, int dx, int dy, int L,
                        COLORREF c, int w = 1) {
    HPEN pen = CreatePen(PS_SOLID, w, c);
    HGDIOBJ op = SelectObject(dc, pen);
    MoveToEx(dc, x + dx * L, y, nullptr);
    LineTo(dc, x, y);
    LineTo(dc, x, y + dy * L);
    SelectObject(dc, op);
    DeleteObject(pen);
}

/* HUD-style card: chamfered panel + neon slash on the cut corner +
 * corner ticks + specular top edge (catches the light). */
inline void card(HDC dc, RECT rc) {
    fill_chamfer(dc, rc, 16, kCard, kCardEdge);
    /* faint CRT scanlines inside the panel */
    {
        HPEN sp = CreatePen(PS_SOLID, 1, kGrid);
        HGDIOBJ sop = SelectObject(dc, sp);
        int saved = SaveDC(dc);
        IntersectClipRect(dc, rc.left, rc.top, rc.right, rc.bottom);
        for (int y = rc.top + 4; y < rc.bottom; y += 5) {
            MoveToEx(dc, rc.left, y, nullptr);
            LineTo(dc, rc.right, y);
        }
        RestoreDC(dc, saved);
        SelectObject(dc, sop);
        DeleteObject(sp);
    }
    /* title marker bar — neon pip just left of the card heading,
     * clear of the corner slash */
    RECT pip{rc.left + 28, rc.top + 15, rc.left + 32, rc.top + 31};
    FillRect(dc, &pip, brush(kAccent));
    HPEN pen = CreatePen(PS_SOLID, 2, kAccent);
    HGDIOBJ op = SelectObject(dc, pen);
    MoveToEx(dc, rc.left + 2, rc.top + 22, nullptr);
    LineTo(dc, rc.left + 22, rc.top + 2);
    SelectObject(dc, op);
    DeleteObject(pen);
    /* specular highlight along the top edge — shallow glass feel */
    pen = CreatePen(PS_SOLID, 1, kFieldEdge);
    op = SelectObject(dc, pen);
    MoveToEx(dc, rc.left + 26, rc.top + 1, nullptr);
    LineTo(dc, rc.right - 1, rc.top + 1);
    SelectObject(dc, op);
    DeleteObject(pen);
    /* faint diagonal weave in the bottom-right corner */
    pen = CreatePen(PS_SOLID, 1, kSecondary);
    op = SelectObject(dc, pen);
    int saved = SaveDC(dc);
    IntersectClipRect(dc, rc.right - 130, rc.bottom - 40, rc.right - 8,
                      rc.bottom - 8);
    for (int x = rc.right - 130; x < rc.right; x += 9) {
        MoveToEx(dc, x, rc.bottom - 8, nullptr);
        LineTo(dc, x + 24, rc.bottom - 40);
    }
    RestoreDC(dc, saved);
    SelectObject(dc, op);
    DeleteObject(pen);
    corner_tick(dc, rc.right - 10, rc.top + 10, -1, 1, 12, kTag);
    corner_tick(dc, rc.left + 10, rc.bottom - 10, 1, -1, 12, kTag);
}

/* hexagon badge outline — HUD unit emblem. */
inline void hex_badge(HDC dc, int cx, int cy, int r, COLORREF edge,
                      COLORREF core) {
    POINT p[6];
    for (int i = 0; i < 6; ++i) {
        double a = 3.14159265 / 3.0 * i;
        p[i] = {cx + (int)(r * cos(a)), cy + (int)(r * 0.85 * sin(a))};
    }
    HPEN pen = CreatePen(PS_SOLID, 2, edge);
    HGDIOBJ ob = SelectObject(dc, GetStockObject(NULL_BRUSH));
    HGDIOBJ op = SelectObject(dc, pen);
    Polygon(dc, p, 6);
    SelectObject(dc, GetStockObject(NULL_PEN));
    SelectObject(dc, brush(core));
    Ellipse(dc, cx - 3, cy - 3, cx + 3, cy + 3);
    SelectObject(dc, ob);
    SelectObject(dc, op);
    DeleteObject(pen);
}

/* staggered dot-grid backdrop (pattern brush — one tile repeated). */
inline HBRUSH grid_brush() {
    static HBRUSH pat = nullptr;
    if (pat) return pat;
    HDC scr = GetDC(nullptr);
    HDC mem = CreateCompatibleDC(scr);
    HBITMAP bmp = CreateCompatibleBitmap(scr, 24, 24);
    HGDIOBJ ob = SelectObject(mem, bmp);
    RECT r{0, 0, 24, 24};
    FillRect(mem, &r, brush(kBg));
    SetPixel(mem, 4, 4, kGrid);
    SetPixel(mem, 16, 16, kGrid);
    SetPixel(mem, 4, 5, kGrid);
    SetPixel(mem, 16, 17, kGrid);
    /* CRT scanline row — every other pixel, one row per tile */
    for (int x = 0; x < 24; x += 2) SetPixel(mem, x, 22, kGrid);
    SelectObject(mem, ob);
    DeleteDC(mem);
    ReleaseDC(nullptr, scr);
    pat = CreatePatternBrush(bmp);
    DeleteObject(bmp);
    return pat;
}

/* diagonal hatch marks — tech texture for the header band. */
inline void diag_hatch(HDC dc, RECT rc, int step, int slope,
                       COLORREF c) {
    HPEN pen = CreatePen(PS_SOLID, 1, c);
    HGDIOBJ op = SelectObject(dc, pen);
    int saved = SaveDC(dc);
    IntersectClipRect(dc, rc.left, rc.top, rc.right, rc.bottom);
    for (int x = rc.left - slope; x < rc.right; x += step) {
        MoveToEx(dc, x, rc.bottom, nullptr);
        LineTo(dc, x + slope, rc.top);
    }
    RestoreDC(dc, saved);
    SelectObject(dc, op);
    DeleteObject(pen);
}

/* vertical two-stop gradient band (header backdrop) */
inline void vgrad(HDC dc, RECT rc, COLORREF top, COLORREF bottom) {
    TRIVERTEX v[2]{};
    v[0].x = rc.left;  v[0].y = rc.top;
    v[0].Red   = (COLOR16)(GetRValue(top) << 8);
    v[0].Green = (COLOR16)(GetGValue(top) << 8);
    v[0].Blue  = (COLOR16)(GetBValue(top) << 8);
    v[0].Alpha = 0;
    v[1].x = rc.right; v[1].y = rc.bottom;
    v[1].Red   = (COLOR16)(GetRValue(bottom) << 8);
    v[1].Green = (COLOR16)(GetGValue(bottom) << 8);
    v[1].Blue  = (COLOR16)(GetBValue(bottom) << 8);
    v[1].Alpha = 0;
    GRADIENT_RECT gr{0, 1};
    GradientFill(dc, v, 2, &gr, 1, GRADIENT_FILL_RECT_V);
}

/* short accent underline / divider */
inline void accent_rule(HDC dc, int x, int y, int w, COLORREF c = kAccent,
                        int h = 2) {
    RECT r{x, y, x + w, y + h};
    FillRect(dc, &r, brush(c));
}

inline void text(HDC dc, const std::string& s, RECT rc, COLORREF color,
                 HFONT font, UINT flags = DT_LEFT | DT_VCENTER | DT_SINGLELINE |
                                         DT_END_ELLIPSIS | DT_NOPREFIX) {
    SetBkMode(dc, TRANSPARENT);
    SetTextColor(dc, color);
    HGDIOBJ of = SelectObject(dc, font);
    std::wstring w = widen(s);
    DrawTextW(dc, w.c_str(), (int)w.size(), &rc, flags);
    SelectObject(dc, of);
}

/* Control props */
constexpr wchar_t kPropMuted[]  = L"fsui.muted";
constexpr wchar_t kPropAccent[] = L"fsui.accent";
constexpr wchar_t kPropCyan[]   = L"fsui.cyan";
constexpr wchar_t kPropHot[]    = L"fsui.hot";
constexpr wchar_t kPropOnCard[] = L"fsui.oncard";

inline void mark_muted(HWND h)  { SetPropW(h, kPropMuted,  (HANDLE)1); }
inline void mark_accent(HWND h) { SetPropW(h, kPropAccent, (HANDLE)1); }
inline void mark_cyan(HWND h)   { SetPropW(h, kPropCyan,   (HANDLE)1); }
inline bool is_muted(HWND h)  { return GetPropW(h, kPropMuted)  != nullptr; }
inline bool is_accent(HWND h) { return GetPropW(h, kPropAccent) != nullptr; }
inline bool is_cyan(HWND h)   { return GetPropW(h, kPropCyan)   != nullptr; }

/* Win10/11 dark chrome (scrollbars, edit borders, dropdowns). */
inline void dark_chrome(HWND h) {
    SetWindowTheme(h, L"DarkMode_Explorer", nullptr);
}

} // namespace theme
} // namespace fsui
#endif /* GPTBRIDGE_FSUI_THEME_H */

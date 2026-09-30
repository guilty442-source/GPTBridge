/* widgets.h — modern-drawn controls for the file-sorter window.
 *
 * FsuiSlider:   TBM_SETRANGE/SETPOS/GETPOS compat; posts WM_HSCROLL
 *               (SB_THUMBTRACK, pos in HIWORD) to its parent like a
 *               real trackbar — handle_hscroll needs no changes.
 * FsuiProgress: PBM_SETPOS compat; rounded fill bar.
 * FsuiScroll:   thin overlay vertical scrollbar driving the parent
 *               via WM_VSCROLL (SB_LINEUP/PAGEUP/THUMBTRACK).
 * buttons/checks stay real BUTTONs with BS_OWNERDRAW — drawn by the
 * content window's WM_DRAWITEM; install_hover() adds hover tracking. */
#ifndef GPTBRIDGE_FSUI_WIDGETS_H
#define GPTBRIDGE_FSUI_WIDGETS_H

#include <windows.h>

namespace fsui {
namespace widgets {

constexpr wchar_t kClsSlider[]   = L"FsuiSlider";
constexpr wchar_t kClsProgress[] = L"FsuiProgress";
constexpr wchar_t kClsScroll[]   = L"FsuiScroll";

/* one-time class registration */
void register_classes(HINSTANCE inst);

/* hover-state subclass for owner-drawn buttons/checks */
void install_hover(HWND h);

/* hot-item tracking for owner-drawn listboxes — stores the hovered
 * row index (1-based prop) so WM_DRAWITEM can highlight it. */
void install_list_hover(HWND h);
int list_hot_item(HWND h); /* -1 when none */

/* FsuiScroll API (direct calls, not messages) */
void scroll_set(HWND scroll, int max, int page);
void scroll_set_pos(HWND scroll, int pos);

HWND create_slider(HWND parent, int id, int x, int y, int w,
                   int lo, int hi, int pos);
HWND create_progress(HWND parent, int id, int x, int y, int w, int h);
HWND create_scroll(HWND parent, int x, int y, int w, int h);

} // namespace widgets
} // namespace fsui
#endif /* GPTBRIDGE_FSUI_WIDGETS_H */

//! fonts — UI font bootstrap for the egui surfaces.
//!
//! egui's bundled fonts (Ubuntu-Light + NotoEmoji) carry no CJK
//! glyphs, so every zh-TW string renders as tofu without a fallback.
//! We install the smoothest available zh-TW UI face (Noto Sans TC —
//! open curves, rounded terminals — with JhengHei Bold behind it:
//! heavier strokes read rounder at small sizes) plus Segoe UI for
//! Latin, which is visibly rounder than bundled Ubuntu-Light.

/// zh-TW CJK candidates, in priority order (roundest first).
const CJK_CANDIDATES: [&str; 6] = [
    "NotoSansTC-VF.ttf", // Noto Sans TC — rounded terminals, UI-grade
    "msjhbd.ttc",        // JhengHei Bold — heavier = smoother at small sizes
    "msjh.ttc",          // JhengHei (zh-TW UI default)
    "msyh.ttc",          // Microsoft YaHei (zh-CN)
    "mingliu.ttc",       // MingLiU (zh-TW serif)
    "kaiu.ttf",          // DFKai-SB (zh-TW)
];

/// Latin candidates — rounded sans preferred over egui's Ubuntu-Light.
const LATIN_CANDIDATES: [&str; 3] = [
    "segoeui.ttf", // Segoe UI — Windows-native humanist sans
    "arial.ttf",
    "calibri.ttf",
];

fn fonts_dir() -> String {
    std::env::var("SystemRoot")
        .map(|root| format!(r"{root}\Fonts"))
        .unwrap_or_else(|_| r"C:\Windows\Fonts".to_string())
}

fn first_available(candidates: &[&str]) -> Option<Vec<u8>> {
    let dir = fonts_dir();
    for name in candidates {
        if let Ok(bytes) = std::fs::read(format!(r"{dir}\{name}")) {
            return Some(bytes);
        }
    }
    None
}

/// Install rounded UI faces: Segoe UI for Latin + Noto Sans TC (or a
/// JhengHei fallback) for CJK, keeping the bundled faces as tail
/// fallbacks.  CJK absence is fail-visible (UI stays Latin-legible).
pub(crate) fn install_ui_fonts(ctx: &egui::Context) {
    let mut fonts = egui::FontDefinitions::default();
    let latin = first_available(&LATIN_CANDIDATES);
    let cjk = first_available(&CJK_CANDIDATES);

    if let Some(bytes) = latin {
        fonts.font_data.insert(
            "round-latin".into(),
            std::sync::Arc::new(egui::FontData::from_owned(bytes)),
        );
        fonts
            .families
            .entry(egui::FontFamily::Proportional)
            .or_default()
            .insert(0, "round-latin".into());
    }
    match cjk {
        Some(bytes) => {
            fonts.font_data.insert(
                "cjk-ui".into(),
                std::sync::Arc::new(egui::FontData::from_owned(bytes)),
            );
            for family in [egui::FontFamily::Proportional, egui::FontFamily::Monospace]
            {
                fonts
                    .families
                    .entry(family)
                    .or_default()
                    .push("cjk-ui".into());
            }
        }
        None => {
            eprintln!("UI_FONT_MISSING: no CJK font found under %SystemRoot%\\Fonts");
        }
    }
    ctx.set_fonts(fonts);
}

//! fonts — UI font bootstrap for the egui surfaces.
//!
//! egui's bundled fonts (Ubuntu-Light + NotoEmoji) carry no CJK
//! glyphs, so every zh-TW string renders as tofu.  We append the
//! first available Windows CJK UI font to the proportional and
//! monospace families as a *fallback* — Latin/digit glyphs keep the
//! bundled typeface, CJK codepoints fall through to the system font.

/// zh-TW-preferred Windows UI fonts, in priority order.
const CJK_CANDIDATES: [&str; 5] = [
    "msjh.ttc",   // Microsoft JhengHei (zh-TW UI default)
    "msyh.ttc",   // Microsoft YaHei (zh-CN)
    "mingliu.ttc", // MingLiU (zh-TW serif)
    "simsun.ttc", // SimSun (zh-CN serif)
    "kaiu.ttf",   // DFKai-SB (zh-TW, ships on zh-TW installs)
];

fn cjk_font_bytes() -> Option<Vec<u8>> {
    let fonts_dir = std::env::var("SystemRoot")
        .map(|root| format!(r"{root}\Fonts"))
        .unwrap_or_else(|_| r"C:\Windows\Fonts".to_string());
    for name in CJK_CANDIDATES {
        let path = format!(r"{fonts_dir}\{name}");
        if let Ok(bytes) = std::fs::read(&path) {
            return Some(bytes);
        }
    }
    None
}

/// Append a CJK-capable system font to every egui font family.
/// No-op when no candidate exists on disk (fail-visible: UI stays
/// legible in Latin and logs the absence).
pub(crate) fn install_ui_fonts(ctx: &egui::Context) {
    let Some(bytes) = cjk_font_bytes() else {
        eprintln!("UI_FONT_MISSING: no CJK font found under %SystemRoot%\\Fonts");
        return;
    };
    let mut fonts = egui::FontDefinitions::default();
    fonts
        .font_data
        .insert(
            "cjk-ui".into(),
            std::sync::Arc::new(egui::FontData::from_owned(bytes)),
        );
    for family in [egui::FontFamily::Proportional, egui::FontFamily::Monospace] {
        fonts
            .families
            .entry(family)
            .or_default()
            .push("cjk-ui".into());
    }
    ctx.set_fonts(fonts);
}

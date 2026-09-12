from __future__ import annotations

from typing import Any


def build_smart_mapping_repair(preview: dict[str, Any]) -> dict[str, Any]:
    """Select a deterministic Excel layout repair without invoking an AI model."""

    consolidated = (
        preview.get("consolidated_layout")
        if isinstance(preview.get("consolidated_layout"), dict)
        else {}
    )
    horizontal = (
        preview.get("horizontal_layout")
        if isinstance(preview.get("horizontal_layout"), dict)
        else {}
    )
    if consolidated.get("detected"):
        count = int(consolidated.get("holding_count") or 0)
        return {
            "recommended": True,
            "layout": "consolidated_report",
            "sheet_name": str(consolidated.get("sheet_name") or ""),
            "confidence_score": 99 if count > 20 else 92,
            "confidence_label": "高",
            "reason": f"同一工作表同時辨識到基金與證券區塊，共 {count} 筆。",
            "changes": ["使用雙區塊報酬表解析器", "保留基金、台股與美股各自欄位"],
        }
    if horizontal.get("detected"):
        count = int(horizontal.get("holding_count") or 0)
        return {
            "recommended": True,
            "layout": "horizontal_matrix",
            "sheet_name": "",
            "confidence_score": 96 if count > 10 else 86,
            "confidence_label": "高" if count > 10 else "中",
            "reason": f"辨識到 {horizontal.get('sheet_count', 0)} 張橫向持股表，共 {count} 筆。",
            "changes": ["依工作表資產類別分組", "自動推定名稱、價格與數量列"],
        }

    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for sheet in preview.get("sheets", []):
        if not isinstance(sheet, dict):
            continue
        for candidate in sheet.get("header_candidates", []):
            if isinstance(candidate, dict):
                candidates.append((sheet, candidate))
    candidates.sort(key=lambda pair: int(pair[1].get("score") or 0), reverse=True)
    if not candidates:
        return {
            "recommended": False,
            "layout": "row_mapping",
            "confidence_score": 0,
            "confidence_label": "低",
            "reason": "未找到可驗證的表頭候選，請人工指定欄位。",
            "changes": [],
        }
    sheet, best = candidates[0]
    runner_up = int(candidates[1][1].get("score") or 0) if len(candidates) > 1 else 0
    score = int(best.get("score") or 0)
    gap = max(0, score - runner_up)
    valid_rows = int(best.get("valid_data_row_count") or 0)
    confidence = min(98, 55 + min(25, valid_rows) + min(18, gap // 20))
    canonical = best.get("canonical_columns") or []
    mapping = {
        str(field): index
        for index, field in enumerate(canonical)
        if str(field or "").strip()
    }
    return {
        "recommended": bool({"symbol", "quantity"}.issubset(mapping)),
        "layout": "row_mapping",
        "sheet_name": str(sheet.get("sheet_name") or ""),
        "header_row_number": best.get("header_row_number"),
        "data_start_row_number": best.get("data_start_row_number"),
        "column_mapping": mapping,
        "confidence_score": confidence,
        "confidence_label": "高" if confidence >= 85 else "中" if confidence >= 65 else "低",
        "reason": f"最佳表頭包含 {valid_rows} 筆有效持股，與次佳候選分差 {gap}。",
        "changes": ["重新選擇最可信表頭", "依欄位內容補上缺少的欄名"],
    }

"""Low-level XLSX (OOXML) parsing: shared strings, cells, columns, sheet entries."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from .portfolio_snapshot import _open_xlsx_workbook_unlocked


def xlsx_column_letter(index: int) -> str:
    if index < 0:
        return ""
    letters = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def xlsx_rows(path: Path) -> list[list[str]]:
    namespace = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with _open_xlsx_workbook_unlocked(path) as workbook:
        shared_strings = read_xlsx_shared_strings(workbook, namespace)
        _sheet_title, sheet_name = xlsx_sheet_entries(workbook)[0]
        sheet_root = ET.fromstring(workbook.read(sheet_name))

    return xlsx_rows_from_root(sheet_root, shared_strings, namespace)


def xlsx_rows_from_root(
    sheet_root: ET.Element,
    shared_strings: list[str],
    namespace: dict[str, str],
) -> list[list[str]]:
    output: list[list[str]] = []
    for row in sheet_root.findall(".//m:sheetData/m:row", namespace):
        try:
            row_number = int(str(row.attrib.get("r", "")))
        except ValueError:
            row_number = len(output) + 1
        while len(output) < max(0, row_number - 1):
            output.append([])
        values: list[str] = []
        for cell in row.findall("m:c", namespace):
            index = xlsx_column_index(str(cell.attrib.get("r", "")))
            while len(values) < index:
                values.append("")
            values.append(read_xlsx_cell(cell, shared_strings, namespace))
        if row_number <= 0:
            output.append(values)
        elif len(output) == row_number - 1:
            output.append(values)
        else:
            output[row_number - 1] = values
    apply_xlsx_merged_cells(output, sheet_root, namespace)
    return output


def apply_xlsx_merged_cells(
    rows: list[list[str]],
    sheet_root: ET.Element,
    namespace: dict[str, str],
) -> None:
    for merged_cell in sheet_root.findall(".//m:mergeCells/m:mergeCell", namespace):
        reference = str(merged_cell.attrib.get("ref", ""))
        coordinates = xlsx_range_coordinates(reference)
        if coordinates is None:
            continue
        start_row, start_column, end_row, end_column = coordinates
        while len(rows) <= start_row:
            rows.append([])
        while len(rows[start_row]) <= start_column:
            rows[start_row].append("")
        value = rows[start_row][start_column]
        if not value:
            continue
        for row_index in range(start_row, end_row + 1):
            while len(rows) <= row_index:
                rows.append([])
            for column_index in range(start_column, end_column + 1):
                while len(rows[row_index]) <= column_index:
                    rows[row_index].append("")
                if not rows[row_index][column_index]:
                    rows[row_index][column_index] = value


def xlsx_sheet_entries(workbook: zipfile.ZipFile) -> list[tuple[str, str]]:
    names = set(workbook.namelist())
    if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
        namespace = {
            "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
        }
        workbook_root = ET.fromstring(workbook.read("xl/workbook.xml"))
        rels_root = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        relationships = {
            str(rel.attrib.get("Id", "")): normalize_xlsx_relationship_target(
                "xl",
                str(rel.attrib.get("Target", "")),
            )
            for rel in rels_root.findall("rel:Relationship", namespace)
        }
        entries: list[tuple[str, str]] = []
        for index, sheet in enumerate(workbook_root.findall(".//m:sheets/m:sheet", namespace), start=1):
            sheet_name = str(sheet.attrib.get("name") or f"Sheet{index}")
            relation_id = str(
                sheet.attrib.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
                    "",
                )
            )
            sheet_path = relationships.get(relation_id, "")
            if sheet_path in names:
                entries.append((sheet_name, sheet_path))
        if entries:
            return entries

    fallback = sorted(
        (
            name
            for name in names
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        ),
        key=natural_sort_key,
    )
    if not fallback:
        raise KeyError("xl/worksheets/sheet1.xml")
    return [(Path(name).stem, name) for name in fallback]


def normalize_xlsx_relationship_target(base_dir: str, target: str) -> str:
    raw = target.replace("\\", "/").strip()
    if raw.startswith("/"):
        path = PurePosixPath(raw.lstrip("/"))
    else:
        path = PurePosixPath(base_dir) / raw
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def natural_sort_key(value: str) -> list[Any]:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    ]


def first_xlsx_sheet_path(workbook: zipfile.ZipFile) -> str:
    return xlsx_sheet_entries(workbook)[0][1]


def read_xlsx_shared_strings(
    workbook: zipfile.ZipFile,
    namespace: dict[str, str],
) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall("m:si", namespace):
        parts = [
            text.text or ""
            for text in item.findall(".//m:t", namespace)
        ]
        strings.append("".join(parts))
    return strings


def xlsx_column_index(reference: str) -> int:
    letters = re.sub(r"[^A-Za-z]", "", reference).upper()
    if not letters:
        return 0
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return max(0, index - 1)


def xlsx_cell_coordinates(reference: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\$?([A-Za-z]+)\$?(\d+)", str(reference).strip())
    if not match:
        return None
    row_number = int(match.group(2))
    if row_number <= 0:
        return None
    return row_number - 1, xlsx_column_index(match.group(1))


def xlsx_range_coordinates(reference: str) -> tuple[int, int, int, int] | None:
    parts = [part.strip() for part in str(reference).split(":") if part.strip()]
    if not parts:
        return None
    start = xlsx_cell_coordinates(parts[0])
    end = xlsx_cell_coordinates(parts[-1])
    if start is None or end is None:
        return None
    start_row, start_column = start
    end_row, end_column = end
    return (
        min(start_row, end_row),
        min(start_column, end_column),
        max(start_row, end_row),
        max(start_column, end_column),
    )


def read_xlsx_cell(
    cell: ET.Element,
    shared_strings: list[str],
    namespace: dict[str, str],
) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(
            text.text or ""
            for text in cell.findall(".//m:t", namespace)
        ).strip()

    value = cell.find("m:v", namespace)
    text = "" if value is None or value.text is None else value.text
    if cell_type == "s":
        try:
            return shared_strings[int(text)].strip()
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "TRUE" if text == "1" else "FALSE"
    return text.strip()

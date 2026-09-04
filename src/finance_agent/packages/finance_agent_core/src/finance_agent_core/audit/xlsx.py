from __future__ import annotations

import datetime as dt
import posixpath
import re
import zipfile
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _tag(name: str) -> str:
    return f"{{{NS_MAIN}}}{name}"


def column_to_index(column: str) -> int:
    index = 0
    if not column:
        raise ValueError("Excel column cannot be empty")
    for character in column.upper():
        if character < "A" or character > "Z":
            raise ValueError(f"Invalid Excel column: {column}")
        index = index * 26 + ord(character) - 64
    return index - 1


def index_to_column(index: int) -> str:
    if index < 0:
        raise ValueError("Excel column index cannot be negative")
    value = index + 1
    column = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        column = chr(65 + remainder) + column
    return column


class XlsxFormatError(ValueError):
    """Raised when a workbook does not contain the expected XLSX parts."""


class XlsxStream:
    """Small, dependency-free, read-only XLSX row stream."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._archive = zipfile.ZipFile(self.path)
        try:
            self._shared_strings = self._load_shared_strings()
            workbook = ET.fromstring(self._archive.read("xl/workbook.xml"))
            self._date_1904 = self._uses_1904_date_system(workbook)
            self.sheets = self._load_sheets(workbook)
            self._date_style_ids = self._load_date_style_ids()
        except (KeyError, ET.ParseError) as error:
            self._archive.close()
            raise XlsxFormatError(f"Invalid XLSX workbook: {self.path}") from error

    def __enter__(self) -> XlsxStream:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._archive.close()

    def _load_shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self._archive.namelist():
            return []
        root = ET.fromstring(self._archive.read("xl/sharedStrings.xml"))
        return [
            "".join(text.text or "" for text in item.iter(_tag("t")))
            for item in root.findall(_tag("si"))
        ]

    @staticmethod
    def _uses_1904_date_system(workbook: ET.Element) -> bool:
        properties = workbook.find(_tag("workbookPr"))
        if properties is None:
            return False
        return properties.attrib.get("date1904", "").lower() in {"1", "true"}

    def _load_sheets(self, workbook: ET.Element) -> list[tuple[str, str]]:
        relations = ET.fromstring(self._archive.read("xl/_rels/workbook.xml.rels"))
        relation_map = {relation.attrib["Id"]: relation.attrib["Target"] for relation in relations}
        sheets_element = workbook.find(_tag("sheets"))
        if sheets_element is None:
            raise XlsxFormatError(f"Workbook has no sheets: {self.path}")

        sheets: list[tuple[str, str]] = []
        for sheet in sheets_element:
            relation_id = sheet.attrib[f"{{{NS_REL}}}id"]
            target = relation_map[relation_id]
            if target.startswith("/"):
                archive_target = target.lstrip("/")
            else:
                archive_target = posixpath.normpath(posixpath.join("xl", target))
            sheets.append((sheet.attrib["name"], archive_target))
        return sheets

    def _load_date_style_ids(self) -> set[int]:
        if "xl/styles.xml" not in self._archive.namelist():
            return set()

        root = ET.fromstring(self._archive.read("xl/styles.xml"))
        built_in_date_ids = set(range(14, 23)) | {27, 30, 36, 45, 46, 47, 50, 57}
        custom_formats: dict[int, str] = {}
        number_formats = root.find(_tag("numFmts"))
        if number_formats is not None:
            for number_format in number_formats:
                custom_formats[int(number_format.attrib["numFmtId"])] = number_format.attrib.get(
                    "formatCode", ""
                )

        date_style_ids: set[int] = set()
        cell_formats = root.find(_tag("cellXfs"))
        if cell_formats is None:
            return date_style_ids
        for style_id, cell_format in enumerate(cell_formats):
            number_format_id = int(cell_format.attrib.get("numFmtId", "0"))
            format_code = custom_formats.get(number_format_id, "")
            looks_like_date = bool(re.search(r"(?<!\[)[ymdhis]", format_code.lower()))
            if number_format_id in built_in_date_ids or looks_like_date:
                date_style_ids.add(style_id)
        return date_style_ids

    def sheet_info(self) -> list[dict[str, str | None]]:
        information: list[dict[str, str | None]] = []
        for name, target in self.sheets:
            root = ET.fromstring(self._archive.read(target))
            dimension = root.find(_tag("dimension"))
            information.append(
                {
                    "name": name,
                    "target": target,
                    "dimension": dimension.attrib.get("ref") if dimension is not None else None,
                }
            )
        return information

    def iter_rows(self, sheet_index: int = 0) -> Iterator[tuple[int, dict[int, Any]]]:
        try:
            _, target = self.sheets[sheet_index]
        except IndexError as error:
            raise IndexError(f"Workbook has no sheet index {sheet_index}: {self.path}") from error

        with self._archive.open(target) as worksheet:
            for _, element in ET.iterparse(worksheet, events=("end",)):
                if element.tag != _tag("row"):
                    continue
                row_number = int(element.attrib.get("r", "0"))
                values: dict[int, Any] = {}
                for cell in element.findall(_tag("c")):
                    reference = cell.attrib.get("r", "")
                    match = re.match(r"([A-Z]+)", reference)
                    if match is None:
                        continue
                    column_index = column_to_index(match.group(1))
                    values[column_index] = self._parse_cell(cell)
                yield row_number, values
                element.clear()

    def _parse_cell(self, cell: ET.Element) -> Any:
        cell_type = cell.attrib.get("t")
        style_id = int(cell.attrib.get("s", "0"))
        value_element = cell.find(_tag("v"))
        inline_string = cell.find(_tag("is"))

        if cell_type == "inlineStr" and inline_string is not None:
            return "".join(text.text or "" for text in inline_string.iter(_tag("t")))
        if value_element is None:
            return None

        raw_value = value_element.text or ""
        if cell_type == "s":
            try:
                return self._shared_strings[int(raw_value)]
            except (IndexError, ValueError):
                return raw_value
        if cell_type == "b":
            return raw_value == "1"
        if cell_type in {"str", "e"}:
            return raw_value

        try:
            number = Decimal(raw_value)
        except InvalidOperation:
            return raw_value

        if style_id in self._date_style_ids:
            epoch = dt.datetime(1904, 1, 1) if self._date_1904 else dt.datetime(1899, 12, 30)
            return epoch + dt.timedelta(days=float(number))
        if number == number.to_integral_value():
            return int(number)
        return number

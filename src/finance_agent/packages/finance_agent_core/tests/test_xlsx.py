from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from finance_agent_core.audit.xlsx import XlsxStream, column_to_index, index_to_column


def _write_minimal_xlsx(path: Path) -> None:
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    relations = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
   Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
   Target="worksheets/sheet1.xml"/>
</Relationships>"""
    worksheet = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:B3"/>
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>name</t></is></c>
      <c r="B1" t="inlineStr"><is><t>value</t></is></c>
    </row>
    <row r="2">
      <c r="A2" t="inlineStr"><is><t>alpha</t></is></c>
      <c r="B2"><v>1</v></c>
    </row>
    <row r="3">
      <c r="A3" t="inlineStr"><is><t>beta</t></is></c>
      <c r="B3"><v>1.25</v></c>
    </row>
  </sheetData>
</worksheet>"""
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relations)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)


def test_excel_column_conversion_round_trip() -> None:
    for index in (0, 25, 26, 51, 52, 702):
        assert column_to_index(index_to_column(index)) == index


def test_xlsx_stream_reads_inline_strings_and_numbers(tmp_path: Path) -> None:
    path = tmp_path / "fixture.xlsx"
    _write_minimal_xlsx(path)

    with XlsxStream(path) as workbook:
        rows = list(workbook.iter_rows())
        information = workbook.sheet_info()

    assert information[0]["dimension"] == "A1:B3"
    assert rows[0] == (1, {0: "name", 1: "value"})
    assert rows[1] == (2, {0: "alpha", 1: 1})
    assert str(rows[2][1][1]) == "1.25"

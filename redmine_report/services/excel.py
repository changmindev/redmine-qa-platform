"""테스트케이스/체크리스트(+실행결과)를 xlsx 로 내보낸다.

결과 열에는 P/F 드롭다운(데이터 유효성)과 Pass/Fail/Blocked 조건부 서식을 넣어
엑셀에서 그대로 수행 결과를 기록할 수 있게 한다.
openpyxl 이 없으면 ExcelUnavailable 을 던지고 API 계층이 안내 메시지를 돌려준다.
"""

from __future__ import annotations

import io

try:
    import openpyxl
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    HAS_XLSX = True
except ImportError:
    HAS_XLSX = False


class ExcelUnavailable(RuntimeError):
    """openpyxl 미설치."""


STATUS_LABEL = {
    "": "미시행",
    "pass": "Pass",
    "fail": "Fail",
    "na": "N/A",
    "blocked": "Blocked",
}

_CHECKLIST_HEADERS = ["이슈ID", "제목", "분류", "항목", "근거", "결과", "실제결과", "결함"]
_CHECKLIST_WIDTHS = [9, 34, 8, 52, 40, 10, 34, 9]

_FULL_HEADERS = [
    "이슈ID", "제목", "No", "우선순위", "1Depth", "2Depth", "3Depth", "4Depth",
    "Type", "테스트목적", "사전조건", "테스트절차", "기대결과", "근거", "결과", "실제결과", "결함",
]
_FULL_WIDTHS = [9, 28, 5, 8, 11, 11, 11, 11, 9, 30, 26, 34, 30, 40, 10, 30, 9]

_RESULT_FILLS = (
    ("Pass", "C6F6D5", "1A7F37"),
    ("Fail", "FED7D7", "C53030"),
    ("Blocked", "FEEBC8", "C05621"),
)


def _evidence(row: dict) -> str:
    """근거 원문. 비어 있으면 '검수 필요' 로 표시해 필터링할 수 있게 한다."""
    text = str((row or {}).get("evidence") or "").strip()
    return text if text else "⚠ 근거 없음 (검수 필요)"


def _defect_ref(row: dict) -> str:
    defect = (row or {}).get("defect")
    return f"#{defect.get('id')}" if isinstance(defect, dict) and defect.get("id") else ""


#: 수식으로 해석될 수 있는 선행 문자 — Redmine 이슈 제목이나 코멘트에 이런 문자로
#: 시작하는 값이 들어오면, openpyxl 이 셀 타입을 자동으로 '수식'(f)으로 잡아
#: 파일을 열 때 그대로 실행된다(CSV/Excel Formula Injection). 텍스트로 강제한다.
_FORMULA_TRIGGERS = ("=", "+", "-", "@")


def _safe(value):
    if isinstance(value, str) and value[:1] in _FORMULA_TRIGGERS:
        return "'" + value
    return value


def build_workbook(cases: list[dict], mode: str = "full") -> io.BytesIO:
    """세트(cases)를 xlsx 바이트 스트림으로 변환한다."""
    if not HAS_XLSX:
        raise ExcelUnavailable("openpyxl 미설치. 'pip install openpyxl' 후 다시 시도하세요.")

    checklist = mode == "checklist"
    headers = _CHECKLIST_HEADERS if checklist else _FULL_HEADERS
    widths = _CHECKLIST_WIDTHS if checklist else _FULL_WIDTHS

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "체크리스트" if checklist else "테스트케이스"
    ws.append(headers)

    header_fill = PatternFill("solid", fgColor="4F46E5")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", horizontal="center")

    for card in cases or []:
        result = (card or {}).get("result") or {}
        if checklist:
            for item in result.get("checklist") or []:
                ws.append([_safe(v) for v in (
                    card.get("id"), card.get("subject"),
                    item.get("category", ""), item.get("text", ""),
                    _evidence(item),
                    STATUS_LABEL.get(item.get("status", ""), ""),
                    item.get("actual", ""), _defect_ref(item),
                )])
        else:
            for idx, case in enumerate(result.get("cases") or [], start=1):
                ws.append([_safe(v) for v in (
                    card.get("id"), card.get("subject"), idx, case.get("priority", ""),
                    case.get("depth1", ""), case.get("depth2", ""),
                    case.get("depth3", ""), case.get("depth4", ""),
                    case.get("type", ""), case.get("objective", ""),
                    case.get("precondition", ""), case.get("procedure", ""),
                    case.get("expected", ""), _evidence(case),
                    STATUS_LABEL.get(case.get("status", ""), ""),
                    case.get("actual", ""), _defect_ref(case),
                )])

    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = "A2"

    wrap = Alignment(vertical="top", wrap_text=True)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = wrap

    result_col = get_column_letter(headers.index("결과") + 1)
    last_row = max(ws.max_row, 2)
    cell_range = f"{result_col}2:{result_col}{last_row}"

    validation = DataValidation(
        type="list", formula1='"미시행,Pass,Fail,N/A,Blocked"', allow_blank=True
    )
    ws.add_data_validation(validation)
    validation.add(cell_range)

    for label, bg, fg in _RESULT_FILLS:
        ws.conditional_formatting.add(
            cell_range,
            CellIsRule(
                operator="equal",
                formula=[f'"{label}"'],
                fill=PatternFill("solid", fgColor=bg),
                font=Font(color=fg, bold=True),
            ),
        )

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream

"""테스트케이스 변경 비교(diff).

이슈가 수정되면 테스트케이스도 낡는다. 다시 생성했을 때 "뭐가 달라졌는지"를
사람이 눈으로 대조하는 건 현실적으로 불가능하므로 여기서 계산한다.

케이스에는 안정적인 ID가 없다(모델이 매번 새로 만든다). 그래서 내용에서
**시그니처**를 뽑아 같은 케이스인지 판단한다.
  · 테스트케이스 : 기능 분류(depth1~4) + 테스트 목적
  · 체크리스트   : 항목 문장
같은 시그니처인데 다른 필드가 바뀌었으면 '변경', 한쪽에만 있으면 '추가/삭제'다.
"""

from __future__ import annotations

import re
import unicodedata

#: 변경 여부를 비교할 필드 (실행 결과 관련 필드는 제외 — 내용 변경이 아니다)
COMPARE_FIELDS = (
    "priority", "type", "depth1", "depth2", "depth3", "depth4",
    "objective", "precondition", "procedure", "expected", "category", "text",
)

_WS = re.compile(r"\s+")


def normalize(value) -> str:
    """공백·유니코드 표기 차이를 없앤 비교용 문자열."""
    text = unicodedata.normalize("NFKC", str(value if value is not None else ""))
    return _WS.sub(" ", text).strip().lower()


def signature(row: dict) -> str:
    """같은 케이스인지 판별하는 키."""
    if row.get("text") is not None and row.get("objective") is None:
        return "cl:" + normalize(row.get("text"))
    parts = [normalize(row.get(f)) for f in ("depth1", "depth2", "depth3", "depth4")]
    parts.append(normalize(row.get("objective")))
    return "tc:" + "|".join(parts)


def label_of(row: dict) -> str:
    """사람이 읽을 한 줄 이름."""
    if row.get("objective"):
        path = " > ".join(p for p in (row.get(f) for f in ("depth1", "depth2", "depth3", "depth4")) if p)
        return f"{path} — {row['objective']}" if path else str(row["objective"])
    return str(row.get("text") or "(내용 없음)")


def _changed_fields(before: dict, after: dict) -> dict[str, dict[str, str]]:
    changes = {}
    for field in COMPARE_FIELDS:
        old, new = before.get(field), after.get(field)
        if old is None and new is None:
            continue
        if normalize(old) != normalize(new):
            changes[field] = {"before": old or "", "after": new or ""}
    return changes


def diff_rows(before: list[dict], after: list[dict]) -> dict:
    """항목 목록 두 개를 비교한다."""
    before_map: dict[str, dict] = {}
    for row in before or []:
        before_map.setdefault(signature(row), row)
    after_map: dict[str, dict] = {}
    for row in after or []:
        after_map.setdefault(signature(row), row)

    added = [
        {"signature": sig, "label": label_of(row), "row": row}
        for sig, row in after_map.items()
        if sig not in before_map
    ]
    removed = [
        {"signature": sig, "label": label_of(row), "row": row}
        for sig, row in before_map.items()
        if sig not in after_map
    ]

    changed = []
    unchanged = 0
    for sig, new_row in after_map.items():
        old_row = before_map.get(sig)
        if old_row is None:
            continue
        fields = _changed_fields(old_row, new_row)
        if fields:
            changed.append({"signature": sig, "label": label_of(new_row), "fields": fields})
        else:
            unchanged += 1

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": unchanged,
            "has_change": bool(added or removed or changed),
        },
    }


def _rows_of_card(card: dict) -> list[dict]:
    result = (card or {}).get("result") or {}
    rows = result.get("cases")
    if not isinstance(rows, list):
        rows = result.get("checklist") or []
    return [r for r in rows if isinstance(r, dict)]


def diff_sets(before_cases: list[dict], after_cases: list[dict]) -> dict:
    """세트(이슈 카드 여러 개) 단위 비교. 이슈별로 나누어 결과를 낸다."""
    before_by_issue = {str(c.get("id")): _rows_of_card(c) for c in before_cases or []}
    after_by_issue = {str(c.get("id")): _rows_of_card(c) for c in after_cases or []}
    subjects = {str(c.get("id")): c.get("subject", "") for c in (after_cases or []) + (before_cases or [])}

    issues = []
    totals = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}

    for issue_id in sorted(set(before_by_issue) | set(after_by_issue)):
        if issue_id not in before_by_issue:
            kind = "issue_added"
        elif issue_id not in after_by_issue:
            kind = "issue_removed"
        else:
            kind = "issue_kept"
        result = diff_rows(before_by_issue.get(issue_id, []), after_by_issue.get(issue_id, []))
        for key in totals:
            totals[key] += result["summary"][key]
        issues.append(
            {
                "issue_id": issue_id,
                "subject": subjects.get(issue_id, ""),
                "kind": kind,
                **result,
            }
        )

    totals["has_change"] = bool(totals["added"] or totals["removed"] or totals["changed"])
    return {"issues": issues, "summary": totals}

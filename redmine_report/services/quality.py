"""릴리스 품질 지표 — Redmine 이슈와 로컬 테스트 기록을 합쳐서 판단 근거를 만든다.

이 모듈이 답하는 질문은 둘이다.
  1. "이 버전, 지금 출시해도 되나?"        → release_readiness()
  2. "이 요구사항, 테스트는 한 거야?"       → traceability()

Redmine 만으로는 1번을 답할 수 없다(테스트 수행 결과가 Redmine 에 없으므로).
로컬 진행 기록만으로도 답할 수 없다(미해결 결함 현황이 Redmine 에 있으므로).
그래서 두 소스를 여기서 합친다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from ..config import settings
from . import redmine, storage
from .timeutil import local_date, parse_dt

log = logging.getLogger(__name__)

#: 출시를 막아야 할 결함 우선순위
BLOCKER_PRIORITIES = ("즉시", "긴급")

#: 실행 결과 코드 → 사람이 읽는 이름
RESULT_LABELS = {
    "pass": "Pass",
    "fail": "Fail",
    "blocked": "Blocked",
    "na": "N/A",
    "untested": "미시행",
}

_EMPTY_COUNTS = {k: 0 for k in RESULT_LABELS}


def _rows_of(card: dict) -> list[dict]:
    """카드(이슈 1건) 안의 테스트 항목들. 테스트케이스든 체크리스트든 동일하게 다룬다."""
    result = (card or {}).get("result") or {}
    rows = result.get("cases")
    if not isinstance(rows, list):
        rows = result.get("checklist") or []
    return [r for r in rows if isinstance(r, dict)]


def _status_of(row: dict) -> str:
    return row.get("status") or "untested"


def _runs_for(project_id: str | None, version_id: str | None) -> list[dict]:
    """이 프로젝트(+버전)의 진행 기록 전체를 읽어온다.

    같은 버전을 여러 회차 돌렸다면 전부 모은다. 회차별 중복은 아래에서 이슈 단위로 정리한다.
    """
    records = []
    for summary in storage.list_sets(project_id=project_id or "", set_type="run"):
        record = storage.read_set(summary["id"])
        if not record:
            continue
        meta = record.get("meta") or {}
        if version_id and str(meta.get("version") or "") != str(version_id):
            continue
        records.append(record)
    # 최근 회차가 앞에 오게 — 같은 이슈가 여러 회차에 있으면 최신 결과를 채택한다.
    # updated_at 은 분 단위라 같은 분에 저장된 두 회차를 구분하지 못한다.
    # 세트 id 끝에 생성 시각(ms)이 들어 있으므로 이를 2차 정렬키로 쓴다.
    records.sort(key=lambda r: (r.get("updated_at", ""), str(r.get("id", ""))), reverse=True)
    return records


def _latest_rows_by_issue(runs: Iterable[dict]) -> dict[str, list[dict]]:
    """이슈별로 '가장 최근 회차'의 항목들만 남긴다."""
    latest: dict[str, list[dict]] = {}
    for record in runs:  # 이미 최신순
        for card in record.get("cases") or []:
            key = str(card.get("id"))
            if key not in latest:
                latest[key] = _rows_of(card)
    return latest


def _count_statuses(rows: Iterable[dict]) -> dict[str, int]:
    counts = dict(_EMPTY_COUNTS)
    for row in rows:
        status = _status_of(row)
        counts[status] = counts.get(status, 0) + 1
    return counts


def _pass_rate(counts: dict[str, int]) -> float:
    """통과율 = Pass / (Pass + Fail).

    Blocked·N/A·미시행은 '판정된 것'이 아니므로 분모에서 뺀다.
    미시행을 분모에 넣으면 테스트를 안 할수록 통과율이 떨어져 진행률과 뒤섞인다.
    """
    judged = counts.get("pass", 0) + counts.get("fail", 0)
    return round(counts.get("pass", 0) / judged * 100, 1) if judged else 0.0


def _velocity(runs: Iterable[dict]) -> dict:
    """최근 실행 속도 → 남은 항목 소진 예상일.

    각 행의 executed_at(실행 시각)이 있어야 계산된다. 없으면 None 을 돌려준다.
    """
    per_day: dict[str, int] = {}
    for record in runs:
        for card in record.get("cases") or []:
            for row in _rows_of(card):
                stamp = parse_dt(row.get("executed_at"))
                if stamp:
                    day = local_date(stamp)
                    per_day[day] = per_day.get(day, 0) + 1
    if not per_day:
        return {"per_day": {}, "recent_avg": None}
    recent = [count for _, count in sorted(per_day.items())[-3:]]
    return {"per_day": dict(sorted(per_day.items())), "recent_avg": round(sum(recent) / len(recent), 1)}


def _defect_summary(issues: list[dict]) -> dict:
    """이 버전에 걸린 결함(버그 트래커) 현황."""
    open_names = settings.open_statuses
    by_priority: dict[str, int] = {}
    blockers: list[dict] = []
    total = open_count = 0

    for issue in issues:
        if (issue.get("tracker") or {}).get("name", "") not in settings.bug_trackers:
            continue
        total += 1
        status = (issue.get("status") or {}).get("name", "")
        if status not in open_names:
            continue
        open_count += 1
        priority = (issue.get("priority") or {}).get("name", "보통")
        by_priority[priority] = by_priority.get(priority, 0) + 1
        if priority in BLOCKER_PRIORITIES:
            blockers.append(
                {
                    "id": issue.get("id"),
                    "subject": issue.get("subject", ""),
                    "priority": priority,
                    "status": status,
                    "assignee": (issue.get("assigned_to") or {}).get("name", "미배정"),
                    "url": redmine.issue_url(issue.get("id")),
                }
            )
    return {
        "total": total,
        "open": open_count,
        "by_priority": by_priority,
        "blockers": blockers,
    }


def _gate(progress: float, pass_rate: float, defects: dict, untested_p0: int) -> dict:
    """출시 신호등 판정.

    임의의 점수를 매기지 않고, '무엇 때문에 막혔는지'를 이유 목록으로 남긴다.
    PM 이 화면을 보고 바로 액션을 정할 수 있어야 하기 때문이다.
    """
    blocking: list[str] = []
    warning: list[str] = []

    if defects["blockers"]:
        blocking.append(f"미해결 Blocker/Critical 결함 {len(defects['blockers'])}건")
    if untested_p0:
        blocking.append(f"미실행 P0 케이스 {untested_p0}건")
    if progress < 80:
        blocking.append(f"테스트 진행률 {progress}% (기준 80%)")

    if 80 <= progress < 95:
        warning.append(f"테스트 진행률 {progress}% — 잔여분 확인 필요")
    if 0 < pass_rate < 95:
        warning.append(f"통과율 {pass_rate}% — 실패 케이스 확인 필요")
    if defects["open"] and not defects["blockers"]:
        warning.append(f"미해결 결함 {defects['open']}건 (Blocker 없음)")

    if blocking:
        return {"level": "blocked", "label": "출시 불가", "reasons": blocking, "warnings": warning}
    if warning:
        return {"level": "caution", "label": "주의", "reasons": [], "warnings": warning}
    return {"level": "ready", "label": "출시 가능", "reasons": [], "warnings": []}


def release_readiness(project_id: str | None, version_id: str) -> dict:
    """목표 버전 하나에 대한 출시 준비도."""
    issues = redmine.fetch_issues_by_version(version_id, project_id)
    runs = _runs_for(project_id, version_id)
    rows_by_issue = _latest_rows_by_issue(runs)

    all_rows = [row for rows in rows_by_issue.values() for row in rows]
    counts = _count_statuses(all_rows)
    total_rows = len(all_rows)
    executed = total_rows - counts.get("untested", 0)
    progress = round(executed / total_rows * 100, 1) if total_rows else 0.0

    untested_p0 = sum(
        1
        for row in all_rows
        if _status_of(row) == "untested" and str(row.get("priority", "")).upper() == "P0"
    )
    failed_rows = [
        {
            "issue_id": issue_id,
            "objective": row.get("objective") or row.get("text", ""),
            "actual": row.get("actual", ""),
            "defect": row.get("defect"),
        }
        for issue_id, rows in rows_by_issue.items()
        for row in rows
        if _status_of(row) == "fail"
    ]

    defects = _defect_summary(issues)
    velocity = _velocity(runs)
    remaining = counts.get("untested", 0)
    eta_days = None
    if velocity["recent_avg"] and remaining:
        eta_days = round(remaining / velocity["recent_avg"], 1)

    covered_issue_ids = {str(i) for i in rows_by_issue}
    uncovered = [
        {"id": i.get("id"), "subject": i.get("subject", "")}
        for i in issues
        if str(i.get("id")) not in covered_issue_ids
    ]

    return {
        "version_id": version_id,
        "issue_total": len(issues),
        "runs": [
            {"id": r["id"], "name": r.get("name", ""), "tester": r.get("tester", ""),
             "updated_at": r.get("updated_at", "")}
            for r in runs
        ],
        "test": {
            "total": total_rows,
            "executed": executed,
            "progress": progress,
            "pass_rate": _pass_rate(counts),
            "counts": counts,
            "untested_p0": untested_p0,
            "remaining": remaining,
            "eta_days": eta_days,
            "velocity": velocity,
        },
        "defects": defects,
        "failed": failed_rows[:50],
        "uncovered_issues": uncovered[:50],
        "uncovered_count": len(uncovered),
        "gate": _gate(progress, _pass_rate(counts), defects, untested_p0),
    }


# ── 요구사항 추적 매트릭스 ──────────────────────────────────


def _coverage_state(tc_count: int, counts: dict[str, int]) -> str:
    if tc_count == 0:
        return "미작성"
    if counts.get("fail", 0):
        return "실패"
    if counts.get("blocked", 0):
        return "차단"
    if counts.get("untested", 0) == tc_count:
        return "미실행"
    if counts.get("untested", 0):
        return "진행중"
    return "통과"


def traceability(
    project_id: str | None,
    version_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """요구사항(이슈) ↔ 테스트케이스 ↔ 실행결과 ↔ 결함 을 한 표로.

    '이 기능 테스트했나?' 를 5초 안에 답하기 위한 뷰다.
    """
    if version_id:
        issues = redmine.fetch_issues_by_version(version_id, project_id)
    elif date_from and date_to:
        issues = redmine.fetch_issues(date_from=date_from, date_to=date_to, project_id=project_id)
    else:
        raise ValueError("version_id 또는 date_from/date_to 가 필요합니다.")

    runs = _runs_for(project_id, version_id)
    rows_by_issue = _latest_rows_by_issue(runs)

    # 원본(master)에만 있고 아직 실행 안 한 케이스도 '작성됨'으로 인정한다.
    master_rows: dict[str, int] = {}
    for summary in storage.list_sets(project_id=project_id or "", set_type="master"):
        record = storage.read_set(summary["id"])
        if not record:
            continue
        meta = record.get("meta") or {}
        if version_id and str(meta.get("version") or "") != str(version_id):
            continue
        for card in record.get("cases") or []:
            key = str(card.get("id"))
            master_rows[key] = max(master_rows.get(key, 0), len(_rows_of(card)))

    matrix = []
    state_counts: dict[str, int] = {}
    for issue in issues:
        key = str(issue.get("id"))
        rows = rows_by_issue.get(key, [])
        counts = _count_statuses(rows)
        tc_count = len(rows) or master_rows.get(key, 0)
        state = _coverage_state(tc_count, counts) if rows else ("미실행" if tc_count else "미작성")
        state_counts[state] = state_counts.get(state, 0) + 1

        defects = []
        for row in rows:
            defect = row.get("defect")
            if isinstance(defect, dict) and defect.get("id"):
                defects.append({"id": defect["id"], "url": defect.get("url", "")})

        matrix.append(
            {
                "issue_id": issue.get("id"),
                "subject": issue.get("subject", ""),
                "tracker": (issue.get("tracker") or {}).get("name", ""),
                "priority": (issue.get("priority") or {}).get("name", ""),
                "status": (issue.get("status") or {}).get("name", ""),
                "assignee": (issue.get("assigned_to") or {}).get("name", "미배정"),
                "url": redmine.issue_url(issue.get("id")),
                "tc_count": tc_count,
                "executed": tc_count - counts.get("untested", 0) if rows else 0,
                "counts": counts,
                "state": state,
                "defects": defects,
            }
        )

    total = len(matrix)
    written = sum(1 for r in matrix if r["tc_count"] > 0)
    passed = state_counts.get("통과", 0)
    return {
        "rows": matrix,
        "summary": {
            "issues": total,
            "written": written,
            "write_rate": round(written / total * 100, 1) if total else 0.0,
            "passed": passed,
            "pass_rate": round(passed / total * 100, 1) if total else 0.0,
            "state_counts": state_counts,
        },
        "scope": {
            "version_id": version_id or "",
            "date_from": date_from or "",
            "date_to": date_to or "",
        },
    }


def traceability_csv(data: dict) -> str:
    """RTM 을 CSV 로. 엑셀에서 바로 열리도록 BOM 은 호출부에서 붙인다."""
    header = [
        "이슈ID", "제목", "트래커", "우선순위", "이슈상태", "담당자",
        "TC수", "실행", "Pass", "Fail", "Blocked", "N/A", "미시행", "커버리지", "결함",
    ]
    lines = [",".join(header)]

    def cell(value: Any) -> str:
        text = str(value if value is not None else "")
        # 이슈 제목 등이 '=' '+' '-' '@' 로 시작하면 엑셀이 셀을 수식으로 해석해
        # 실행할 수 있다(CSV Injection). 앞에 작은따옴표를 붙여 텍스트로 강제한다.
        if text[:1] in ("=", "+", "-", "@"):
            text = "'" + text
        if any(ch in text for ch in ',"\n'):
            text = '"' + text.replace('"', '""') + '"'
        return text

    for row in data["rows"]:
        counts = row["counts"]
        lines.append(
            ",".join(
                cell(v)
                for v in (
                    row["issue_id"], row["subject"], row["tracker"], row["priority"],
                    row["status"], row["assignee"], row["tc_count"], row["executed"],
                    counts.get("pass", 0), counts.get("fail", 0), counts.get("blocked", 0),
                    counts.get("na", 0), counts.get("untested", 0), row["state"],
                    " ".join(f"#{d['id']}" for d in row["defects"]),
                )
            )
        )
    return "\n".join(lines)

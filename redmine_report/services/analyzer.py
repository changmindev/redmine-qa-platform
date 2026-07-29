"""수집한 이슈 데이터를 리포트용 통계로 변환한다.

담당자별 통계(assignee_stats)의 키는 항상 고정 영문 키(new/progress/resolved/qa)를 쓴다.
Redmine 상태명이 프로젝트마다 다르므로, 화면/PDF 는 이 영문 키에 의존한다.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from ..config import settings
from .timeutil import days_between, local_date, local_month, now_utc, parse_dt

#: 우선순위 표시 순서 — Redmine 기본값(낮음 < 보통 < 높음 < 긴급 < 즉시)의 역순.
#: 원본은 "즉시"가 "낮음" 뒤에 있어 리포트 막대가 뒤죽박죽 정렬됐다.
PRIORITY_ORDER = ["즉시", "긴급", "높음", "보통", "낮음"]


def status_key_map() -> dict[str, str]:
    """Redmine 상태명 → 고정 영문 키."""
    return {name: key for key, name in settings.statuses.items()}


def _status_name(issue: dict, default: str = "") -> str:
    return (issue.get("status") or {}).get("name", default)


def _tracker_name(issue: dict, default: str = "") -> str:
    return (issue.get("tracker") or {}).get("name", default)


def _priority_name(issue: dict, default: str = "보통") -> str:
    return (issue.get("priority") or {}).get("name", default)


def _assignee_name(issue: dict) -> str:
    return (issue.get("assigned_to") or {}).get("name", "미배정")


def _status_transitions(journals: list[dict]):
    """저널에서 상태 변경 이력만 (created_on, old_id, new_id) 로 뽑는다."""
    for journal in sorted(journals or [], key=lambda j: j.get("created_on", "")):
        for detail in journal.get("details", []) or []:
            if detail.get("property") == "attr" and detail.get("name") == "status_id":
                yield (
                    journal.get("created_on"),
                    str(detail.get("old_value") or ""),
                    str(detail.get("new_value") or ""),
                )


def _is_reopened(journals: list[dict], status_map: dict) -> bool:
    """완료(닫힘) 상태에서 다시 열린 이력이 있으면 True."""
    was_closed = False
    for _, _old_id, new_id in _status_transitions(journals):
        new_closed = status_map.get(new_id, {}).get("is_closed", False)
        if new_closed:
            was_closed = True
        elif was_closed:
            return True
    return False


def _ever_closed(journals: list[dict], status_map: dict) -> bool:
    return any(
        status_map.get(new_id, {}).get("is_closed", False)
        for _, _, new_id in _status_transitions(journals)
    )


def _long_term_issues(open_issues: list[dict], threshold_days: int | None = None) -> list[dict]:
    """미해결 장기 이슈 목록 (경과일 내림차순)."""
    threshold = settings.long_term_days if threshold_days is None else threshold_days
    now = now_utc()
    result = []
    for issue in open_issues:
        created = parse_dt(issue.get("created_on"))
        days_open = days_between(created, now)
        if days_open is None or days_open < threshold:
            continue
        result.append(
            {
                "id": issue.get("id"),
                "subject": issue.get("subject", ""),
                "assignee": _assignee_name(issue),
                "priority": _priority_name(issue),
                "status": _status_name(issue),
                "tracker": _tracker_name(issue),
                "days_open": days_open,
                "created_on": local_date(created) or "-",
            }
        )
    result.sort(key=lambda x: (-x["days_open"], x["priority"]))
    return result


def _priority_avg_days(issues: list[dict]) -> dict[str, float]:
    """우선순위별 평균 처리일 (해결·QA 완료 건 기준)."""
    closed = settings.closed_statuses
    days_by_pri: dict[str, list[int]] = defaultdict(list)
    for issue in issues:
        if _status_name(issue) not in closed:
            continue
        created = parse_dt(issue.get("created_on"))
        updated = parse_dt(issue.get("updated_on"))
        if not (created and updated):
            continue
        days_by_pri[_priority_name(issue)].append((updated - created).days)

    averages = {p: round(sum(d) / len(d), 1) for p, d in days_by_pri.items() if d}
    ordered = {p: averages[p] for p in PRIORITY_ORDER if p in averages}
    ordered.update({p: v for p, v in averages.items() if p not in ordered})
    return ordered


def _bug_trend(issues: list[dict]) -> dict:
    """버그 트래커 이슈의 월별 생성 추이 및 전월 대비 증감."""
    monthly: dict[str, int] = defaultdict(int)
    for issue in issues:
        if _tracker_name(issue) not in settings.bug_trackers:
            continue
        month = local_month(parse_dt(issue.get("created_on")))
        if month:
            monthly[month] += 1

    sorted_months = sorted(monthly.items())
    current = previous = 0
    change_pct = 0.0
    if len(sorted_months) >= 2:
        previous, current = sorted_months[-2][1], sorted_months[-1][1]
        if previous:
            change_pct = round((current - previous) / previous * 100, 1)
        elif current:
            change_pct = 100.0
    elif sorted_months:
        current = sorted_months[0][1]

    return {
        "monthly": dict(sorted_months),
        "current": current,
        "previous": previous,
        "change_pct": change_pct,
        "total_bugs": sum(monthly.values()),
    }


def _daily_resolved(issues: list[dict], status_map: dict) -> dict[str, int]:
    """일자별 '해결된 이슈 수'.

    한 이슈가 하루에 여러 번 닫히더라도 그 날짜에 1건으로만 센다.
    (원본은 상태 변경 이벤트마다 더해 재오픈이 잦은 이슈가 중복 집계됐다.)
    """
    seen: set[tuple] = set()
    daily: dict[str, int] = defaultdict(int)
    closed_names = settings.closed_statuses

    for issue in issues:
        issue_id = issue.get("id")
        journals = issue.get("journals") or []
        counted = False
        for created_on, _, new_id in _status_transitions(journals):
            if not status_map.get(new_id, {}).get("is_closed", False):
                continue
            day = local_date(parse_dt(created_on))
            if not day:
                continue
            counted = True
            if (issue_id, day) not in seen:
                seen.add((issue_id, day))
                daily[day] += 1

        # 저널이 없거나 상태 이력이 남지 않은 경우 updated_on 으로 폴백
        if not counted and _status_name(issue) in closed_names:
            day = local_date(parse_dt(issue.get("updated_on")))
            if day and (issue_id, day) not in seen:
                seen.add((issue_id, day))
                daily[day] += 1

    return dict(sorted(daily.items()))


def _reopen_stats(issues: list[dict], status_map: dict) -> dict:
    """이슈 재오픈율 (완료 후 재오픈된 건 / 완료 이력이 있는 건)."""
    reopened = ever_closed = bug_reopened = bug_ever_closed = 0

    for issue in issues:
        journals = issue.get("journals") or []
        if not journals:
            continue
        is_bug = _tracker_name(issue) in settings.bug_trackers
        if _is_reopened(journals, status_map):
            reopened += 1
            ever_closed += 1
            if is_bug:
                bug_reopened += 1
                bug_ever_closed += 1
        elif _ever_closed(journals, status_map):
            ever_closed += 1
            if is_bug:
                bug_ever_closed += 1

    return {
        "reopen_count": reopened,
        "ever_closed": ever_closed,
        "reopen_rate": round(reopened / ever_closed * 100, 1) if ever_closed else 0,
        "bug_reopen_count": bug_reopened,
        "bug_ever_closed": bug_ever_closed,
        "bug_reopen_rate": round(bug_reopened / bug_ever_closed * 100, 1) if bug_ever_closed else 0,
    }


#: 우선순위 → 부하 가중치. 긴급 1건과 낮음 1건을 같게 세면 워크로드가 왜곡된다.
PRIORITY_WEIGHT = {"즉시": 5, "긴급": 5, "높음": 3, "보통": 2, "낮음": 1}
_DEFAULT_WEIGHT = 2


def workload(open_issues: list[dict]) -> dict:
    """담당자별 워크로드와 병목 진단.

    단순 건수 나열이 아니라 '누가 병목인지'까지 판단한다.
      · load_score : 우선순위 가중 합 (긴급 5 … 낮음 1)
      · aging      : 장기 미해결(LONG_TERM_DAYS 초과) 건수
      · verdict    : 과부하 / 지연위험 / 정상 / 여유
    """
    now = now_utc()
    people: dict[str, dict] = {}

    for issue in open_issues or []:
        name = _assignee_name(issue)
        priority = _priority_name(issue)
        created = parse_dt(issue.get("created_on"))
        age = days_between(created, now) or 0

        person = people.setdefault(
            name,
            {
                "assignee": name,
                "open": 0,
                "load_score": 0,
                "urgent": 0,
                "aging": 0,
                "oldest_days": 0,
                "by_priority": {},
            },
        )
        person["open"] += 1
        person["load_score"] += PRIORITY_WEIGHT.get(priority, _DEFAULT_WEIGHT)
        person["by_priority"][priority] = person["by_priority"].get(priority, 0) + 1
        if priority in ("즉시", "긴급"):
            person["urgent"] += 1
        if age >= settings.long_term_days:
            person["aging"] += 1
        person["oldest_days"] = max(person["oldest_days"], age)

    rows = sorted(people.values(), key=lambda p: -p["load_score"])
    if not rows:
        return {"rows": [], "total_open": 0, "avg_load": 0, "overloaded": [], "summary": ""}

    scores = [p["load_score"] for p in rows]
    avg_load = round(sum(scores) / len(scores), 1)
    # 평균의 1.5배를 넘으면 과부하로 본다 — 표준편차보다 설명하기 쉽고 소규모 팀에서 안정적이다.
    overload_line = max(avg_load * 1.5, avg_load + 3)

    for person in rows:
        if person["load_score"] >= overload_line:
            person["verdict"] = "과부하"
        elif person["aging"] >= 3:
            person["verdict"] = "지연위험"
        elif person["load_score"] <= avg_load * 0.5:
            person["verdict"] = "여유"
        else:
            person["verdict"] = "정상"
        person["share"] = round(person["load_score"] / sum(scores) * 100, 1) if scores else 0

    overloaded = [p["assignee"] for p in rows if p["verdict"] == "과부하"]
    idle = [p["assignee"] for p in rows if p["verdict"] == "여유"]

    if overloaded and idle:
        summary = f"{', '.join(overloaded)} 에 부하가 몰려 있습니다. {', '.join(idle)} 로 재분배를 검토하세요."
    elif overloaded:
        summary = f"{', '.join(overloaded)} 의 부하가 평균의 1.5배를 넘습니다."
    elif any(p["verdict"] == "지연위험" for p in rows):
        risky = [p["assignee"] for p in rows if p["verdict"] == "지연위험"]
        summary = f"{', '.join(risky)} 에게 장기 미해결 건이 쌓이고 있습니다."
    else:
        summary = "담당자별 부하가 고르게 분산되어 있습니다."

    return {
        "rows": rows,
        "total_open": sum(p["open"] for p in rows),
        "avg_load": avg_load,
        "overload_line": round(overload_line, 1),
        "overloaded": overloaded,
        "summary": summary,
    }


def analyze(
    issues: list[dict],
    open_issues: list[dict] | None = None,
    status_map: dict | None = None,
) -> dict:
    """이슈 목록 → 대시보드/PDF 가 소비하는 통계 dict."""
    status_map = status_map or {}
    statuses = settings.statuses
    key_map = status_key_map()

    status_count: dict[str, int] = defaultdict(int)
    priority_count: dict[str, int] = defaultdict(int)
    tracker_count: dict[str, int] = defaultdict(int)
    daily_created: dict[str, int] = defaultdict(int)
    resolved_days: list[int] = []

    def _blank_assignee():
        return {
            "total": 0,
            "new": 0,
            "progress": 0,
            "resolved": 0,
            "qa": 0,
            "other": 0,
            "avg_days": "-",
            "_days_list": [],
        }

    assignee_stats: dict[str, dict] = defaultdict(_blank_assignee)

    for issue in issues:
        raw_status = _status_name(issue, "기타")
        created: datetime | None = parse_dt(issue.get("created_on"))
        updated: datetime | None = parse_dt(issue.get("updated_on"))

        status_count[raw_status] += 1
        tracker_count[_tracker_name(issue, "기타")] += 1
        priority_count[_priority_name(issue)] += 1

        day = local_date(created)
        if day:
            daily_created[day] += 1

        stats = assignee_stats[_assignee_name(issue)]
        stats["total"] += 1
        stats[key_map.get(raw_status, "other")] += 1

        if raw_status in settings.closed_statuses and created and updated:
            days = (updated - created).days
            stats["_days_list"].append(days)
            resolved_days.append(days)

    for stats in assignee_stats.values():
        durations = stats.pop("_days_list")
        stats["avg_days"] = round(sum(durations) / len(durations), 1) if durations else "-"

    qa_count = status_count.get(statuses["qa"], 0)
    open_list = (
        open_issues
        if open_issues is not None
        else [i for i in issues if _status_name(i) in settings.open_statuses]
    )

    return {
        "total": len(issues),
        "status_count": dict(status_count),
        "assignee_stats": dict(assignee_stats),
        "tracker_count": dict(tracker_count),
        "priority_count": dict(priority_count),
        "daily_created": dict(sorted(daily_created.items())),
        "daily_resolved": _daily_resolved(issues, status_map),
        "avg_days_total": (
            round(sum(resolved_days) / len(resolved_days), 1) if resolved_days else "-"
        ),
        "qa_rate": round(qa_count / len(issues) * 100, 1) if issues else 0,
        "wip_count": status_count.get(statuses["progress"], 0),
        "qa_count": qa_count,
        # 리포트 핵심 지표
        "workload": workload(open_list),
        "long_term_issues": _long_term_issues(open_list),
        "priority_avg_days": _priority_avg_days(issues),
        "bug_trend": _bug_trend(issues),
        "reopen": _reopen_stats(issues, status_map),
    }

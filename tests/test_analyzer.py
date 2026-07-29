"""분석 로직 테스트 — 특히 원본에 있던 버그들의 회귀 방지."""

from __future__ import annotations

from redmine_report.services.analyzer import PRIORITY_ORDER, analyze
from redmine_report.services.timeutil import local_date, parse_dt


def _closed_journal(day: str, old="2", new="5"):
    return {
        "created_on": f"{day}T05:00:00Z",
        "details": [{"property": "attr", "name": "status_id", "old_value": old, "new_value": new}],
    }


def test_priority_order_puts_immediate_first():
    """'즉시'는 Redmine 에서 가장 높은 우선순위 — 목록 맨 앞이어야 한다."""
    assert PRIORITY_ORDER[0] == "즉시"
    assert PRIORITY_ORDER.index("긴급") < PRIORITY_ORDER.index("보통")
    assert PRIORITY_ORDER[-1] == "낮음"


def test_parse_dt_is_timezone_aware():
    dt = parse_dt("2026-07-01T12:00:00Z")
    assert dt is not None and dt.tzinfo is not None
    assert parse_dt("garbage") is None
    assert parse_dt(None) is None


def test_local_date_converts_utc_to_display_timezone():
    """UTC 2026-06-30 22:00 은 KST 로 2026-07-01 이다(원본은 하루 밀렸다)."""
    assert local_date(parse_dt("2026-06-30T22:00:00Z")) == "2026-07-01"


def test_analyze_counts_status_and_assignee(issue_factory, status_map):
    issues = [
        issue_factory(1, status="신규", assignee="A"),
        issue_factory(2, status="진행중", assignee="A"),
        issue_factory(3, status="완료(Dev-QA)", assignee="B"),
    ]
    stats = analyze(issues, open_issues=[], status_map=status_map)

    assert stats["total"] == 3
    assert stats["status_count"]["신규"] == 1
    assert stats["qa_count"] == 1
    assert stats["qa_rate"] == 33.3
    assert stats["wip_count"] == 1
    assert stats["assignee_stats"]["A"]["total"] == 2
    assert stats["assignee_stats"]["A"]["new"] == 1
    assert stats["assignee_stats"]["A"]["progress"] == 1
    assert stats["assignee_stats"]["B"]["qa"] == 1


def test_analyze_handles_empty_input(status_map):
    stats = analyze([], open_issues=[], status_map=status_map)
    assert stats["total"] == 0
    assert stats["qa_rate"] == 0
    assert stats["avg_days_total"] == "-"


def test_daily_resolved_counts_each_issue_once_per_day(issue_factory, status_map):
    """같은 이슈가 하루에 두 번 닫혀도 그 날짜에 1건으로만 센다."""
    issue = issue_factory(
        1,
        status="작업완료",
        journals=[_closed_journal("2026-07-02"), _closed_journal("2026-07-02")],
    )
    stats = analyze([issue], open_issues=[], status_map=status_map)
    assert stats["daily_resolved"] == {"2026-07-02": 1}


def test_reopen_stats(issue_factory, status_map):
    reopened = issue_factory(
        1,
        journals=[
            _closed_journal("2026-07-02"),
            {
                "created_on": "2026-07-03T05:00:00Z",
                "details": [
                    {"property": "attr", "name": "status_id", "old_value": "5", "new_value": "2"}
                ],
            },
        ],
    )
    closed_only = issue_factory(2, journals=[_closed_journal("2026-07-02")])

    stats = analyze([reopened, closed_only], open_issues=[], status_map=status_map)
    reopen = stats["reopen"]

    assert reopen["reopen_count"] == 1
    assert reopen["ever_closed"] == 2
    assert reopen["reopen_rate"] == 50.0


def test_long_term_issues_uses_open_list(issue_factory, status_map):
    old = issue_factory(9, status="신규", created_on="2020-01-01T00:00:00Z")
    stats = analyze([], open_issues=[old], status_map=status_map)

    long_term = stats["long_term_issues"]
    assert len(long_term) == 1
    assert long_term[0]["id"] == 9
    assert long_term[0]["days_open"] > 14


def test_bug_trend_change_pct(issue_factory, status_map):
    issues = [
        issue_factory(1, tracker="버그", created_on="2026-05-10T00:00:00Z"),
        issue_factory(2, tracker="버그", created_on="2026-06-10T00:00:00Z"),
        issue_factory(3, tracker="버그", created_on="2026-06-11T00:00:00Z"),
        issue_factory(4, tracker="기능", created_on="2026-06-12T00:00:00Z"),
    ]
    trend = analyze(issues, open_issues=[], status_map=status_map)["bug_trend"]

    assert trend["total_bugs"] == 3  # '기능' 트래커는 제외
    assert trend["previous"] == 1
    assert trend["current"] == 2
    assert trend["change_pct"] == 100.0

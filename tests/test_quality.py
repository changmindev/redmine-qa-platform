"""릴리스 준비도 · 요구사항 추적 매트릭스 · 워크로드 테스트."""

from __future__ import annotations

import pytest

from redmine_report.services import quality, redmine, storage
from redmine_report.services.analyzer import workload


@pytest.fixture(autouse=True)
def _isolated(data_dir):
    return data_dir


def _card(issue_id, statuses, priority="P1", subject="제목"):
    """이슈 카드 1건 — statuses 목록대로 실행 상태를 채운다."""
    return {
        "id": issue_id,
        "subject": subject,
        "result": {
            "cases": [
                {
                    "priority": priority,
                    "depth1": "회원",
                    "objective": f"검증 {idx}",
                    "status": status,
                }
                for idx, status in enumerate(statuses)
            ]
        },
    }


def _save_run(version, cards, name="1회차"):
    record = storage.build_set(
        {
            "type": "run",
            "project": "test-project",
            "name": name,
            "meta": {"version": version, "versionName": "v1.0"},
            "cases": cards,
        }
    )
    storage.write_set(record)
    return record


def _issue(issue_id, tracker="기능", priority="보통", status="신규", subject="이슈"):
    return {
        "id": issue_id,
        "subject": subject,
        "tracker": {"name": tracker},
        "priority": {"name": priority},
        "status": {"name": status},
        "assigned_to": {"name": "홍길동"},
    }


# ── 워크로드 ────────────────────────────────────────────


def test_workload_weights_priority_over_count(issue_factory):
    """건수가 적어도 긴급이 많으면 부하가 더 크게 잡혀야 한다."""
    open_issues = [
        issue_factory(1, assignee="A", priority="긴급"),
        issue_factory(2, assignee="A", priority="긴급"),
        issue_factory(3, assignee="B", priority="낮음"),
        issue_factory(4, assignee="B", priority="낮음"),
        issue_factory(5, assignee="B", priority="낮음"),
    ]
    result = workload(open_issues)
    rows = {r["assignee"]: r for r in result["rows"]}

    assert rows["A"]["open"] == 2 and rows["B"]["open"] == 3
    assert rows["A"]["load_score"] == 10  # 긴급 5 × 2
    assert rows["B"]["load_score"] == 3  # 낮음 1 × 3
    assert result["rows"][0]["assignee"] == "A"  # 부하 순 정렬


def test_workload_flags_overload_and_suggests_rebalance(issue_factory):
    open_issues = [issue_factory(i, assignee="A", priority="긴급") for i in range(1, 7)]
    open_issues += [issue_factory(10, assignee="B", priority="낮음")]
    result = workload(open_issues)

    assert "A" in result["overloaded"]
    assert "재분배" in result["summary"]


def test_workload_empty():
    result = workload([])
    assert result["rows"] == [] and result["total_open"] == 0


def test_workload_counts_aging(issue_factory):
    old = [issue_factory(i, assignee="A", created_on="2020-01-01T00:00:00Z") for i in range(1, 4)]
    rows = {r["assignee"]: r for r in workload(old)["rows"]}
    assert rows["A"]["aging"] == 3
    assert rows["A"]["oldest_days"] > 1000


# ── 릴리스 준비도 ────────────────────────────────────────


def test_release_gate_blocks_on_open_blocker(monkeypatch):
    monkeypatch.setattr(
        redmine,
        "fetch_issues_by_version",
        lambda *a, **kw: [
            _issue(1),
            _issue(2, tracker="버그", priority="긴급", status="신규"),
        ],
    )
    _save_run("7", [_card(1, ["pass"] * 10)])

    data = quality.release_readiness("test-project", "7")

    assert data["gate"]["level"] == "blocked"
    assert any("Blocker" in r for r in data["gate"]["reasons"])
    assert data["defects"]["open"] == 1
    assert data["defects"]["blockers"][0]["id"] == 2


def test_release_gate_blocks_on_untested_p0(monkeypatch):
    monkeypatch.setattr(redmine, "fetch_issues_by_version", lambda *a, **kw: [_issue(1)])
    _save_run("7", [_card(1, ["pass"] * 9 + ["untested"], priority="P0")])

    data = quality.release_readiness("test-project", "7")
    assert data["gate"]["level"] == "blocked"
    assert any("P0" in r for r in data["gate"]["reasons"])


def test_release_gate_ready_when_all_clear(monkeypatch):
    monkeypatch.setattr(redmine, "fetch_issues_by_version", lambda *a, **kw: [_issue(1)])
    _save_run("7", [_card(1, ["pass"] * 10)])

    data = quality.release_readiness("test-project", "7")
    assert data["gate"]["level"] == "ready"
    assert data["test"]["progress"] == 100.0
    assert data["test"]["pass_rate"] == 100.0


def test_pass_rate_excludes_untested_and_na(monkeypatch):
    """미시행·N/A 를 분모에 넣으면 '테스트를 안 할수록 통과율이 낮아지는' 왜곡이 생긴다."""
    monkeypatch.setattr(redmine, "fetch_issues_by_version", lambda *a, **kw: [_issue(1)])
    _save_run("7", [_card(1, ["pass", "pass", "pass", "fail", "na", "untested"])])

    data = quality.release_readiness("test-project", "7")
    assert data["test"]["pass_rate"] == 75.0  # 3 / (3+1)
    assert data["test"]["progress"] == round(5 / 6 * 100, 1)


def test_release_reports_uncovered_issues(monkeypatch):
    monkeypatch.setattr(
        redmine, "fetch_issues_by_version", lambda *a, **kw: [_issue(1), _issue(2), _issue(3)]
    )
    _save_run("7", [_card(1, ["pass"])])

    data = quality.release_readiness("test-project", "7")
    assert data["uncovered_count"] == 2
    assert {u["id"] for u in data["uncovered_issues"]} == {2, 3}


def test_release_uses_latest_run_per_issue(monkeypatch):
    """같은 이슈를 두 회차 돌렸으면 최신 회차 결과를 쓴다."""
    monkeypatch.setattr(redmine, "fetch_issues_by_version", lambda *a, **kw: [_issue(1)])
    _save_run("7", [_card(1, ["fail", "fail"])], name="1회차")
    _save_run("7", [_card(1, ["pass", "pass"])], name="2회차")

    data = quality.release_readiness("test-project", "7")
    assert data["test"]["counts"]["fail"] == 0
    assert data["test"]["counts"]["pass"] == 2


def test_release_ignores_other_versions(monkeypatch):
    monkeypatch.setattr(redmine, "fetch_issues_by_version", lambda *a, **kw: [_issue(1)])
    _save_run("99", [_card(1, ["pass"] * 5)])

    data = quality.release_readiness("test-project", "7")
    assert data["test"]["total"] == 0


def test_velocity_and_eta(monkeypatch):
    monkeypatch.setattr(redmine, "fetch_issues_by_version", lambda *a, **kw: [_issue(1)])
    card = _card(1, ["pass"] * 6 + ["untested"] * 4)
    for idx, row in enumerate(card["result"]["cases"][:6]):
        row["executed_at"] = f"2026-07-0{idx // 2 + 1}T02:00:00Z"
    _save_run("7", [card])

    data = quality.release_readiness("test-project", "7")
    assert data["test"]["velocity"]["recent_avg"] == 2.0
    assert data["test"]["eta_days"] == 2.0


# ── 요구사항 추적 매트릭스 ───────────────────────────────


def test_traceability_states(monkeypatch):
    monkeypatch.setattr(
        redmine,
        "fetch_issues_by_version",
        lambda *a, **kw: [_issue(1, subject="통과"), _issue(2, subject="실패"),
                          _issue(3, subject="미작성")],
    )
    _save_run("7", [_card(1, ["pass", "pass"]), _card(2, ["pass", "fail"])])

    data = quality.traceability("test-project", version_id="7")
    states = {r["issue_id"]: r["state"] for r in data["rows"]}

    assert states[1] == "통과"
    assert states[2] == "실패"
    assert states[3] == "미작성"
    assert data["summary"]["issues"] == 3
    assert data["summary"]["written"] == 2


def test_traceability_collects_defect_links(monkeypatch):
    monkeypatch.setattr(redmine, "fetch_issues_by_version", lambda *a, **kw: [_issue(1)])
    card = _card(1, ["fail"])
    card["result"]["cases"][0]["defect"] = {"id": 555, "url": "http://x/issues/555"}
    _save_run("7", [card])

    row = quality.traceability("test-project", version_id="7")["rows"][0]
    assert row["defects"] == [{"id": 555, "url": "http://x/issues/555"}]


def test_traceability_requires_scope():
    with pytest.raises(ValueError):
        quality.traceability("test-project")


def test_traceability_csv_escapes_commas(monkeypatch):
    monkeypatch.setattr(
        redmine, "fetch_issues_by_version", lambda *a, **kw: [_issue(1, subject="가, 나")]
    )
    _save_run("7", [_card(1, ["pass"])])

    csv = quality.traceability_csv(quality.traceability("test-project", version_id="7"))
    assert '"가, 나"' in csv
    assert csv.splitlines()[0].startswith("이슈ID,제목")


def test_traceability_csv_neutralizes_formula_injection(monkeypatch):
    """이슈 제목이 '=' 등으로 시작해도 CSV 셀이 수식으로 해석되지 않아야 한다."""
    monkeypatch.setattr(
        redmine,
        "fetch_issues_by_version",
        lambda *a, **kw: [_issue(1, subject='=HYPERLINK("http://evil.example")')],
    )
    _save_run("7", [_card(1, ["pass"])])

    csv = quality.traceability_csv(quality.traceability("test-project", version_id="7"))
    row = csv.splitlines()[1]
    assert "'=HYPERLINK(" in row

"""테스트케이스 diff · 리비전 이력 테스트."""

from __future__ import annotations

import pytest

from redmine_report.services import storage, tcdiff


def _tc(objective, expected="기대", depth1="회원", priority="P1"):
    return {
        "priority": priority,
        "type": "정상",
        "depth1": depth1,
        "depth2": "",
        "depth3": "",
        "depth4": "",
        "objective": objective,
        "precondition": "",
        "procedure": "1. 실행",
        "expected": expected,
    }


# ── 시그니처 ────────────────────────────────────────────


def test_signature_ignores_whitespace_and_case():
    a = _tc("로그인  검증")
    b = _tc("로그인 검증")
    assert tcdiff.signature(a) == tcdiff.signature(b)


def test_signature_differs_by_feature_path():
    assert tcdiff.signature(_tc("검증", depth1="회원")) != tcdiff.signature(_tc("검증", depth1="결제"))


def test_checklist_signature():
    row = {"category": "기본", "text": "홈으로 이동하는지 확인"}
    assert tcdiff.signature(row).startswith("cl:")


# ── 항목 단위 diff ───────────────────────────────────────


def test_diff_detects_added_and_removed():
    before = [_tc("A"), _tc("B")]
    after = [_tc("B"), _tc("C")]
    result = tcdiff.diff_rows(before, after)

    assert result["summary"]["added"] == 1
    assert result["summary"]["removed"] == 1
    assert result["summary"]["unchanged"] == 1
    assert result["added"][0]["label"].endswith("C")
    assert result["removed"][0]["label"].endswith("A")


def test_diff_detects_field_change():
    before = [_tc("로그인 검증", expected="홈으로 이동")]
    after = [_tc("로그인 검증", expected="대시보드로 이동")]
    result = tcdiff.diff_rows(before, after)

    assert result["summary"]["changed"] == 1
    change = result["changed"][0]["fields"]["expected"]
    assert change["before"] == "홈으로 이동"
    assert change["after"] == "대시보드로 이동"


def test_diff_ignores_execution_fields():
    """실행 결과(status/actual)는 '내용 변경'이 아니므로 diff 에 잡히면 안 된다."""
    before = [_tc("검증")]
    after = [{**_tc("검증"), "status": "fail", "actual": "오류 발생", "done": True}]
    assert tcdiff.diff_rows(before, after)["summary"]["has_change"] is False


def test_diff_of_identical_lists():
    rows = [_tc("A"), _tc("B")]
    result = tcdiff.diff_rows(rows, rows)
    assert result["summary"] == {
        "added": 0, "removed": 0, "changed": 0, "unchanged": 2, "has_change": False
    }


def test_diff_empty_sides():
    assert tcdiff.diff_rows([], [_tc("A")])["summary"]["added"] == 1
    assert tcdiff.diff_rows([_tc("A")], [])["summary"]["removed"] == 1


# ── 세트 단위 diff ───────────────────────────────────────


def _card(issue_id, objectives):
    return {"id": issue_id, "subject": f"이슈 {issue_id}",
            "result": {"cases": [_tc(o) for o in objectives]}}


def test_diff_sets_by_issue():
    before = [_card(1, ["A", "B"]), _card(2, ["X"])]
    after = [_card(1, ["A", "C"]), _card(3, ["Z"])]
    result = tcdiff.diff_sets(before, after)

    by_issue = {i["issue_id"]: i for i in result["issues"]}
    assert by_issue["1"]["kind"] == "issue_kept"
    assert by_issue["2"]["kind"] == "issue_removed"
    assert by_issue["3"]["kind"] == "issue_added"
    assert result["summary"]["added"] == 2   # 1번의 C + 3번의 Z
    assert result["summary"]["removed"] == 2  # 1번의 B + 2번의 X
    assert result["summary"]["has_change"] is True


# ── 리비전 이력 ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated(data_dir):
    return data_dir


def test_revision_created_only_when_cases_change():
    record = storage.build_set({"type": "master", "project": "p", "name": "세트",
                                "cases": [_card(1, ["A"])]})
    storage.write_set(record)
    assert storage.list_revisions(record["id"]) == []  # 최초 저장은 이력 없음

    record["cases"] = [_card(1, ["A", "B"])]
    storage.write_set(record)
    assert len(storage.list_revisions(record["id"])) == 1

    storage.write_set(record)  # 내용 동일 → 이력 안 쌓임
    assert len(storage.list_revisions(record["id"])) == 1


def test_revision_not_kept_for_run_sets():
    record = storage.build_set({"type": "run", "project": "p", "name": "회차",
                                "cases": [_card(1, ["A"])]})
    storage.write_set(record)
    record["cases"] = [_card(1, ["A", "B"])]
    storage.write_set(record)
    assert storage.list_revisions(record["id"]) == []


def test_revision_pruned_to_limit(monkeypatch):
    monkeypatch.setattr(storage, "MAX_REVISIONS", 3)
    record = storage.build_set({"type": "master", "project": "p", "name": "세트",
                                "cases": [_card(1, ["A"])]})
    storage.write_set(record)
    for n in range(6):
        record["cases"] = [_card(1, ["A"] + [f"추가{n}"])]
        storage.write_set(record)
    assert len(storage.list_revisions(record["id"])) <= 3


def test_read_revision_current_and_diff():
    record = storage.build_set({"type": "master", "project": "p", "name": "세트",
                                "cases": [_card(1, ["A"])]})
    storage.write_set(record)
    record["cases"] = [_card(1, ["A", "B"])]
    storage.write_set(record)

    revs = storage.list_revisions(record["id"])
    old = storage.read_revision(record["id"], revs[0]["rev"])
    new = storage.read_revision(record["id"], "current")

    result = tcdiff.diff_sets(old["cases"], new["cases"])
    assert result["summary"]["added"] == 1


def test_read_revision_rejects_bad_rev():
    with pytest.raises(storage.StorageError):
        storage.read_revision("m_p_1", "../../etc")


def test_delete_set_removes_revisions():
    record = storage.build_set({"type": "master", "project": "p", "name": "세트",
                                "cases": [_card(1, ["A"])]})
    storage.write_set(record)
    record["cases"] = [_card(1, ["A", "B"])]
    storage.write_set(record)
    assert storage.list_revisions(record["id"])

    storage.delete_set(record["id"])
    assert storage.list_revisions(record["id"]) == []

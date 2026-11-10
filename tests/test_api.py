"""HTTP 계층 테스트 — Redmine/Claude 는 모두 대체(monkeypatch)한다."""

from __future__ import annotations

import pytest

from redmine_report.api import reports as reports_api
from redmine_report.api import testcases as tc_api
from redmine_report.services import ai, redmine


@pytest.fixture()
def fake_redmine(monkeypatch, issue_factory, status_map):
    issues = [issue_factory(1, subject="로그인 실패"), issue_factory(2, subject="결제 오류")]

    monkeypatch.setattr(redmine, "fetch_issues", lambda **kw: issues)
    monkeypatch.setattr(redmine, "fetch_issues_by_version", lambda *a, **kw: issues[:1])
    monkeypatch.setattr(redmine, "fetch_open_issues", lambda **kw: [])
    monkeypatch.setattr(redmine, "fetch_statuses", lambda: status_map)
    monkeypatch.setattr(redmine, "fetch_versions", lambda **kw: [{"id": 7, "name": "v1.0"}])
    monkeypatch.setattr(redmine, "fetch_project_info", lambda **kw: {"name": "테스트 프로젝트"})
    monkeypatch.setattr(redmine, "fetch_issue", lambda iid: issue_factory(int(iid)))
    return issues


def test_dashboard_page_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_testcases_page_renders(client):
    assert client.get("/testcases").status_code == 200


def test_healthz_does_not_leak_secrets(client):
    body = client.get("/healthz").get_json()
    assert body["ok"] is True
    assert body["redmine_key_set"] is True
    assert "test-key" not in str(body)


def test_report_endpoint(client, fake_redmine):
    resp = client.get("/api/report?date_from=2026-07-01&date_to=2026-07-10")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["stats"]["total"] == 2
    assert body["project"] == "테스트 프로젝트"
    assert body["date_from"] == "2026-07-01"


def test_report_uses_cache(client, fake_redmine, monkeypatch):
    calls = {"n": 0}

    def counting_fetch(**kw):
        calls["n"] += 1
        return fake_redmine

    monkeypatch.setattr(redmine, "fetch_issues", counting_fetch)
    url = "/api/report?date_from=2026-07-01&date_to=2026-07-10"
    client.get(url)
    client.get(url)
    assert calls["n"] == 1

    client.post("/api/clear_cache")
    client.get(url)
    assert calls["n"] == 2


@pytest.mark.parametrize(
    "query",
    ["?date_from=2026-13-01&date_to=2026-07-10", "?date_from=nope&date_to=2026-07-10", "?months=abc"],
)
def test_report_rejects_bad_params(client, fake_redmine, query):
    resp = client.get("/api/report" + query)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_report_defaults_to_months_when_no_dates(client, fake_redmine):
    assert client.get("/api/report").status_code == 200


def test_redmine_failure_becomes_502(client, monkeypatch):
    def boom(**kw):
        raise redmine.RedmineError("서버 다운", status_code=500)

    monkeypatch.setattr(redmine, "fetch_versions", boom)
    resp = client.get("/api/versions")
    assert resp.status_code == 502
    assert "서버 다운" in resp.get_json()["error"]


def test_tc_issues_requires_range_or_version(client, fake_redmine):
    resp = client.get("/api/tc_issues")
    assert resp.status_code == 400


def test_tc_issues_by_range(client, fake_redmine):
    body = client.get("/api/tc_issues?date_from=2026-07-01&date_to=2026-07-10").get_json()
    assert body["total"] == 2
    assert body["issues"][0]["subject"] == "로그인 실패"


def test_tc_issues_by_version_only(client, fake_redmine):
    body = client.get("/api/tc_issues?version_id=7").get_json()
    assert body["version_id"] == "7"
    assert body["total"] == 1


def test_tc_issues_swaps_reversed_dates(client, fake_redmine):
    body = client.get("/api/tc_issues?date_from=2026-07-10&date_to=2026-07-01").get_json()
    assert body["date_from"] == "2026-07-01"
    assert body["date_to"] == "2026-07-10"


def test_tc_generate_requires_issue_id(client, fake_redmine):
    assert client.get("/api/tc_generate").status_code == 400


def test_tc_generate_returns_card(client, fake_redmine, monkeypatch):
    monkeypatch.setattr(
        ai, "generate_for_issue", lambda *a, **kw: {"summary": "요약", "cases": [{"priority": "P0"}]}
    )
    body = client.get("/api/tc_generate?issue_id=1").get_json()
    assert body["id"] == 1
    assert body["result"]["cases"][0]["priority"] == "P0"


def test_tc_generate_caches_success_only(client, fake_redmine, monkeypatch):
    calls = {"n": 0}

    def gen(*a, **kw):
        calls["n"] += 1
        return {"error": "일시적 오류"}

    monkeypatch.setattr(ai, "generate_for_issue", gen)
    client.get("/api/tc_generate?issue_id=1")
    client.get("/api/tc_generate?issue_id=1")
    assert calls["n"] == 2  # 실패는 캐시하지 않는다


def test_tc_more(client, fake_redmine, monkeypatch):
    monkeypatch.setattr(ai, "generate_more_for_issue", lambda *a, **kw: {"cases": []})
    resp = client.post("/api/tc_more", json={"issue_id": 1, "existing": ["기존 항목"]})
    assert resp.status_code == 200
    assert resp.get_json()["id"] == 1


def test_project_prompt_roundtrip(client):
    assert client.get("/api/project_prompt?project_id=p1").get_json()["is_custom"] is False

    client.post("/api/project_prompt", json={"project_id": "p1", "prompt": "P1 전용 지침"})
    body = client.get("/api/project_prompt?project_id=p1").get_json()
    assert body["prompt"] == "P1 전용 지침"
    assert body["is_custom"] is True
    assert body["default_prompt"]  # 편집 시드용 기본 지침은 항상 내려준다


def test_sets_crud(client):
    payload = {
        "type": "master",
        "project": "p1",
        "name": "회귀 세트",
        "cases": [{"id": 1, "result": {"cases": [{"status": "pass"}]}}],
    }
    created = client.post("/api/sets", json=payload).get_json()
    set_id = created["id"]
    assert created["summary"]["rows"] == 1

    assert len(client.get("/api/sets?project_id=p1").get_json()["sets"]) == 1
    assert client.get(f"/api/sets/{set_id}").get_json()["name"] == "회귀 세트"

    client.delete(f"/api/sets/{set_id}")
    assert client.get(f"/api/sets/{set_id}").status_code == 404


def test_sets_requires_name(client):
    assert client.post("/api/sets", json={"project": "p1"}).status_code == 400


def test_sets_rejects_bad_id(client):
    assert client.get("/api/sets/..%2F..%2Fsecret").status_code in (400, 404)


def test_export_xlsx(client):
    payload = {
        "mode": "full",
        "title": "회귀",
        "cases": [{"id": 1, "subject": "제목", "result": {"cases": [{"priority": "P0"}]}}],
    }
    resp = client.post("/api/export_xlsx", json=payload)
    assert resp.status_code == 200
    assert "spreadsheetml" in resp.headers["Content-Type"]


@pytest.fixture()
def fake_create_issue(monkeypatch):
    monkeypatch.setattr(redmine, "fetch_trackers", lambda: {"결함": 3})
    calls = []

    def create_issue(project_id, subject, description, tracker_id=None, priority_id=None):
        calls.append({"subject": subject, "description": description, "tracker_id": tracker_id})
        return {"id": 999}

    monkeypatch.setattr(redmine, "create_issue", create_issue)
    tc_api._tracker_ids.clear()
    return calls


DEFECT = {"subject": "로그인 실패 재발", "description": "[재현 절차]\n1. 로그인\n\n[실제 결과]\n500"}


def test_defect_registration_after_preview(client, fake_create_issue):
    preview = client.post("/api/redmine_issue/preview", json=DEFECT).get_json()
    assert preview["subject"] == DEFECT["subject"]
    assert preview["warnings"] == []
    assert fake_create_issue == []  # 미리보기만으로는 등록되지 않는다

    resp = client.post(
        "/api/redmine_issue",
        json={**DEFECT, "confirm_token": preview["confirm_token"], "confirmed": True},
    )
    body = resp.get_json()
    assert body["id"] == 999
    assert body["url"].endswith("/issues/999")
    assert fake_create_issue[0]["tracker_id"] == 3


def test_defect_registration_requires_subject(client):
    assert client.post("/api/redmine_issue", json={"description": "x"}).status_code == 400
    assert client.post("/api/redmine_issue/preview", json={"description": "x"}).status_code == 400


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"confirmed": True}, "미리보기"),  # 토큰 없이 바로 등록
        ({"confirm_token": "0" * 64, "confirmed": True}, "바뀌었습니다"),  # 위조 토큰
    ],
)
def test_defect_registration_rejected_without_valid_preview(client, fake_create_issue, extra, message):
    resp = client.post("/api/redmine_issue", json={**DEFECT, **extra})
    assert resp.status_code == 400
    assert message in resp.get_json()["error"]
    assert fake_create_issue == []


def test_defect_registration_rejects_edits_after_preview(client, fake_create_issue):
    token = client.post("/api/redmine_issue/preview", json=DEFECT).get_json()["confirm_token"]
    edited = {**DEFECT, "description": DEFECT["description"] + " (몰래 추가)"}
    resp = client.post("/api/redmine_issue", json={**edited, "confirm_token": token, "confirmed": True})
    assert resp.status_code == 400
    assert fake_create_issue == []


def test_defect_registration_requires_human_confirmation(client, fake_create_issue):
    token = client.post("/api/redmine_issue/preview", json=DEFECT).get_json()["confirm_token"]
    resp = client.post("/api/redmine_issue", json={**DEFECT, "confirm_token": token})
    assert resp.status_code == 400
    assert fake_create_issue == []


def test_defect_preview_warns_on_empty_sections(client):
    body = {"subject": "x", "description": "[재현 절차]\n\n\n[실제 결과]\n  \n\n(QA 진행)"}
    warnings = client.post("/api/redmine_issue/preview", json=body).get_json()["warnings"]
    assert len(warnings) == 2


def test_backend_status_endpoint(client):
    assert "backend" in client.get("/api/backend_status").get_json()


def test_pdf_endpoint_writes_file(client, fake_redmine, monkeypatch, tmp_path):
    from redmine_report.config import settings

    monkeypatch.setattr(settings, "report_output_dir", tmp_path, raising=False)
    monkeypatch.setattr(reports_api.reporting, "PDFKIT_AVAILABLE", False)

    resp = client.get("/api/pdf?date_from=2026-07-01&date_to=2026-07-10")
    assert resp.status_code == 200
    assert list(tmp_path.glob("*.html"))


def test_responses_carry_basic_security_headers(client):
    # 로컬 도구라도 다른 사이트가 이 화면을 iframe 에 넣지 못해야 한다(클릭재킹).
    resp = client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "same-origin"


def test_oversized_request_is_rejected_before_reading_body(app, client):
    # 본문을 다 읽고 나서 크기를 재면 이미 메모리에 올라간 뒤다.
    limit = app.config["MAX_CONTENT_LENGTH"]
    resp = client.post(
        "/api/auto/runs",
        data=b"x" * (limit + 1),
        content_type="application/octet-stream",
    )
    assert resp.status_code == 413
    assert "error" in resp.get_json()

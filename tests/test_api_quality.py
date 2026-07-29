"""신규 엔드포인트 HTTP 테스트 — 릴리스 준비도 · RTM · 기획서 생성 · 리비전/diff."""

from __future__ import annotations

import pytest

from redmine_report.services import ai, redmine, storage


def _issue(issue_id, tracker="기능", priority="보통", status="신규", subject="이슈"):
    return {
        "id": issue_id,
        "subject": subject,
        "tracker": {"name": tracker},
        "priority": {"name": priority},
        "status": {"name": status},
        "assigned_to": {"name": "홍길동"},
        "created_on": "2026-07-01T01:00:00Z",
        "updated_on": "2026-07-02T01:00:00Z",
    }


def _card(issue_id, statuses, priority="P1"):
    return {
        "id": issue_id,
        "subject": f"이슈 {issue_id}",
        "result": {
            "cases": [
                {"priority": priority, "depth1": "회원", "objective": f"검증 {i}", "status": s}
                for i, s in enumerate(statuses)
            ]
        },
    }


@pytest.fixture()
def version_issues(monkeypatch):
    issues = [_issue(1), _issue(2, tracker="버그", priority="긴급")]
    monkeypatch.setattr(redmine, "fetch_issues_by_version", lambda *a, **kw: issues)
    monkeypatch.setattr(redmine, "fetch_issues", lambda **kw: issues)
    return issues


def _save_run(project="test-project", version="7", cards=None):
    record = storage.build_set(
        {
            "type": "run",
            "project": project,
            "name": "1회차",
            "meta": {"version": version, "versionName": "v1.0"},
            "cases": cards or [_card(1, ["pass", "fail"])],
        }
    )
    storage.write_set(record)
    return record


# ── 릴리스 준비도 ────────────────────────────────────────


def test_release_readiness_requires_version(client, version_issues):
    resp = client.get("/api/release_readiness?project_id=test-project")
    assert resp.status_code == 400
    assert "version_id" in resp.get_json()["error"]


def test_release_readiness_returns_gate(client, version_issues):
    _save_run()
    body = client.get("/api/release_readiness?version_id=7&project_id=test-project").get_json()

    assert body["gate"]["level"] in ("ready", "caution", "blocked")
    assert body["test"]["total"] == 2
    assert body["defects"]["open"] == 1
    assert body["issue_total"] == 2


def test_release_readiness_with_no_runs(client, version_issues):
    """진행 기록이 없어도 500 이 아니라 '진행률 0' 으로 답해야 한다."""
    body = client.get("/api/release_readiness?version_id=7&project_id=test-project").get_json()
    assert body["test"]["total"] == 0
    assert body["test"]["progress"] == 0.0
    assert body["gate"]["level"] == "blocked"


# ── 요구사항 추적 매트릭스 ───────────────────────────────


def test_traceability_by_version(client, version_issues):
    _save_run()
    body = client.get("/api/traceability?version_id=7&project_id=test-project").get_json()

    assert body["summary"]["issues"] == 2
    states = {r["issue_id"]: r["state"] for r in body["rows"]}
    assert states[1] == "실패"     # pass + fail
    assert states[2] == "미작성"


def test_traceability_by_date_range(client, version_issues):
    body = client.get(
        "/api/traceability?date_from=2026-07-01&date_to=2026-07-31&project_id=test-project"
    ).get_json()
    assert body["scope"]["date_from"] == "2026-07-01"
    assert body["summary"]["issues"] == 2


def test_traceability_requires_scope(client, version_issues):
    assert client.get("/api/traceability?project_id=test-project").status_code == 400


def test_traceability_csv_download(client, version_issues):
    _save_run()
    resp = client.get("/api/traceability.csv?version_id=7&project_id=test-project")
    assert resp.status_code == 200
    text = resp.data.decode("utf-8")
    assert text.startswith("﻿")          # 엑셀 한글 깨짐 방지 BOM
    assert "이슈ID" in text


# ── 기획서 기반 생성 ─────────────────────────────────────


def test_tc_from_spec_requires_body(client):
    assert client.post("/api/tc_from_spec", json={"title": "제목"}).status_code == 400


def test_tc_from_spec_returns_card(client, monkeypatch):
    captured = {}

    def fake(title, spec, mode="full", guidance=None, avoid=None, purpose="full"):
        captured.update(title=title, spec=spec, mode=mode, purpose=purpose)
        return {"summary": "요약", "cases": [{"priority": "P0"}]}

    monkeypatch.setattr(ai, "generate_from_spec", fake)

    body = client.post(
        "/api/tc_from_spec",
        json={"title": "로그인 개편", "spec": "요구사항 본문", "card_id": "SPEC-1"},
    ).get_json()

    assert body["id"] == "SPEC-1"
    assert body["subject"] == "로그인 개편"
    assert body["source"] == "spec"
    assert body["result"]["cases"][0]["priority"] == "P0"
    assert captured["spec"] == "요구사항 본문"


def test_extract_spec_from_file(client):
    import io

    data = {"file": (io.BytesIO("로그인 요구사항 문서".encode()), "spec.txt")}
    body = client.post("/api/extract_spec", data=data,
                       content_type="multipart/form-data").get_json()

    assert body["kind"] == "text"
    assert body["source"] == "spec.txt"
    assert "로그인 요구사항" in body["text"]
    assert body["truncated"] is False


def test_extract_spec_truncates_long_file(client):
    import io

    from redmine_report.services import ai

    big = ("가" * (ai.MAX_SPEC_CHARS + 2000)).encode()
    data = {"file": (io.BytesIO(big), "big.txt")}
    body = client.post("/api/extract_spec", data=data,
                       content_type="multipart/form-data").get_json()

    assert body["truncated"] is True
    assert "앞부분만" in body["text"]


def test_extract_spec_rejects_unsupported_file(client):
    import io

    data = {"file": (io.BytesIO(b"MZ"), "virus.exe")}
    resp = client.post("/api/extract_spec", data=data, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "지원하지 않는" in resp.get_json()["error"]


def test_extract_spec_requires_file_or_url(client):
    resp = client.post("/api/extract_spec", json={})
    assert resp.status_code == 400


def test_extract_spec_from_figma(client, monkeypatch):
    from redmine_report.services import extract

    monkeypatch.setattr(
        extract, "from_figma",
        lambda url: {"text": "[로그인 화면]\n이메일", "kind": "figma", "source": "로그인 개편"},
    )
    body = client.post("/api/extract_spec",
                       json={"url": "https://figma.com/design/abc/Spec"}).get_json()
    assert body["kind"] == "figma"
    assert "이메일" in body["text"]


def test_extract_spec_rejects_arbitrary_url(client):
    resp = client.post("/api/extract_spec", json={"url": "http://10.0.0.1/secret"})
    assert resp.status_code == 400
    assert "Figma" in resp.get_json()["error"]


def test_spec_prompt_truncates_long_document():
    prompt = ai.build_spec_prompt("긴 문서", "가" * (ai.MAX_SPEC_CHARS + 500))
    assert "이하 생략" in prompt
    assert len(prompt) < ai.MAX_SPEC_CHARS + 500


def test_generate_from_spec_rejects_empty():
    assert "error" in ai.generate_from_spec("제목", "   ")


# ── 리비전 / diff ────────────────────────────────────────


def _save_master(client, cases, set_id=None):
    payload = {"type": "master", "project": "p1", "name": "원본", "cases": cases}
    if set_id:
        payload["id"] = set_id
    return client.post("/api/sets", json=payload).get_json()


def test_revisions_and_diff_endpoints(client):
    created = _save_master(client, [_card(1, ["untested"])])
    sid = created["id"]

    assert client.get(f"/api/sets/{sid}/revisions").get_json()["revisions"] == []

    _save_master(client, [_card(1, ["untested", "untested"])], set_id=sid)
    revisions = client.get(f"/api/sets/{sid}/revisions").get_json()["revisions"]
    assert len(revisions) == 1

    diff = client.get(f"/api/sets/{sid}/diff?base={revisions[0]['rev']}").get_json()
    assert diff["summary"]["added"] == 1
    assert diff["target"]["rev"] == "current"


def test_diff_requires_base(client):
    created = _save_master(client, [_card(1, ["untested"])])
    assert client.get(f"/api/sets/{created['id']}/diff").status_code == 400


def test_diff_preview_against_inline_cases(client):
    body = client.post(
        "/api/sets/diff_preview",
        json={"before": [_card(1, ["untested"])], "after": [_card(1, ["untested", "untested"])]},
    ).get_json()
    assert body["summary"]["added"] == 1
    assert body["summary"]["has_change"] is True


def test_diff_preview_requires_something(client):
    assert client.post("/api/sets/diff_preview", json={"after": []}).status_code == 400

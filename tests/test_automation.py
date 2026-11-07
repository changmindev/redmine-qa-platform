"""자동화 실행 결과 — JUnit 파싱 · 지난 실행 대비 비교 · 불안정 TC."""

from __future__ import annotations

import io

import pytest

from redmine_report.services import automation


def junit(**statuses: str) -> bytes:
    """{"테스트 이름": "pass|fail|skip|xfail"} → JUnit XML."""
    body = []
    for name, status in statuses.items():
        inner = {
            "pass": "",
            "fail": '<failure message="Expected: 515 Received: 514">trace</failure>',
            "skip": '<skipped message="slow"/>',
            "xfail": '<skipped type="pytest.xfail" message="D-01 이미지 404"/>',
        }[status]
        body.append(f'<testcase classname="customers.spec.ts" name="{name}" time="0.5">{inner}</testcase>')
    return f'<testsuites><testsuite name="e2e">{"".join(body)}</testsuite></testsuites>'.encode()


# ── 파싱 ─────────────────────────────────────────────


def test_parse_maps_each_status():
    cases = {c["name"]: c for c in automation.parse_junit(junit(a="pass", b="fail", c="skip", d="xfail"))}
    assert [cases[k]["status"] for k in "abcd"] == ["pass", "fail", "skip", "xfail"]
    assert cases["b"]["message"] == "Expected: 515 Received: 514"
    assert cases["a"]["key"] == "customers.spec.ts::a"


def test_parse_reads_error_as_fail_and_bare_testsuite_root():
    xml = b'<testsuite><testcase name="t"><error message="boom"/></testcase></testsuite>'
    (case,) = automation.parse_junit(xml)
    assert (case["status"], case["key"]) == ("fail", "t")


@pytest.mark.parametrize("data", [b"not xml", b"<testsuites></testsuites>"])
def test_parse_rejects_non_results(data):
    with pytest.raises(automation.AutomationError):
        automation.parse_junit(data)


def test_suite_name_cannot_escape_store(data_dir):
    with pytest.raises(automation.AutomationError):
        automation.save_run("../etc", junit(a="pass"))


# ── 비교 ─────────────────────────────────────────────


def test_compare_splits_new_still_fixed(data_dir):
    automation.save_run("web", junit(a="pass", b="fail", c="fail", gone="pass"))
    automation.save_run("web", junit(a="fail", b="fail", c="pass", new="fail"))
    diff = automation.compare_latest("web")

    assert [c["name"] for c in diff["new_fail"]] == ["a", "new"]
    assert [c["name"] for c in diff["still_fail"]] == ["b"]
    assert [c["name"] for c in diff["fixed"]] == ["c"]
    assert diff["added"] == ["customers.spec.ts::new"]
    assert diff["removed"] == ["customers.spec.ts::gone"]


def test_xfail_is_not_reported_as_new_failure(data_dir):
    automation.save_run("web", junit(a="pass"))
    automation.save_run("web", junit(a="xfail"))
    assert automation.compare_latest("web")["new_fail"] == []


def test_first_run_has_no_previous(data_dir):
    automation.save_run("web", junit(a="fail"))
    diff = automation.compare_latest("web")
    assert diff["previous_id"] is None
    assert [c["name"] for c in diff["new_fail"]] == ["a"]
    assert diff["added"] == []


def test_runs_saved_in_same_millisecond_do_not_overwrite(data_dir, monkeypatch):
    monkeypatch.setattr(automation.time, "time", lambda: 1_700_000_000.0)
    automation.save_run("web", junit(a="pass"))
    automation.save_run("web", junit(a="fail"))
    assert len(automation.list_runs("web")) == 2


# ── 불안정 TC ─────────────────────────────────────────


def test_flaky_counts_flips_not_failure_rate(data_dir):
    # flaky: P F P P F → 3번 뒤바뀜 / broken: 매번 실패 → 0번 / once: 1번만 뒤바뀜
    seq = {
        "flaky": "pfppf",
        "broken": "fffff",
        "once": "pppff",
    }
    for i in range(5):
        automation.save_run(
            "web", junit(**{k: {"p": "pass", "f": "fail"}[v[i]] for k, v in seq.items()})
        )

    items = automation.flaky("web", window=10, min_flips=2)
    assert [x["name"] for x in items] == ["flaky"]
    assert items[0]["flips"] == 3
    assert items[0]["history"] == "PFPPF"
    assert items[0]["last_message"] == "Expected: 515 Received: 514"


def test_flaky_ignores_skip_and_window_limits_history(data_dir):
    for status in ["fail", "pass", "fail", "skip", "pass", "pass", "pass"]:
        automation.save_run("web", junit(t=status))
    assert automation.flaky("web", window=3, min_flips=1) == []
    (item,) = automation.flaky("web", window=7, min_flips=2)
    assert item["history"] == "FPF·PPP"
    assert item["flips"] == 3


# ── API ──────────────────────────────────────────────


def upload(client, suite, data):
    return client.post(
        "/api/auto/runs",
        data={"suite": suite, "file": (io.BytesIO(data), "results.xml")},
        content_type="multipart/form-data",
    )


def test_upload_returns_comparison_with_previous(client):
    upload(client, "loyalhub-e2e", junit(a="pass"))
    body = upload(client, "loyalhub-e2e", junit(a="fail")).get_json()
    assert body["run"]["counts"]["fail"] == 1
    assert [c["name"] for c in body["compare"]["new_fail"]] == ["a"]

    assert client.get("/api/auto/suites").get_json()["suites"] == ["loyalhub-e2e"]
    assert len(client.get("/api/auto/runs?suite=loyalhub-e2e").get_json()["runs"]) == 2


def test_upload_rejects_bad_input_with_400(client):
    assert client.post("/api/auto/runs", data={"suite": "x"}).status_code == 400
    assert upload(client, "x", b"<html/>").status_code == 400
    assert upload(client, "a b", junit(a="pass")).status_code == 400


def test_flaky_endpoint_validates_numbers(client):
    upload(client, "web", junit(a="pass"))
    assert client.get("/api/auto/flaky?suite=web&window=abc").status_code == 400
    assert client.get("/api/auto/flaky?suite=web").get_json()["items"] == []


def test_automation_page_renders(client):
    assert client.get("/automation").status_code == 200

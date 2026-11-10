"""리포트 HTML 생성 테스트 — 특히 이스케이프 회귀 방지."""

from __future__ import annotations

from redmine_report.services.analyzer import analyze
from redmine_report.services.cache import TTLCache
from redmine_report.services.reporting import build_html


def _stats(issue_factory, status_map, **kw):
    return analyze([issue_factory(**kw)], open_issues=[], status_map=status_map)


def test_build_html_contains_key_sections(issue_factory, status_map):
    html = build_html(_stats(issue_factory, status_map), "2026-07-01", "2026-07-10", "내 프로젝트")

    assert "<!DOCTYPE html>" in html
    assert "내 프로젝트" in html
    assert "미해결 장기 이슈" in html
    assert "우선순위별 평균 처리시간" in html
    assert "이슈 재오픈율" in html


def test_build_html_escapes_issue_subject(issue_factory, status_map):
    """제목에 태그가 들어가도 그대로 삽입되면 안 된다."""
    evil = '<script>alert("xss")</script>'
    stats = analyze(
        [],
        open_issues=[issue_factory(1, subject=evil, created_on="2020-01-01T00:00:00Z")],
        status_map=status_map,
    )
    html = build_html(stats, "2026-07-01", "2026-07-10", "p")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_build_html_escapes_project_name(issue_factory, status_map):
    html = build_html(_stats(issue_factory, status_map), "2026-07-01", "2026-07-10", "<b>P</b>")
    assert "<b>P</b>" not in html


def test_status_headers_come_from_settings(issue_factory, status_map):
    """담당자 표 헤더는 config 의 상태명을 그대로 쓴다(하드코딩 금지)."""
    html = build_html(_stats(issue_factory, status_map), "2026-07-01", "2026-07-10", "p")
    assert "완료(Dev-QA)" in html
    assert "확인Dev-QA" not in html  # 원본의 하드코딩된 잘못된 라벨


def test_ttl_cache_evicts_oldest():
    cache = TTLCache(ttl_seconds=60, max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)

    assert len(cache) == 2
    assert cache.get("a") is None
    assert cache.get("c") == 3


def test_ttl_cache_expires():
    cache = TTLCache(ttl_seconds=0, max_entries=4)
    cache.set("a", 1)
    assert cache.get("a") is None


def test_ttl_cache_get_or_set_calls_factory_once():
    cache = TTLCache(ttl_seconds=60, max_entries=4)
    calls = []

    def factory():
        calls.append(1)
        return "value"

    assert cache.get_or_set("k", factory) == "value"
    assert cache.get_or_set("k", factory) == "value"
    assert len(calls) == 1


def test_pdf_options_block_script_and_local_files(issue_factory, status_map, monkeypatch, data_dir):
    # pdfkit 1.0.0 는 고친 버전이 없다(CVE-2025-26240). wkhtmltopdf 옵션으로 막는다.
    import types

    from redmine_report.config import settings
    from redmine_report.services import reporting

    seen = {}

    def fake_from_string(html_doc, path, options=None):
        seen.update(options or {})
        open(path, "wb").close()

    monkeypatch.setattr(settings, "report_output_dir", data_dir, raising=False)
    monkeypatch.setattr(reporting, "PDFKIT_AVAILABLE", True)
    monkeypatch.setattr(
        reporting, "pdfkit", types.SimpleNamespace(from_string=fake_from_string), raising=False
    )

    out = reporting.generate_report(_stats(issue_factory, status_map), "2026-07-01", "2026-07-10")

    assert out.suffix == ".pdf"
    assert "disable-javascript" in seen
    assert "disable-local-file-access" in seen

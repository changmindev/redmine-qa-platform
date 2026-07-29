"""템플릿 무결성 — 화면을 다시 디자인해도 기능이 조용히 죽지 않도록 지킨다.

tools/check_dom_contract.py 와 같은 검사를 테스트로도 돌린다.
(JS 가 찾는 id 존재 / CSS 클래스 정의 / JS 문법)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from check_dom_contract import TEMPLATE_DIR, check  # noqa: E402

TEMPLATES = sorted(TEMPLATE_DIR.glob("*.html"))


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_template_integrity(template):
    problems = check(template)
    assert not problems, f"{template.name}: " + " / ".join(problems)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_template_links_stylesheets(template):
    """인라인 <style> 대신 공용 스타일시트를 참조해야 한다."""
    source = template.read_text(encoding="utf-8")
    assert "css/base.css" in source


def test_pages_render_without_leftover_template_tags(client):
    for path in ("/", "/testcases"):
        html = client.get(path).data.decode("utf-8")
        leftovers = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", html)
        assert not leftovers, f"{path} 에 치환되지 않은 템플릿 태그: {leftovers[:3]}"


def test_static_css_is_served(client):
    for name in ("base.css", "dashboard.css", "testcases.css"):
        resp = client.get(f"/static/css/{name}")
        assert resp.status_code == 200
        assert len(resp.data) > 1000


def test_dashboard_exposes_status_order(client):
    """상태 색상은 이름에 고정 배정된다 — 서버가 순서를 내려줘야 한다."""
    html = client.get("/").data.decode("utf-8")
    match = re.search(r"const STATUS_ORDER = (\[.*?\]);", html)
    assert match, "STATUS_ORDER 가 렌더링되지 않았습니다."
    assert "\\uc2e0\\uaddc" in match.group(1) or "신규" in match.group(1)


def test_testcases_page_has_new_feature_hooks(client):
    """새로 추가한 기능의 진입점이 화면에 있는지."""
    html = client.get("/testcases").data.decode("utf-8")
    for hook in ('id="spec-text"', 'id="spec-run"', 'id="rev-open"',
                 'id="rev-modal"', 'id="keys-modal"', 'id="show-keys"'):
        assert hook in html, f"{hook} 이(가) 없습니다."


def test_dashboard_has_readiness_and_workload(client):
    html = client.get("/").data.decode("utf-8")
    for hook in ('id="release-card"', 'id="gate-badge"', 'id="readiness-grid"',
                 'id="wl-rows"', 'id="trend-table"', 'id="tracker-table"'):
        assert hook in html, f"{hook} 이(가) 없습니다."

"""HTML 페이지 라우트 + 헬스체크."""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template

from ..config import settings
from ..services import ai

bp = Blueprint("pages", __name__)


@bp.get("/")
def dashboard():
    # 상태 색상은 '이름'에 고정 배정한다. 데이터 순서에 따라 색이 바뀌면
    # 필터를 걸었을 때 남은 항목의 색이 뒤바뀌어 읽는 사람을 오도한다.
    return render_template(
        "dashboard.html",
        default_project=settings.project_id,
        status_order=list(settings.statuses.values()),
        long_term_days=settings.long_term_days,
    )


@bp.get("/testcases")
def testcases():
    return render_template("testcases.html", default_project=settings.project_id)


@bp.get("/healthz")
def healthz():
    """설정이 제대로 됐는지 한눈에 확인 (비밀값은 노출하지 않는다)."""
    return jsonify(
        {
            "ok": True,
            "redmine_url": settings.redmine_url or None,
            "redmine_key_set": bool(settings.redmine_api_key),
            "project_id": settings.project_id or None,
            "generator": ai.backend_status(),
        }
    )


@bp.get("/api/backend_status")
def backend_status():
    """테스트케이스 생성 백엔드(API 키 / claude CLI) 준비 상태."""
    return jsonify(ai.backend_status())

"""릴리스 품질 API — 출시 준비도, 요구사항 추적 매트릭스."""

from __future__ import annotations

import io

from flask import Blueprint, jsonify, request, send_file

from ..services import quality
from .common import BadRequest, optional_date_pair

bp = Blueprint("quality", __name__, url_prefix="/api")


@bp.get("/release_readiness")
def release_readiness():
    """목표 버전 하나의 출시 준비도(신호등 + 근거)."""
    version_id = request.args.get("version_id") or ""
    if not version_id:
        raise BadRequest("version_id 가 필요합니다. 목표 버전을 선택하세요.")
    project_id = request.args.get("project_id") or None
    return jsonify(quality.release_readiness(project_id, version_id))


def _rtm_scope():
    version_id = request.args.get("version_id") or None
    date_pair = optional_date_pair(request.args.get("date_from"), request.args.get("date_to"))
    if not version_id and not date_pair:
        raise BadRequest("version_id 또는 date_from·date_to 가 필요합니다.")
    date_from, date_to = date_pair if date_pair else (None, None)
    return request.args.get("project_id") or None, version_id, date_from, date_to


@bp.get("/traceability")
def traceability():
    """요구사항(이슈) ↔ 테스트케이스 ↔ 실행결과 ↔ 결함 매트릭스."""
    project_id, version_id, date_from, date_to = _rtm_scope()
    return jsonify(quality.traceability(project_id, version_id, date_from, date_to))


@bp.get("/traceability.csv")
def traceability_csv():
    """RTM CSV 내려받기 (엑셀에서 한글이 깨지지 않도록 BOM 포함)."""
    project_id, version_id, date_from, date_to = _rtm_scope()
    data = quality.traceability(project_id, version_id, date_from, date_to)
    payload = "﻿" + quality.traceability_csv(data)
    stream = io.BytesIO(payload.encode("utf-8"))
    label = version_id or f"{date_from}_{date_to}"
    return send_file(
        stream,
        as_attachment=True,
        download_name=f"traceability_{label}.csv",
        mimetype="text/csv; charset=utf-8",
    )

"""자동화 실행 결과 API — 업로드 · 목록 · 지난 실행 대비 비교 · 불안정 TC."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..services import automation
from .common import BadRequest

bp = Blueprint("automation", __name__, url_prefix="/api/auto")


@bp.post("/runs")
def upload_run():
    """JUnit XML 한 개를 실행 1회로 저장하고, 바로 앞 실행과의 비교를 함께 돌려준다.

    CI 에서도 그대로 부를 수 있게 multipart 업로드만 받는다:
      curl -F suite=loyalhub-e2e -F file=@results.xml http://<허브>/api/auto/runs
    """
    upload = request.files.get("file")
    if upload is None:
        raise BadRequest("JUnit XML 파일을 첨부하세요(file).")
    suite = request.form.get("suite") or ""
    record = automation.save_run(suite, upload.read(), label=request.form.get("label") or "")
    return jsonify(
        {
            "run": {k: record[k] for k in ("id", "suite", "label", "created_at", "counts")},
            "compare": automation.compare_latest(record["suite"], record["id"]),
        }
    )


@bp.get("/suites")
def suites():
    return jsonify({"suites": automation.list_suites()})


@bp.get("/runs")
def runs():
    suite = request.args.get("suite") or ""
    return jsonify({"suite": suite, "runs": automation.list_runs(suite)})


@bp.get("/compare")
def compare():
    suite = request.args.get("suite") or ""
    return jsonify(automation.compare_latest(suite, request.args.get("run_id") or None))


@bp.get("/flaky")
def flaky():
    suite = request.args.get("suite") or ""
    try:
        window = int(request.args.get("window") or 10)
        min_flips = int(request.args.get("min_flips") or 2)
    except ValueError as exc:
        raise BadRequest("window·min_flips 는 숫자여야 합니다.") from exc
    return jsonify(
        {
            "suite": suite,
            "window": window,
            "min_flips": min_flips,
            "items": automation.flaky(suite, window=window, min_flips=max(1, min_flips)),
        }
    )

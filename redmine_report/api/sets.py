"""테스트 세트 API — 원본(master) / 진행 기록(run) 저장·조회·삭제."""

from __future__ import annotations

from flask import Blueprint, jsonify, request  # noqa: F401  (request 는 아래 라우트에서 사용)

from ..services import storage, tcdiff
from .common import BadRequest, json_body

bp = Blueprint("sets", __name__, url_prefix="/api/sets")


@bp.get("")
def list_sets():
    return jsonify(
        {
            "sets": storage.list_sets(
                project_id=request.args.get("project_id") or "",
                set_type=request.args.get("type"),
            )
        }
    )


@bp.post("")
def save_set():
    payload = json_body()
    if not str(payload.get("name") or "").strip():
        raise BadRequest("세트 이름을 입력하세요.")
    existing = storage.read_set(payload["id"]) if payload.get("id") else {}
    record = storage.build_set(payload, existing)
    storage.write_set(record)
    return jsonify({"ok": True, "id": record["id"], "summary": storage.summarize(record)})


@bp.get("/<sid>")
def get_set(sid: str):
    record = storage.read_set(sid)
    if not record:
        return jsonify({"error": "세트를 찾을 수 없습니다."}), 404
    return jsonify(record)


@bp.delete("/<sid>")
def remove_set(sid: str):
    storage.delete_set(sid)
    return jsonify({"ok": True})


@bp.get("/<sid>/revisions")
def revisions(sid: str):
    """원본 세트의 변경 이력(최신순)."""
    return jsonify({"id": sid, "revisions": storage.list_revisions(sid)})


@bp.get("/<sid>/diff")
def diff(sid: str):
    """두 리비전 사이의 변경 내역. target 기본값은 현재 저장본."""
    base = request.args.get("base")
    if not base:
        raise BadRequest("base 리비전을 지정하세요.")
    target = request.args.get("target") or "current"
    before = storage.read_revision(sid, base)
    after = storage.read_revision(sid, target)
    return jsonify(
        {
            "id": sid,
            "base": {"rev": before.get("rev", base), "saved_at": before.get("saved_at", "")},
            "target": {"rev": after.get("rev", target), "saved_at": after.get("saved_at", "")},
            **tcdiff.diff_sets(before.get("cases") or [], after.get("cases") or []),
        }
    )


@bp.post("/diff_preview")
def diff_preview():
    """저장하기 전 현재 작업본과 특정 리비전을 비교한다(재생성 직후 확인용)."""
    payload = json_body()
    before = payload.get("before")
    if before is None:
        set_id = payload.get("id")
        rev = payload.get("rev") or "current"
        if not set_id:
            raise BadRequest("비교할 리비전(id·rev) 또는 before 케이스가 필요합니다.")
        before = storage.read_revision(set_id, rev).get("cases") or []
    return jsonify(tcdiff.diff_sets(before, payload.get("after") or []))

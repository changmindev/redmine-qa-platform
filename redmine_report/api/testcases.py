"""테스트케이스 생성기 API — 이슈 목록/생성/추가생성/지침/엑셀/결함등록."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets

from flask import Blueprint, jsonify, request, send_file

from ..config import settings
from ..services import ai, excel, extract, redmine, storage
from .common import (
    BadRequest,
    generation_cache,
    issue_meta,
    issue_store,
    json_body,
    mode_of,
    optional_date_pair,
    range_key,
    version_key,
)

log = logging.getLogger(__name__)

bp = Blueprint("testcases", __name__, url_prefix="/api")

_tracker_ids: dict[str, int] = {}


def _resolve_tracker_id(name: str | None) -> int | None:
    if not name:
        return None
    if name not in _tracker_ids:
        try:
            _tracker_ids.update(redmine.fetch_trackers())
        except redmine.RedmineError as exc:
            log.warning("트래커 목록 조회 실패: %s", exc)
    return _tracker_ids.get(name)


def _cached_issue(issue_id, date_from: str | None, date_to: str | None, project_id: str | None) -> dict:
    """목록 조회 때 받아둔 이슈를 재사용하고, 코멘트가 없으면 상세로 보강한다."""
    issue = None
    if date_from and date_to:
        bucket = issue_store.get(range_key(date_from, date_to, project_id)) or {}
        issue = bucket.get(str(issue_id))
    if issue is None or not issue.get("journals"):
        issue = redmine.fetch_issue(issue_id)
    return issue


@bp.get("/tc_issues")
def tc_issues():
    """이슈 '목록'만 조회(생성 없음).

    기간과 목표 버전(version_id)을 AND(교집합)로 결합한다.
      · 둘 다 지정 → 그 버전에 속하면서 기간 내에 생성된 이슈
      · 기간만     → 기간 내 이슈 전체
      · 버전만     → 그 버전에 배정된 이슈 전체(날짜 무관, 릴리스 QA용)
    """
    date_pair = optional_date_pair(request.args.get("date_from"), request.args.get("date_to"))
    version_id = request.args.get("version_id") or None
    project_id = request.args.get("project_id") or None

    if not date_pair and not version_id:
        raise BadRequest("date_from·date_to 또는 version_id 파라미터가 필요합니다.")

    payload: dict = {"gen_warn": settings.testcase_max_issues, "truncated": False}

    if date_pair:
        date_from, date_to = date_pair
        issues = redmine.fetch_issues(
            date_from=date_from, date_to=date_to, version_id=version_id, project_id=project_id
        )
        issue_store.set(
            range_key(date_from, date_to, project_id), {str(i.get("id")): i for i in issues}
        )
        payload.update(date_from=date_from, date_to=date_to)
        if version_id:
            payload["version_id"] = version_id
    else:
        issues = redmine.fetch_issues_by_version(version_id, project_id)
        issue_store.set(version_key(version_id, project_id), {str(i.get("id")): i for i in issues})
        payload["version_id"] = version_id

    payload.update(
        total=len(issues),
        analyzed=len(issues),
        issues=[issue_meta(i) for i in issues],
    )
    return jsonify(payload)


@bp.route("/tc_generate", methods=["GET", "POST"])
def tc_generate():
    """이슈 한 건의 테스트케이스(full) 또는 체크리스트(checklist) 생성.

    POST body 의 avoid(다른 이슈에서 이미 만든 목록)를 주면 중복을 피한다.
    """
    if request.method == "POST":
        data = json_body()
        get = data.get
        avoid = data.get("avoid") or []
    else:
        get = request.args.get
        avoid = []

    issue_id = get("issue_id")
    if not issue_id:
        raise BadRequest("issue_id 가 필요합니다.")

    mode = mode_of(get("mode"))
    purpose = ai.normalize_purpose(get("purpose"))
    project_id = get("project_id") or None
    refresh = str(get("refresh") or "") in ("1", "true", "True")
    date_pair = optional_date_pair(get("date_from"), get("date_to"))
    date_from, date_to = date_pair if date_pair else (None, None)

    guidance = storage.get_project_prompt(project_id)
    # 지침이 바뀌면 결과도 달라지므로 캐시 키에 지침 해시를 포함한다.
    # 중복 회피 컨텍스트가 있으면 매번 결과가 달라지므로 캐시를 쓰지 않는다.
    use_cache = not avoid
    guide_hash = hashlib.sha1(guidance.encode("utf-8")).hexdigest()[:12]
    cache_key = (str(issue_id), mode, purpose, guide_hash)
    if use_cache and not refresh:
        cached = generation_cache.get(cache_key)
        if cached is not None:
            return jsonify(cached)

    issue = _cached_issue(issue_id, date_from, date_to, project_id)
    card = {
        **issue_meta(issue),
        "result": ai.generate_for_issue(
            issue, mode=mode, avoid=avoid, guidance=guidance, purpose=purpose
        ),
    }
    if use_cache and "error" not in card["result"]:
        generation_cache.set(cache_key, card)
    return jsonify(card)


@bp.post("/extract_spec")
def extract_spec():
    """첨부 파일 / Figma 주소에서 기획 내용을 텍스트로 뽑아 돌려준다.

    바로 생성으로 넘기지 않고 텍스트만 돌려주는 이유:
    사용자가 **무엇이 모델에 들어가는지 눈으로 보고 고칠 수 있어야** 하기 때문이다.
    화면에서는 이 결과를 기획서 입력칸에 채워 넣는다.
    """
    upload = request.files.get("file")
    if upload is not None:
        filename = upload.filename or "첨부파일"
        try:
            data = upload.read()
        except OSError as exc:
            raise BadRequest(f"파일을 읽지 못했습니다: {exc}") from exc
        result = extract.from_upload(filename, data)
    else:
        data = json_body()
        url = str(data.get("url") or "").strip()
        if not url:
            raise BadRequest("파일을 첨부하거나 Figma 주소를 입력하세요.")
        result = extract.from_url(url)

    text = result["text"]
    truncated = len(text) > ai.MAX_SPEC_CHARS
    if truncated:
        text = text[: ai.MAX_SPEC_CHARS] + "\n…(문서가 길어 앞부분만 사용합니다)"

    log.info("기획 문서 추출: %s (%s, %d자)", result["source"], result["kind"], len(text))
    return jsonify(
        {
            "text": text,
            "kind": result["kind"],
            "source": result["source"],
            "chars": len(text),
            "truncated": truncated,
        }
    )


@bp.post("/tc_from_spec")
def tc_from_spec():
    """기획서/요구사항 문서로 테스트케이스를 생성한다(개발 전 Shift-Left).

    이슈가 아직 없어도 되므로, 카드 id 는 `SPEC-<n>` 같은 문자열을 프론트가 지정한다.
    출력 형식은 이슈 기반 생성과 동일해 같은 표·같은 내보내기를 그대로 쓴다.
    """
    data = json_body()
    spec_text = str(data.get("spec") or "").strip()
    if not spec_text:
        raise BadRequest("기획서 내용을 입력하세요.")

    title = str(data.get("title") or "").strip() or "기획서"
    mode = mode_of(data.get("mode"))
    project_id = data.get("project_id") or None
    card_id = str(data.get("card_id") or "").strip() or "SPEC-1"

    result = ai.generate_from_spec(
        title,
        spec_text,
        mode=mode,
        guidance=storage.get_project_prompt(project_id),
        avoid=data.get("avoid") or [],
        purpose=ai.normalize_purpose(data.get("purpose")),
    )
    return jsonify(
        {
            "id": card_id,
            "subject": title,
            "tracker": "기획서",
            "priority": "",
            "status": "설계",
            "source": "spec",
            "result": result,
        }
    )


@bp.post("/tc_more")
def tc_more():
    """이미 있는 항목과 겹치지 않는 '새' 항목만 추가 생성한다."""
    data = json_body()
    issue_id = data.get("issue_id")
    if not issue_id:
        raise BadRequest("issue_id 가 필요합니다.")

    mode = mode_of(data.get("mode"))
    project_id = data.get("project_id") or None
    date_pair = optional_date_pair(data.get("date_from"), data.get("date_to"))
    date_from, date_to = date_pair if date_pair else (None, None)

    issue = _cached_issue(issue_id, date_from, date_to, project_id)
    result = ai.generate_more_for_issue(
        issue,
        mode=mode,
        existing=data.get("existing") or [],
        guidance=storage.get_project_prompt(project_id),
        purpose=ai.normalize_purpose(data.get("purpose")),
    )
    return jsonify({"id": issue.get("id"), "result": result})


@bp.route("/project_prompt", methods=["GET", "POST"])
def project_prompt():
    """프로젝트별 '생성 지침' 조회/저장 (서버 파일에 저장 → 팀 공유)."""
    if request.method == "POST":
        data = json_body()
        project_id = data.get("project_id")
        storage.save_project_prompt(project_id, data.get("prompt", ""))
        return jsonify({"ok": True, "prompt": storage.get_project_prompt(project_id)})

    project_id = request.args.get("project_id")
    saved = storage.get_project_prompt(project_id)
    return jsonify(
        {
            "project_id": project_id or "",
            "prompt": saved,  # 이 프로젝트에 저장된 전용 지침(없으면 "")
            "is_custom": bool(saved.strip()),
            "default_prompt": ai.default_guidance("full"),  # 미설정 시 편집 시드
        }
    )


@bp.post("/export_xlsx")
def export_xlsx():
    """현재 TC/체크리스트(+실행결과)를 엑셀(xlsx)로 내보낸다."""
    data = json_body()
    title = (str(data.get("title") or "testcases")).strip() or "testcases"
    stream = excel.build_workbook(data.get("cases") or [], mode_of(data.get("mode")))
    return send_file(
        stream,
        as_attachment=True,
        download_name=f"{title}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ── 결함 등록 2단계 확인 ────────────────────────────────
#   1단계 /redmine_issue/preview : 최종 제목·설명을 서버가 정리해 돌려주고 확인 토큰을 준다.
#   2단계 /redmine_issue          : 그 토큰과 '사람이 확인했다'는 표시가 있어야만 등록한다.
#
#   토큰은 미리보기한 내용 그대로에 서명한 값이라, 미리보기 뒤에 한 글자라도 바뀌면 거절된다.
#   화면을 거치지 않는 호출(스크립트·AI 도구)도 같은 두 단계를 밟아야 하므로,
#   "확인 없이 바로 등록"하는 경로가 서버에 아예 없다.
#   서명 키는 프로세스가 뜰 때마다 새로 만든다 — 서버를 재시작하면 미리보기를 다시 해야 한다.
_PREVIEW_KEY = secrets.token_bytes(32)


def _defect_fields(data: dict) -> dict:
    subject = str(data.get("subject") or "").strip()
    if not subject:
        raise BadRequest("제목을 입력하세요.")
    return {
        "project_id": str(data.get("project_id") or ""),
        "subject": subject,
        "description": str(data.get("description") or "").replace("\r\n", "\n"),
        "tracker": str(data.get("tracker") or settings.defect_tracker_name or ""),
    }


def _preview_token(fields: dict) -> str:
    message = json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hmac.new(_PREVIEW_KEY, message, hashlib.sha256).hexdigest()


def _defect_warnings(description: str) -> list[str]:
    """등록 전에 사람이 한 번 더 볼 곳. 막지는 않는다 — 판단은 확인하는 사람이 한다."""
    warnings = []
    for section, hint in (
        ("[재현 절차]", "재현 절차가 비어 있습니다 — 개발자가 재현하지 못하면 결함이 돌아옵니다."),
        ("[실제 결과]", "실제 결과가 비어 있습니다 — 기대 결과와 무엇이 달랐는지 적어 주세요."),
    ):
        if section in description:
            body = description.split(section, 1)[1].split("\n\n", 1)[0]
            if not body.strip():
                warnings.append(hint)
    return warnings


@bp.post("/redmine_issue/preview")
def redmine_issue_preview():
    """1단계: 등록될 내용을 그대로 돌려주고 확인 토큰을 발급한다(아직 등록하지 않는다)."""
    fields = _defect_fields(json_body())
    return jsonify(
        {
            **fields,
            "warnings": _defect_warnings(fields["description"]),
            "confirm_token": _preview_token(fields),
        }
    )


@bp.post("/redmine_issue")
def redmine_issue():
    """2단계: 미리보기에서 사람이 확인한 내용만 Redmine 결함으로 등록한다."""
    data = json_body()
    fields = _defect_fields(data)
    token = str(data.get("confirm_token") or "")
    if not token:
        raise BadRequest("미리보기에서 내용을 확인한 뒤 등록하세요.")
    if not hmac.compare_digest(token, _preview_token(fields)):
        raise BadRequest("미리보기 뒤에 내용이 바뀌었습니다. 다시 미리보기 하세요.")
    if data.get("confirmed") is not True:
        raise BadRequest("'직접 확인했습니다'에 체크해야 등록됩니다.")

    issue = redmine.create_issue(
        fields["project_id"] or None,
        fields["subject"],
        fields["description"],
        tracker_id=_resolve_tracker_id(fields["tracker"]),
    )
    issue_id = issue.get("id")
    log.info("결함 등록 #%s (확인 후 등록)", issue_id)
    return jsonify(
        {
            "ok": True,
            "id": issue_id,
            "url": redmine.issue_url(issue_id),
            "tracker": fields["tracker"],
        }
    )

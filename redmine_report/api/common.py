"""API 계층 공용 유틸 — 요청 파라미터 검증과 캐시.

원본은 `int(request.args.get("months"))` 처럼 검증 없이 형변환해 잘못된
쿼리스트링 하나로 500 이 났고, 예외 문자열을 그대로 클라이언트에 노출했다.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from flask import request

from ..config import settings
from ..services.cache import TTLCache

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: 기간 리포트 통계 캐시 (조회 → 분석은 비싸고 결과가 잘 안 바뀐다)
report_cache = TTLCache(settings.cache_ttl_seconds, settings.cache_max_entries)
#: 이슈 단건 생성 결과 캐시
generation_cache = TTLCache(settings.cache_ttl_seconds, settings.cache_max_entries * 8)
#: 목록 조회로 받아둔 이슈 원본 (생성 시 재조회를 아낀다)
issue_store = TTLCache(settings.cache_ttl_seconds, settings.cache_max_entries)


class BadRequest(ValueError):
    """400 으로 변환되는 입력 오류."""


def clear_all_caches() -> None:
    report_cache.clear()
    generation_cache.clear()
    issue_store.clear()


def valid_date(value: str | None, field: str) -> str:
    if not value or not _DATE_RE.match(value):
        raise BadRequest(f"{field} 는 YYYY-MM-DD 형식이어야 합니다.")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise BadRequest(f"{field} 가 올바른 날짜가 아닙니다: {value}") from exc
    return value


def date_range(date_from: str | None, date_to: str | None, months_raw: str | None) -> tuple[str, str]:
    """날짜 범위를 확정한다. 둘 다 없으면 months(기본 1) 로 계산."""
    if date_from or date_to:
        start = valid_date(date_from, "date_from")
        end = valid_date(date_to, "date_to")
        return (end, start) if start > end else (start, end)

    months = 1
    if months_raw:
        try:
            months = int(months_raw)
        except ValueError as exc:
            raise BadRequest("months 는 정수여야 합니다.") from exc
    months = max(1, min(months, 60))
    today = date.today()
    return (today - timedelta(days=30 * months)).isoformat(), today.isoformat()


def optional_date_pair(date_from: str | None, date_to: str | None) -> tuple[str, str] | None:
    """둘 다 있을 때만 검증된 (from, to) 를 돌려준다. 하나만 있으면 오류."""
    if not date_from and not date_to:
        return None
    start = valid_date(date_from, "date_from")
    end = valid_date(date_to, "date_to")
    return (end, start) if start > end else (start, end)


def mode_of(value: str | None) -> str:
    return "checklist" if value == "checklist" else "full"


def json_body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def range_key(date_from: str, date_to: str, project_id: str | None) -> str:
    return f"{project_id or 'default'}|{date_from}|{date_to}"


def version_key(version_id: str, project_id: str | None) -> str:
    return f"{project_id or 'default'}|ver|{version_id}"


def issue_meta(issue: dict) -> dict:
    return {
        "id": issue.get("id"),
        "subject": issue.get("subject", ""),
        "tracker": (issue.get("tracker") or {}).get("name", ""),
        "priority": (issue.get("priority") or {}).get("name", ""),
        "status": (issue.get("status") or {}).get("name", ""),
    }

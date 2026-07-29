"""Redmine REST API 클라이언트.

원본 대비 개선점
- requests.Session + 재시도(백오프)로 일시적 네트워크 오류에 견딘다.
- 페이지네이션 무한 루프 방지(빈 응답 / 진행 없음 감지).
- URL 결합을 urljoin 기반으로 정리(설정에 슬래시가 있어도 `//` 가 생기지 않음).
- 호출 실패를 RedmineError 로 감싸 상위(API 계층)에서 일관되게 처리한다.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote, urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..config import settings

log = logging.getLogger(__name__)

#: 페이지네이션 안전장치 — 이 이상은 설정 실수로 간주하고 중단한다.
_MAX_PAGES = 200


class RedmineError(RuntimeError):
    """Redmine 호출 실패. status_code 가 있으면 원 응답 코드."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


_session = _build_session()


def _url(path: str) -> str:
    settings.require_redmine()
    return urljoin(settings.redmine_url + "/", path.lstrip("/"))


def _headers() -> dict[str, str]:
    return {
        "X-Redmine-API-Key": settings.redmine_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _request(method: str, path: str, **kwargs) -> dict:
    url = _url(path)
    try:
        resp = _session.request(
            method,
            url,
            headers=_headers(),
            timeout=settings.http_timeout,
            verify=settings.verify_ssl,
            **kwargs,
        )
    except requests.RequestException as exc:
        log.warning("Redmine 요청 실패 %s %s: %s", method, path, exc)
        raise RedmineError(f"Redmine 서버에 연결할 수 없습니다: {exc}") from exc

    if resp.status_code >= 400:
        detail = resp.text[:300].strip()
        log.warning("Redmine %s %s → HTTP %s %s", method, path, resp.status_code, detail)
        raise RedmineError(
            f"Redmine 응답 오류(HTTP {resp.status_code}): {detail or resp.reason}",
            status_code=resp.status_code,
        )
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise RedmineError("Redmine 응답을 JSON으로 해석할 수 없습니다.") from exc


def _paginate_issues(params: dict) -> list[dict]:
    """이슈 목록을 전 페이지 수집한다.

    Redmine 이 total_count 를 주지 않거나 빈 배치를 반환하는 경우에도
    루프가 끝나도록 방어한다(원본은 이 경우 무한 루프 가능).
    """
    issues: list[dict] = []
    limit = max(1, min(100, settings.page_size))
    offset = 0
    for _ in range(_MAX_PAGES):
        page_params = {**params, "offset": offset, "limit": limit}
        data = _request("GET", "issues.json", params=page_params)
        batch = data.get("issues") or []
        issues.extend(batch)
        total = int(data.get("total_count") or 0)
        offset += limit
        if not batch or offset >= total:
            break
    else:
        log.warning("이슈 페이지네이션이 %s 페이지를 초과해 중단했습니다.", _MAX_PAGES)
    return issues


def _project(project_id: str | None) -> str:
    pid = (project_id or settings.project_id or "").strip()
    if not pid:
        raise RedmineError("프로젝트 ID가 지정되지 않았습니다.")
    return pid


def fetch_issues(
    date_from: str | None = None,
    date_to: str | None = None,
    months: int | None = None,
    version_id: str | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """기간(생성일 기준)으로 이슈를 조회한다. version_id 를 주면 교집합."""
    if date_from and date_to:
        since, until = date_from, date_to
    else:
        today = date.today()
        since = (today - timedelta(days=30 * (months or 1))).isoformat()
        until = today.isoformat()

    params = {
        "project_id": _project(project_id),
        "status_id": "*",
        "created_on": f"><{since}|{until}",
        "include": "journals",
    }
    if version_id and version_id != "all":
        params["fixed_version_id"] = version_id
    return _paginate_issues(params)


def fetch_issues_by_version(version_id: str, project_id: str | None = None) -> list[dict]:
    """목표 버전(마일스톤)에 배정된 이슈 전체(열림+닫힘, 날짜 무관)."""
    return _paginate_issues(
        {
            "project_id": _project(project_id),
            "status_id": "*",
            "fixed_version_id": version_id,
        }
    )


def fetch_open_issues(version_id: str | None = None, project_id: str | None = None) -> list[dict]:
    """현재 미완료(오픈) 이슈 전체."""
    params = {"project_id": _project(project_id), "status_id": "open"}
    if version_id and version_id != "all":
        params["fixed_version_id"] = version_id
    return _paginate_issues(params)


def fetch_issue(issue_id: Any) -> dict:
    """이슈 상세. 코멘트·첨부·연관이슈를 포함해 테스트케이스 생성 품질을 높인다."""
    data = _request(
        "GET",
        f"issues/{quote(str(issue_id))}.json",
        params={"include": "journals,attachments,relations"},
    )
    return data.get("issue", {})


def fetch_statuses() -> dict[str, dict]:
    """상태 ID → {name, is_closed} 매핑."""
    data = _request("GET", "issue_statuses.json")
    return {
        str(s["id"]): {"name": s["name"], "is_closed": bool(s.get("is_closed", False))}
        for s in data.get("issue_statuses", [])
    }


def fetch_versions(project_id: str | None = None) -> list[dict]:
    """프로젝트의 버전(마일스톤) 목록 — 최신순."""
    pid = _project(project_id)
    data = _request("GET", f"projects/{quote(pid)}/versions.json")
    versions = data.get("versions", [])
    return sorted(versions, key=lambda v: v.get("created_on", ""), reverse=True)


def fetch_trackers() -> dict[str, int]:
    """트래커 이름 → id (이슈 생성 시 tracker_id 필요)."""
    data = _request("GET", "trackers.json")
    return {t["name"]: t["id"] for t in data.get("trackers", [])}


def fetch_project_info(project_id: str | None = None) -> dict:
    pid = _project(project_id)
    data = _request("GET", f"projects/{quote(pid)}.json")
    return data.get("project", {})


def create_issue(
    project_id: str | None,
    subject: str,
    description: str,
    tracker_id: int | None = None,
    priority_id: int | None = None,
) -> dict:
    """이슈를 생성한다(실패 케이스 → 결함 등록용)."""
    issue: dict[str, Any] = {
        "project_id": _project(project_id),
        "subject": subject,
        "description": description,
    }
    if tracker_id:
        issue["tracker_id"] = tracker_id
    if priority_id:
        issue["priority_id"] = priority_id
    data = _request("POST", "issues.json", json={"issue": issue})
    return data.get("issue", {})


def issue_url(issue_id: Any) -> str:
    return f"{settings.redmine_url}/issues/{issue_id}"

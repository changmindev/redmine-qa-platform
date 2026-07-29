"""대시보드용 API — 버전 목록, 기간 리포트, PDF 내려받기, 캐시 비우기."""

from __future__ import annotations

from flask import Blueprint, jsonify, request, send_file

from ..services import redmine, reporting
from ..services.analyzer import analyze
from .common import clear_all_caches, date_range, report_cache

bp = Blueprint("reports", __name__, url_prefix="/api")


def _load(date_from: str, date_to: str, version_id: str, project_id: str | None) -> dict:
    key = f"{project_id or 'default'}|{date_from}|{date_to}|{version_id or 'all'}"

    def build() -> dict:
        issues = redmine.fetch_issues(
            date_from=date_from, date_to=date_to, version_id=version_id, project_id=project_id
        )
        open_issues = redmine.fetch_open_issues(version_id=version_id, project_id=project_id)
        status_map = redmine.fetch_statuses()
        project = redmine.fetch_project_info(project_id=project_id)
        return {
            "stats": analyze(issues, open_issues=open_issues, status_map=status_map),
            "project": project.get("name", "프로젝트"),
        }

    return report_cache.get_or_set(key, build)


def _params() -> tuple[str, str, str, str | None]:
    date_from, date_to = date_range(
        request.args.get("date_from"),
        request.args.get("date_to"),
        request.args.get("months"),
    )
    return (
        date_from,
        date_to,
        request.args.get("version_id", "all"),
        request.args.get("project_id") or None,
    )


@bp.get("/versions")
def versions():
    project_id = request.args.get("project_id") or None
    return jsonify({"versions": redmine.fetch_versions(project_id=project_id)})


@bp.get("/report")
def report():
    date_from, date_to, version_id, project_id = _params()
    data = _load(date_from, date_to, version_id, project_id)
    return jsonify({**data, "date_from": date_from, "date_to": date_to})


@bp.get("/pdf")
def pdf():
    date_from, date_to, version_id, project_id = _params()
    data = _load(date_from, date_to, version_id, project_id)
    path = reporting.generate_report(
        data["stats"], date_from, date_to, data["project"], version_id
    )
    return send_file(path, as_attachment=True, download_name=path.name)


@bp.post("/clear_cache")
def clear_cache():
    clear_all_caches()
    return jsonify({"ok": True})

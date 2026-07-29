"""팀 공유 저장소 — 프로젝트별 생성 지침 + 테스트 세트.

- 원본(master)   : 생성된 '테스트 전' TC. 프로젝트별로 여러 개 이름 저장.
- 진행 기록(run) : 원본을 복제해 P/F 결과를 담은 기록. 이어서 수정 가능(공유).
  ※ UI 표기는 '진행 기록', 내부 type/폴더는 "run"/"runs" 유지(기존 저장물 호환).

각 세트는 data/store/<masters|runs>/<id>.json 파일 1개로 저장한다
(파일 1개 = 세트 1개라 동시 저장 충돌이 최소화된다).
쓰기는 임시 파일 + os.replace 로 원자적으로 수행해, 중간에 죽어도 파일이 깨지지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from ..config import settings
from .timeutil import local_now_str

log = logging.getLogger(__name__)

#: 세트 ID 허용 형식 — 경로 조작(../) 방지
_ID_RE = re.compile(r"^[mr]_[A-Za-z0-9._-]{1,120}$")

_STATUS_KEYS = ("pass", "fail", "na", "blocked", "untested")


class StorageError(RuntimeError):
    """저장소 읽기/쓰기 실패."""


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise StorageError(f"파일을 저장하지 못했습니다: {exc}") from exc


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as exc:
        log.warning("JSON 읽기 실패 %s: %s", path, exc)
        return default


# ── 프로젝트별 생성 지침 ─────────────────────────────────────


def _prompts() -> dict[str, str]:
    data = _read_json(settings.prompts_path, {})
    return data if isinstance(data, dict) else {}


def _prompt_key(project_id: Any) -> str:
    return str(project_id or "").strip() or "__default__"


def get_project_prompt(project_id: Any) -> str:
    return str(_prompts().get(_prompt_key(project_id), "") or "")


def save_project_prompt(project_id: Any, text: str) -> None:
    """빈 값으로 저장하면 항목을 삭제한다(→ 기본 지침 사용)."""
    data = _prompts()
    key = _prompt_key(project_id)
    if str(text or "").strip():
        data[key] = str(text)
    else:
        data.pop(key, None)
    _write_json_atomic(settings.prompts_path, data)


# ── 테스트 세트(원본 / 진행 기록) ────────────────────────────


def kind_of(set_id: str) -> str:
    return "runs" if str(set_id).startswith("r_") else "masters"


def validate_id(set_id: str) -> str:
    sid = str(set_id or "")
    if not _ID_RE.match(sid):
        raise StorageError("세트 ID 형식이 올바르지 않습니다.")
    return sid


def _store_sub(kind: str) -> Path:
    directory = settings.store_dir / kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def set_path(set_id: str) -> Path:
    sid = validate_id(set_id)
    return _store_sub(kind_of(sid)) / f"{sid}.json"


def new_set_id(set_type: str, project: str | None) -> str:
    prefix = "r_" if set_type == "run" else "m_"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(project or "proj")).strip("-").lower() or "proj"
    return f"{prefix}{slug}_{int(time.time() * 1000)}"


def read_set(set_id: str) -> dict:
    try:
        path = set_path(set_id)
    except StorageError:
        return {}
    data = _read_json(path, {})
    return data if isinstance(data, dict) else {}


def write_set(record: dict, keep_revision: bool = True) -> None:
    """세트를 저장한다. 원본(master)은 저장할 때마다 리비전 스냅샷을 남긴다."""
    if keep_revision and record.get("type") == "master":
        previous = read_set(record["id"])
        if previous and previous.get("cases") != record.get("cases"):
            _save_revision(previous)
    _write_json_atomic(set_path(record["id"]), record)


# ── 리비전(변경 이력) ────────────────────────────────────
#   원본을 덮어쓰기 전 상태를 스냅샷으로 남겨 "무엇이 바뀌었는지" 비교할 수 있게 한다.
#   무한히 쌓이면 폴더가 커지므로 최근 N개만 유지한다.
MAX_REVISIONS = 20


def _revision_dir(set_id: str) -> Path:
    directory = settings.store_dir / "revisions" / validate_id(set_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _save_revision(record: dict) -> None:
    directory = _revision_dir(record["id"])
    stamp = str(int(time.time() * 1000))
    snapshot = {
        "rev": stamp,
        "saved_at": record.get("updated_at") or local_now_str(),
        "name": record.get("name", ""),
        "mode": record.get("mode", "full"),
        "cases": record.get("cases") or [],
    }
    _write_json_atomic(directory / f"{stamp}.json", snapshot)

    existing = sorted(directory.glob("*.json"))
    for stale in existing[:-MAX_REVISIONS]:
        stale.unlink(missing_ok=True)


def list_revisions(set_id: str) -> list[dict]:
    """리비전 목록(최신순). 케이스 본문은 빼고 요약만."""
    try:
        directory = _revision_dir(set_id)
    except StorageError:
        return []
    items = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        snapshot = _read_json(path, {})
        if not snapshot:
            continue
        cases = snapshot.get("cases") or []
        rows = sum(
            len((c.get("result") or {}).get("cases") or (c.get("result") or {}).get("checklist") or [])
            for c in cases
        )
        items.append(
            {
                "rev": snapshot.get("rev", path.stem),
                "saved_at": snapshot.get("saved_at", ""),
                "name": snapshot.get("name", ""),
                "issues": len(cases),
                "rows": rows,
            }
        )
    return items


def read_revision(set_id: str, rev: str) -> dict:
    """특정 리비전 스냅샷. rev='current' 면 현재 저장본을 돌려준다."""
    if rev == "current":
        record = read_set(set_id)
        return {"rev": "current", "saved_at": record.get("updated_at", ""), "cases": record.get("cases") or []}
    safe = re.sub(r"[^0-9]", "", str(rev))
    if not safe:
        raise StorageError("리비전 번호가 올바르지 않습니다.")
    snapshot = _read_json(_revision_dir(set_id) / f"{safe}.json", {})
    if not snapshot:
        raise StorageError("해당 리비전을 찾을 수 없습니다.")
    return snapshot


def delete_set(set_id: str) -> None:
    """세트와 그 리비전 이력을 함께 지운다(고아 폴더가 남지 않도록)."""
    try:
        set_path(set_id).unlink(missing_ok=True)
        revisions = settings.store_dir / "revisions" / validate_id(set_id)
        if revisions.is_dir():
            for snapshot in revisions.glob("*.json"):
                snapshot.unlink(missing_ok=True)
            revisions.rmdir()
    except OSError as exc:
        raise StorageError(f"세트를 삭제하지 못했습니다: {exc}") from exc


def build_set(payload: dict, existing: dict | None = None) -> dict:
    """저장 요청(payload)을 정규화된 세트 레코드로 만든다."""
    existing = existing or {}
    set_type = "run" if payload.get("type") == "run" else "master"
    set_id = payload.get("id") or new_set_id(set_type, payload.get("project"))
    return {
        "id": validate_id(set_id),
        "type": set_type,
        "project": str(payload.get("project") or ""),
        "name": str(payload.get("name") or "").strip(),
        "mode": payload.get("mode") or "full",
        "meta": payload.get("meta") or {},
        "tester": str(payload.get("tester") or ""),
        "master_id": payload.get("master_id") or existing.get("master_id", ""),
        "master_name": payload.get("master_name") or existing.get("master_name", ""),
        "cases": payload.get("cases") or [],
        "created_at": existing.get("created_at") or local_now_str(),
        "updated_at": local_now_str(),
    }


def summarize(record: dict) -> dict:
    """목록 표시용 요약(케이스 원문은 빼고 개수/상태만)."""
    counts = dict.fromkeys(_STATUS_KEYS, 0)
    rows = 0
    for card in record.get("cases") or []:
        result = (card or {}).get("result") or {}
        items = result.get("cases")
        if not isinstance(items, list):
            items = result.get("checklist") or []
        for row in items:
            rows += 1
            status = (row or {}).get("status") or "untested"
            counts[status] = counts.get(status, 0) + 1
    return {
        "id": record.get("id"),
        "type": record.get("type"),
        "project": record.get("project", ""),
        "name": record.get("name", ""),
        "mode": record.get("mode", "full"),
        "tester": record.get("tester", ""),
        "master_name": record.get("master_name", ""),
        "issues": len(record.get("cases") or []),
        "rows": rows,
        "status_counts": counts,
        "created_at": record.get("created_at", ""),
        "updated_at": record.get("updated_at", ""),
    }


def list_sets(project_id: str | None = None, set_type: str | None = None) -> list[dict]:
    kinds = (
        ["masters"] if set_type == "master" else ["runs"] if set_type == "run" else ["masters", "runs"]
    )
    items = []
    for kind in kinds:
        for entry in sorted(_store_sub(kind).glob("*.json")):
            record = read_set(entry.stem)
            if not record:
                continue
            if project_id and record.get("project", "") != project_id:
                continue
            items.append(summarize(record))
    items.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return items

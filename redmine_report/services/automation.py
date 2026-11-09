"""자동화 실행 결과 — JUnit XML 수집 · 지난 실행 대비 비교 · 불안정 TC 모음.

사람이 수행한 진행 기록(run)과 따로 둔다. 자동화 결과는 하루에도 여러 번 쌓이고
형식(JUnit XML)이 정해져 있어서, 수동 진행 기록의 P/F 열에 섞으면 둘 다 읽기 어려워진다.

  data/store/auto_runs/<suite>/<run_id>.json   — 실행 1회 = 파일 1개

JUnit XML 을 고른 이유: Playwright(`--reporter=junit`)·pytest(`--junitxml`)·
WebdriverIO 모두 별도 플러그인 없이 내보낸다. 도구마다 파서를 따로 두지 않아도 된다.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from ..config import settings
from .storage import StorageError, _read_json, _write_json_atomic
from .timeutil import local_now_str

#: 스위트 이름 허용 형식 — 폴더 이름이 되므로 경로 조작(../)을 막는다
_SUITE_RE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")
_RUN_RE = re.compile(r"^a_[0-9]{10,16}$")

#: 실행 1회에 받을 최대 XML 크기. 보통 수백 KB 이며, 이보다 크면 로그가 통째로 들어온 경우다.
MAX_XML_BYTES = 10 * 1024 * 1024

#: 판정 상태. xfail(알려진 결함으로 고정해 둔 실패)은 fail 과 섞지 않는다 —
#: 섞으면 '이미 아는 결함'이 매번 '새 실패'로 올라와 진짜 새 실패가 묻힌다.
STATUSES = ("pass", "fail", "skip", "xfail")


class AutomationError(StorageError):
    """자동화 결과 처리 실패(형식 오류 등). 화면에는 400 으로 보인다."""


# ── 수집 ──────────────────────────────────────────────


def validate_suite(suite: str) -> str:
    name = str(suite or "").strip()
    if not _SUITE_RE.match(name):
        raise AutomationError("스위트 이름은 영문·숫자·. _ - 만, 60자 이내로 입력하세요.")
    return name


def _case_status(case: ET.Element) -> tuple[str, str]:
    """<testcase> 하나의 상태와 실패 메시지."""
    for tag in ("failure", "error"):
        node = case.find(tag)
        if node is not None:
            return "fail", (node.get("message") or node.text or "").strip()[:500]
    node = case.find("skipped")
    if node is not None:
        message = (node.get("message") or node.text or "").strip()
        # pytest 는 xfail 을 skipped(type="pytest.xfail") 로 내보낸다.
        if "xfail" in (node.get("type") or "") or message.lower().startswith("xfail"):
            return "xfail", message[:500]
        return "skip", message[:500]
    return "pass", ""


def parse_junit(data: bytes) -> list[dict]:
    """JUnit XML → 케이스 목록. 같은 키가 두 번 나오면 마지막 결과를 쓴다."""
    if len(data) > MAX_XML_BYTES:
        raise AutomationError("결과 파일이 너무 큽니다(10MB 초과).")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise AutomationError(f"JUnit XML 로 읽지 못했습니다: {exc}") from exc

    cases: dict[str, dict] = {}
    for node in root.iter("testcase"):
        name = (node.get("name") or "").strip()
        if not name:
            continue
        classname = (node.get("classname") or "").strip()
        key = f"{classname}::{name}" if classname else name
        status, message = _case_status(node)
        try:
            duration = round(float(node.get("time") or 0), 3)
        except ValueError:
            duration = 0.0
        cases[key] = {
            "key": key,
            "name": name,
            "group": classname,
            "status": status,
            "message": message,
            "duration": duration,
        }
    if not cases:
        raise AutomationError("파일에 <testcase> 가 없습니다. JUnit XML 결과인지 확인하세요.")
    return list(cases.values())


def _counts(cases: list[dict]) -> dict:
    counts = dict.fromkeys(STATUSES, 0)
    for case in cases:
        counts[case["status"]] = counts.get(case["status"], 0) + 1
    return counts


# ── 저장 ──────────────────────────────────────────────


def _suite_dir(suite: str) -> Path:
    directory = settings.store_dir / "auto_runs" / validate_suite(suite)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_run(suite: str, data: bytes, *, label: str = "") -> dict:
    cases = parse_junit(data)
    directory = _suite_dir(suite)
    # 파일 이름 순서 = 실행 순서다. CI 가 같은 밀리초에 두 번 올려도 덮어쓰지 않게 한 칸씩 민다.
    stamp = int(time.time() * 1000)
    while (directory / f"a_{stamp}.json").exists():
        stamp += 1
    run_id = f"a_{stamp}"
    record = {
        "id": run_id,
        "suite": validate_suite(suite),
        "label": str(label or "").strip()[:80],
        "created_at": local_now_str(),
        "counts": _counts(cases),
        "cases": cases,
    }
    _write_json_atomic(directory / f"{run_id}.json", record)
    return record


def list_suites() -> list[str]:
    base = settings.store_dir / "auto_runs"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and _SUITE_RE.match(p.name))


def list_runs(suite: str) -> list[dict]:
    """실행 목록(오래된 순). 케이스 본문은 빼고 요약만."""
    items = []
    for path in sorted(_suite_dir(suite).glob("a_*.json")):
        record = _read_json(path, {})
        if record:
            items.append({k: record.get(k) for k in ("id", "suite", "label", "created_at", "counts")})
    return items


def read_run(suite: str, run_id: str) -> dict:
    if not _RUN_RE.match(str(run_id or "")):
        raise AutomationError("실행 ID 형식이 올바르지 않습니다.")
    record = _read_json(_suite_dir(suite) / f"{run_id}.json", {})
    if not record:
        raise AutomationError("해당 실행을 찾을 수 없습니다.")
    return record


def _load_runs(suite: str) -> list[dict]:
    return [read_run(suite, item["id"]) for item in list_runs(suite)]


# ── 지난 실행 대비 비교 ─────────────────────────────────


def compare(previous: dict | None, current: dict) -> dict:
    """두 실행을 비교해 '사람이 먼저 봐야 할 것'부터 나눈다.

    new_fail    : 지난번엔 통과(또는 없던 TC)였는데 이번에 실패 → 가장 먼저 볼 것
    still_fail  : 지난번에도 실패 → 이미 알려진 실패, 결함이 등록됐는지만 확인
    fixed       : 지난번엔 실패였는데 이번에 통과 → 결함을 닫아도 되는지 확인
    added/removed : TC 자체가 생기거나 사라짐 → 실패 0건이어도 범위가 줄었을 수 있다
    """
    before = {c["key"]: c for c in (previous or {}).get("cases") or []}
    now = {c["key"]: c for c in current.get("cases") or []}

    new_fail, still_fail, fixed = [], [], []
    for key, case in now.items():
        old = before.get(key)
        if case["status"] == "fail":
            (still_fail if old and old["status"] == "fail" else new_fail).append(case)
        elif case["status"] == "pass" and old and old["status"] == "fail":
            fixed.append(case)

    return {
        "previous_id": (previous or {}).get("id"),
        "current_id": current.get("id"),
        "new_fail": new_fail,
        "still_fail": still_fail,
        "fixed": fixed,
        "added": sorted(set(now) - set(before)) if previous else [],
        "removed": sorted(set(before) - set(now)),
    }


def compare_latest(suite: str, run_id: str | None = None) -> dict:
    """지정한 실행(없으면 최신)을 그 바로 앞 실행과 비교한다."""
    runs = list_runs(suite)
    if not runs:
        raise AutomationError("이 스위트에는 아직 올린 실행 결과가 없습니다.")
    ids = [r["id"] for r in runs]
    index = ids.index(run_id) if run_id in ids else len(ids) - 1
    current = read_run(suite, ids[index])
    previous = read_run(suite, ids[index - 1]) if index > 0 else None
    return compare(previous, current)


# ── 불안정 TC 모음 ─────────────────────────────────────


def flaky(suite: str, window: int = 10, min_flips: int = 2) -> list[dict]:
    """최근 `window` 번 실행에서 통과↔실패가 `min_flips` 번 이상 뒤바뀐 TC.

    '실패율'이 아니라 '뒤바뀐 횟수'로 센다. 매번 실패하는 TC 는 불안정한 게 아니라
    고장 난 것이고(still_fail 로 이미 보인다), 가끔만 실패하는 TC 가 재시도에 묻히기 쉽다.
    skip·xfail 은 판정을 하지 않은 것이라 뒤바뀜 계산에서 건너뛴다.
    """
    window = max(2, min(int(window), 100))
    runs = _load_runs(suite)[-window:]
    history: dict[str, list[str]] = {}
    names: dict[str, dict] = {}
    for index, run in enumerate(runs):
        for case in run.get("cases") or []:
            history.setdefault(case["key"], ["-"] * len(runs))[index] = case["status"]
            names[case["key"]] = case

    items = []
    for key, marks in history.items():
        judged = [m for m in marks if m in ("pass", "fail")]
        flips = sum(1 for a, b in zip(judged, judged[1:], strict=False) if a != b)
        if flips >= min_flips:
            fails = judged.count("fail")
            message = ""
            for run in reversed(runs):
                hit = next((c for c in run.get("cases") or [] if c["key"] == key), None)
                if hit and hit["status"] == "fail":
                    message = hit["message"]
                    break
            items.append(
                {
                    "key": key,
                    "name": names[key]["name"],
                    "group": names[key]["group"],
                    "flips": flips,
                    "fails": fails,
                    "judged": len(judged),
                    "history": "".join({"pass": "P", "fail": "F"}.get(m, "·") for m in marks),
                    "last_message": message,
                }
            )
    items.sort(key=lambda x: (-x["flips"], -x["fails"], x["key"]))
    return items

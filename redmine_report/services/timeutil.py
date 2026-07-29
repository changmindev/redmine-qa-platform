"""시간대 처리.

Redmine 은 `2026-07-01T12:34:56Z`(UTC)로 시각을 돌려준다.
원본 코드는 이를 naive datetime 으로 파싱한 뒤 로컬 `datetime.now()` 와 비교했다.
KST 기준으로 최대 9시간 오차가 생겨 '경과일'과 '일자별 집계'가 어긋난다.
여기서는 모두 aware datetime 으로 다루고, 표시용 날짜는 설정된 시간대로 변환한다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..config import settings

log = logging.getLogger(__name__)

_ISO_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d")


def _resolve_tz() -> timezone:
    name = settings.display_timezone
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)  # type: ignore[return-value]
    except Exception:
        # Windows 등 tzdata 가 없는 환경 — KST 고정 오프셋으로 폴백.
        log.debug("시간대 %s 를 찾을 수 없어 UTC+9 로 폴백합니다.", name)
        return timezone(timedelta(hours=9))


DISPLAY_TZ = _resolve_tz()


def parse_dt(value: str | None) -> datetime | None:
    """Redmine 시각 문자열 → tz-aware datetime(UTC). 실패 시 None."""
    if not value:
        return None
    for fmt in _ISO_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_date(dt: datetime | None) -> str:
    """표시 시간대 기준 'YYYY-MM-DD'."""
    return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d") if dt else ""


def local_month(dt: datetime | None) -> str:
    return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m") if dt else ""


def local_now_str(fmt: str = "%Y-%m-%d %H:%M") -> str:
    return now_utc().astimezone(DISPLAY_TZ).strftime(fmt)


def days_between(start: datetime | None, end: datetime | None = None) -> int | None:
    if not start:
        return None
    return ((end or now_utc()) - start).days

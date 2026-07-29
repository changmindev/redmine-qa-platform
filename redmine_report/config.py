"""애플리케이션 설정.

모든 설정은 환경변수(또는 프로젝트 루트의 `.env` 파일)에서 읽는다.
비밀값(Redmine API 키 등)은 절대 소스코드에 하드코딩하지 않는다.

사용법:
    from redmine_report.config import settings
    settings.redmine_url
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:  # python-dotenv 는 선택 의존성 — 없으면 순수 환경변수만 사용
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover - 설치 환경에 따라 다름
    pass


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_set(key: str, default: set[str]) -> set[str]:
    raw = _env(key)
    if not raw:
        return set(default)
    return {p.strip() for p in raw.split(",") if p.strip()}


class ConfigError(RuntimeError):
    """필수 설정이 비어 있을 때 발생."""


@dataclass
class Settings:
    """환경변수에서 읽어 만든 애플리케이션 설정.

    frozen 이 아닌 이유: 테스트에서 개별 항목만 바꿔 끼울 수 있어야 한다.
    런타임에는 어디서도 값을 바꾸지 않는다.
    """

    # ── Redmine ────────────────────────────────────────────────
    redmine_url: str = field(default_factory=lambda: _env("REDMINE_URL").rstrip("/"))
    redmine_api_key: str = field(default_factory=lambda: _env("REDMINE_API_KEY"))
    project_id: str = field(default_factory=lambda: _env("REDMINE_PROJECT_ID"))
    verify_ssl: bool = field(default_factory=lambda: _env_bool("REDMINE_VERIFY_SSL", True))
    http_timeout: int = field(default_factory=lambda: _env_int("REDMINE_HTTP_TIMEOUT", 30))
    page_size: int = field(default_factory=lambda: _env_int("REDMINE_PAGE_SIZE", 100))

    # ── Claude(Anthropic) ──────────────────────────────────────
    # 비워두면 로컬에 설치된 `claude` CLI(구독 인증)로 호출한다.
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-opus-5"))
    # 확장 사고(thinking)가 기본 활성이므로 max_tokens 에 여유를 둔다(사고+본문 합산 상한).
    anthropic_max_tokens: int = field(default_factory=lambda: _env_int("ANTHROPIC_MAX_TOKENS", 16000))
    anthropic_effort: str = field(default_factory=lambda: _env("ANTHROPIC_EFFORT", "high"))
    claude_cli: str = field(default_factory=lambda: _env("CLAUDE_CLI", "claude"))
    claude_cli_timeout: int = field(default_factory=lambda: _env_int("CLAUDE_CLI_TIMEOUT", 300))

    # ── 기획서 첨부 ────────────────────────────────────────────
    # Figma > Settings > Security > Personal access tokens 에서 발급.
    # 없으면 Figma 주소는 읽지 못하고, 파일 첨부만 동작한다.
    figma_token: str = field(default_factory=lambda: _env("FIGMA_TOKEN"))
    max_upload_mb: int = field(default_factory=lambda: _env_int("MAX_UPLOAD_MB", 10))

    # ── 도메인 규칙 ────────────────────────────────────────────
    # Redmine에 등록된 상태명과 '정확히' 일치해야 집계된다.
    status_new: str = field(default_factory=lambda: _env("STATUS_NEW", "신규"))
    status_progress: str = field(default_factory=lambda: _env("STATUS_PROGRESS", "진행중"))
    status_resolved: str = field(default_factory=lambda: _env("STATUS_RESOLVED", "작업완료"))
    status_qa: str = field(default_factory=lambda: _env("STATUS_QA", "완료(Dev-QA)"))

    bug_trackers: set[str] = field(
        default_factory=lambda: _env_set("BUG_TRACKERS", {"버그", "Bug", "결함", "Defect"})
    )
    defect_tracker_name: str = field(default_factory=lambda: _env("DEFECT_TRACKER_NAME", "결함"))
    long_term_days: int = field(default_factory=lambda: _env_int("LONG_TERM_DAYS", 14))
    testcase_max_issues: int = field(default_factory=lambda: _env_int("TESTCASE_MAX_ISSUES", 20))

    # ── 앱/저장 경로 ───────────────────────────────────────────
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR") or (BASE_DIR / "data")))
    report_output_dir: Path = field(
        default_factory=lambda: Path(_env("REPORT_OUTPUT_DIR") or (BASE_DIR / "reports"))
    )
    cache_ttl_seconds: int = field(default_factory=lambda: _env_int("CACHE_TTL_SECONDS", 600))
    cache_max_entries: int = field(default_factory=lambda: _env_int("CACHE_MAX_ENTRIES", 64))

    # Redmine 은 UTC 로 시각을 돌려준다. 일자별 집계·경과일 계산은 이 표시 시간대 기준.
    display_timezone: str = field(default_factory=lambda: _env("DISPLAY_TIMEZONE", "Asia/Seoul"))

    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 5000))
    debug: bool = field(default_factory=lambda: _env_bool("FLASK_DEBUG", False))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())

    # ── 파생 값 ────────────────────────────────────────────────
    @property
    def statuses(self) -> dict[str, str]:
        """고정 영문 키 → Redmine 상태명."""
        return {
            "new": self.status_new,
            "progress": self.status_progress,
            "resolved": self.status_resolved,
            "qa": self.status_qa,
        }

    @property
    def open_statuses(self) -> set[str]:
        return {self.status_new, self.status_progress}

    @property
    def closed_statuses(self) -> set[str]:
        return {self.status_resolved, self.status_qa}

    @property
    def prompts_path(self) -> Path:
        return self.data_dir / "project_prompts.json"

    @property
    def store_dir(self) -> Path:
        return self.data_dir / "store"

    def require_redmine(self) -> None:
        """Redmine 호출 직전에 필수 설정을 검증한다."""
        missing = [
            name
            for name, value in (
                ("REDMINE_URL", self.redmine_url),
                ("REDMINE_API_KEY", self.redmine_api_key),
                ("REDMINE_PROJECT_ID", self.project_id),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"환경변수 {', '.join(missing)} 가 설정되지 않았습니다. "
                ".env.example 을 .env 로 복사한 뒤 값을 채우세요."
            )


settings = Settings()

"""테스트 공통 픽스처.

실제 Redmine / Claude 는 절대 호출하지 않는다. 모든 외부 호출은 monkeypatch 로 대체한다.
설정은 임시 디렉터리를 바라보게 해서 사용자의 data/ 를 건드리지 않는다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 앱 임포트 전에 환경변수를 세팅해야 settings 가 이 값으로 만들어진다.
os.environ.setdefault("REDMINE_URL", "https://redmine.test")
os.environ.setdefault("REDMINE_API_KEY", "test-key")
os.environ.setdefault("REDMINE_PROJECT_ID", "test-project")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """settings.data_dir 를 임시 폴더로 돌린다."""
    from redmine_report.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    return tmp_path


@pytest.fixture()
def app(data_dir):
    from redmine_report import create_app

    application = create_app(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    from redmine_report.api.common import clear_all_caches

    clear_all_caches()
    return app.test_client()


@pytest.fixture()
def issue_factory():
    """테스트용 Redmine 이슈 dict 를 만든다."""

    def make(
        issue_id=1,
        subject="로그인 실패",
        status="신규",
        tracker="버그",
        priority="보통",
        assignee="홍길동",
        created_on="2026-07-01T01:00:00Z",
        updated_on="2026-07-05T01:00:00Z",
        journals=None,
    ):
        return {
            "id": issue_id,
            "subject": subject,
            "status": {"name": status},
            "tracker": {"name": tracker},
            "priority": {"name": priority},
            "assigned_to": {"name": assignee},
            "created_on": created_on,
            "updated_on": updated_on,
            "journals": journals or [],
        }

    return make


@pytest.fixture()
def status_map():
    """상태 ID → {name, is_closed}. 1=신규, 2=진행중, 5=작업완료(닫힘)."""
    return {
        "1": {"name": "신규", "is_closed": False},
        "2": {"name": "진행중", "is_closed": False},
        "5": {"name": "작업완료", "is_closed": True},
    }

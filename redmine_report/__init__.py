"""Redmine QA 리포트 & 테스트케이스 생성기 — Flask 애플리케이션 팩토리."""

from __future__ import annotations

import logging
from contextlib import suppress
from pathlib import Path

from flask import Flask, jsonify, request, url_for
from werkzeug.exceptions import HTTPException

from .api import BLUEPRINTS
from .config import ConfigError, settings
from .services.ai import GenerationError
from .services.excel import ExcelUnavailable
from .services.extract import ExtractError
from .services.redmine import RedmineError
from .services.storage import StorageError

__version__ = "1.0.0"

log = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # 요청마다 찍히는 urllib3 재시도 경고는 노이즈가 크다.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _register_error_handlers(app: Flask) -> None:
    """예외를 일관된 JSON 오류로 변환한다.

    화면(프론트)은 응답의 `error` 필드를 그대로 사용자에게 보여준다.
    로그인 없는 소규모 팀용 도구라 원인 메시지는 노출하되, 예기치 못한 예외는
    스택트레이스를 서버 로그에만 남기고 일반 메시지를 돌려준다.
    """

    def _json_error(message: str, status: int):
        if request.path.startswith("/api/"):
            return jsonify({"error": message}), status
        return message, status

    from .api.common import BadRequest

    @app.errorhandler(BadRequest)
    def _bad_request(exc: BadRequest):
        return _json_error(str(exc), 400)

    @app.errorhandler(ConfigError)
    def _config_error(exc: ConfigError):
        log.error("설정 오류: %s", exc)
        return _json_error(str(exc), 500)

    @app.errorhandler(RedmineError)
    def _redmine_error(exc: RedmineError):
        status = 502 if exc.status_code and exc.status_code >= 500 else 400
        message = str(exc)
        if exc.status_code == 422:
            message += " (트래커명·필수 필드·권한을 확인하세요)"
        elif exc.status_code in (401, 403):
            message += " (REDMINE_API_KEY 의 권한을 확인하세요)"
        return _json_error(message, status)

    @app.errorhandler(GenerationError)
    def _generation_error(exc: GenerationError):
        return _json_error(str(exc), 502)

    @app.errorhandler(ExtractError)
    def _extract_error(exc: ExtractError):
        # 첨부 처리 실패는 대부분 사용자가 고칠 수 있는 문제(형식·용량·토큰)다.
        return _json_error(str(exc), 400)

    @app.errorhandler(StorageError)
    def _storage_error(exc: StorageError):
        return _json_error(str(exc), 400)

    @app.errorhandler(ExcelUnavailable)
    def _excel_error(exc: ExcelUnavailable):
        return _json_error(str(exc), 500)

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        # 404/405 등 프레임워크가 만든 응답은 그대로 두되, API 경로는 JSON 으로 돌려준다.
        return _json_error(exc.description or exc.name, exc.code or 500)

    @app.errorhandler(Exception)
    def _unexpected(exc: Exception):
        log.exception("처리되지 않은 오류 (%s %s)", request.method, request.path)
        return _json_error("서버 내부 오류가 발생했습니다. 서버 로그를 확인하세요.", 500)


def create_app(**overrides) -> Flask:
    """Flask 앱을 만들어 반환한다 (테스트에서도 이 함수를 쓴다)."""
    _configure_logging()
    app = Flask(__name__)
    app.config.update(
        JSON_SORT_KEYS=False,
        # 템플릿을 메모리에 캐시하지 않는다.
        # 이게 꺼져 있으면 화면을 고쳐도 서버를 껐다 켜기 전까지 예전 화면이 그대로 나온다.
        TEMPLATES_AUTO_RELOAD=True,
        # 업로드 크기 검사는 서비스 계층에도 있지만, 그건 본문을 다 읽은 뒤다.
        # 여기서 먼저 막아야 아주 큰 요청을 메모리에 올리지 않는다(여유 1MB).
        MAX_CONTENT_LENGTH=(max(settings.max_upload_mb, 10) + 1) * 1024 * 1024,
        **overrides,
    )

    # CSS 를 고쳤는데 브라우저가 예전 파일을 계속 쓰는 걸 막는다.
    #
    # 앱 버전(1.0.0)을 붙이면 안 된다 — 버전은 그대로 두고 CSS 만 고치는 일이 대부분이라
    # 주소가 안 바뀌고, 브라우저는 캐시에 있는 옛날 파일을 계속 쓴다.
    # 파일의 수정 시각을 붙이면 파일이 바뀔 때마다 주소가 저절로 달라진다.
    @app.context_processor
    def _asset_helpers():
        def static_url(filename: str) -> str:
            stamp = 0
            if app.static_folder:
                # 파일이 없거나 읽지 못해도 화면은 떠야 하므로 조용히 넘긴다.
                with suppress(OSError):
                    stamp = int((Path(app.static_folder) / filename).stat().st_mtime)
            return url_for("static", filename=filename, v=stamp)

        return {"static_url": static_url}

    # 로그인 없는 로컬 도구라도, 사용자가 같은 브라우저로 연 다른 사이트가
    # 이 화면을 iframe 에 넣거나 응답 형식을 추측하게 두지 않는다.
    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    settings.data_dir.mkdir(parents=True, exist_ok=True)

    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)
    _register_error_handlers(app)

    log.info(
        "앱 초기화 완료 (redmine=%s, project=%s, data_dir=%s)",
        settings.redmine_url or "(미설정)",
        settings.project_id or "(미설정)",
        settings.data_dir,
    )
    return app


__all__ = ["create_app", "__version__"]

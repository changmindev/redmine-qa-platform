"""실행 진입점.

    python app.py          # 개발 서버 (기본 http://127.0.0.1:5000)
    start.bat              # 윈도우에서 더블클릭

운영 배포는 WSGI 서버를 쓴다:
    waitress-serve --port=5000 "redmine_report:create_app()"
"""

from redmine_report import create_app
from redmine_report.config import settings

app = create_app()

if __name__ == "__main__":
    print(f"  대시보드          : http://{settings.host}:{settings.port}/")
    print(f"  테스트케이스 생성기: http://{settings.host}:{settings.port}/testcases")
    print(f"  자동화 결과        : http://{settings.host}:{settings.port}/automation")
    app.run(host=settings.host, port=settings.port, debug=settings.debug)

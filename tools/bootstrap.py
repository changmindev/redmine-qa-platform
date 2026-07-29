"""처음 실행을 도와주는 설치 스크립트 (start.bat 이 호출한다).

왜 배치 파일이 아니라 파이썬인가
--------------------------------
cmd.exe 는 .bat 파일의 바이트를 '현재 콘솔 코드페이지'로 읽는다.
한국어 Windows 는 보통 949, 어떤 환경은 65001(UTF-8)이라 어느 인코딩으로 저장해도
누군가의 화면에서는 한글이 깨진다. 그래서 배치 파일에는 영문만 두고,
한글 안내는 전부 여기(파이썬)에서 출력한다.
파이썬은 콘솔 코드페이지에 맞춰 알아서 인코딩하므로 어디서든 안 깨진다.

    python tools/bootstrap.py          # 준비 후 서버 실행
    python tools/bootstrap.py --setup  # 준비만 하고 종료
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
ENV_FILE = ROOT / ".env"
ENV_SAMPLE = ROOT / ".env.example"

LINE = "=" * 52


def say(message: str = "") -> None:
    """콘솔이 표현하지 못하는 글자가 있어도 죽지 않게 출력한다."""
    encoding = sys.stdout.encoding or "utf-8"
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def fail(message: str, hint: str = "") -> None:
    say()
    say(f"[오류] {message}")
    if hint:
        say(f"       {hint}")
    say()
    input("엔터를 누르면 창이 닫힙니다... ")
    sys.exit(1)


def ensure_venv() -> None:
    if VENV_PY.exists():
        return
    say("[최초 1회] 가상환경을 만듭니다. 잠시만 기다려 주세요...")
    result = subprocess.run([sys.executable, "-m", "venv", str(VENV)])
    if result.returncode != 0 or not VENV_PY.exists():
        fail("가상환경을 만들지 못했습니다.", "Python 설치 상태를 확인해 주세요.")

    say("[최초 1회] 필요한 패키지를 설치합니다. (인터넷 연결 필요)")
    subprocess.run([str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip", "-q"])
    result = subprocess.run([str(VENV_PY), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])
    if result.returncode != 0:
        fail("패키지 설치에 실패했습니다.", "인터넷 연결이나 사내 프록시 설정을 확인해 주세요.")
    say("[완료] 설치가 끝났습니다.")
    say()


def ensure_env() -> None:
    if ENV_FILE.exists():
        return
    if not ENV_SAMPLE.exists():
        fail(".env.example 파일이 없습니다.", "프로젝트 폴더가 온전한지 확인해 주세요.")

    shutil.copyfile(ENV_SAMPLE, ENV_FILE)
    say("[최초 1회] 설정 파일(.env)을 만들었습니다.")
    say()
    say("  편집기가 열립니다. 아래 세 가지를 채우고 저장한 뒤 창을 닫아 주세요.")
    say("    REDMINE_URL          Redmine 주소")
    say("    REDMINE_API_KEY      Redmine 개인설정 > API 액세스 키")
    say("    REDMINE_PROJECT_ID   프로젝트 주소의 영문 ID")
    say()
    # OS 마다 기본 편집기를 여는 방법이 다르다. 실패해도 경로만 알려주고 계속 진행한다.
    opener = None
    if os.name == "nt":
        opener = ["notepad", str(ENV_FILE)]
    elif sys.platform == "darwin":
        opener = ["open", "-t", str(ENV_FILE)]
    if opener:
        try:
            subprocess.run(opener)
        except OSError:
            say(f"  편집기를 열지 못했습니다. 직접 열어 주세요: {ENV_FILE}")
    else:
        say(f"  파일 위치: {ENV_FILE}")
    say()


def show_banner() -> None:
    say(LINE)
    say("   Redmine QA 플랫폼")
    say(LINE)
    say()


def show_urls() -> None:
    say("브라우저에서 아래 주소로 접속하세요.")
    say("   리포트         http://localhost:5000")
    say("   테스트케이스    http://localhost:5000/testcases")
    say("   설정 점검      http://localhost:5000/healthz")
    say()
    say("종료하려면 이 창을 닫거나 Ctrl+C 를 누르세요.")
    say()


def main() -> int:
    show_banner()
    ensure_venv()
    ensure_env()

    if "--setup" in sys.argv:
        say("준비가 끝났습니다.")
        return 0

    show_urls()
    try:
        return subprocess.run([str(VENV_PY), str(ROOT / "app.py")]).returncode
    except KeyboardInterrupt:
        say()
        say("서버를 종료했습니다.")
        return 0


if __name__ == "__main__":
    sys.exit(main())

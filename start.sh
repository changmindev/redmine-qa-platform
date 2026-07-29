#!/bin/sh
# macOS / Linux 실행 스크립트 (윈도우는 start.bat).
# 설치·실행 로직은 tools/bootstrap.py 한 곳에만 두고 양쪽이 함께 쓴다 —
# 두 벌로 나누면 한쪽만 고치는 일이 반드시 생긴다.
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3.10 이상이 필요합니다. https://www.python.org 에서 설치하세요."
  exit 1
fi

exec "$PY" tools/bootstrap.py "$@"

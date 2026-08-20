#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "== main_server 환경 설정 =="
PY=$(command -v python3)
echo "python: $PY"

if [ ! -d .venv ]; then
  echo "venv 생성..."
  "$PY" -m venv .venv
fi

echo "의존성 설치..."
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "완료. 실행: ./start_manager.sh"
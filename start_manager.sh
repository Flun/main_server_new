#!/usr/bin/env bash
cd "$(dirname "$0")"

if [ -x .venv/bin/python ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

echo "== AI Server Manager (포트 8999) =="
while true; do
  "$PY" app.py
  echo "[경고] 서버가 종료되었습니다. 5초 후 재시작 (Ctrl+C 로 중단)..."
  sleep 5
done
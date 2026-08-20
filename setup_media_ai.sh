#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"

if [ -x .venv/bin/python ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

echo "== Media Studio AI (음성 분리) 설치 =="
echo "대상 파이썬: $PY"
"$PY" -m pip install --target "$(pwd)/.media_ai_packages" -r media_ai_requirements.txt
echo "== 완료: .media_ai_packages 에 설치됨 =="
echo "서버를 재시작한 뒤 Media Studio에서 AI 음성 분리를 사용하세요."

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

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "Media Studio용 ffmpeg 설치..."
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo apt-get update
    sudo apt-get install -y ffmpeg
  else
    echo "[경고] ffmpeg가 없습니다. sudo apt-get install ffmpeg 를 실행해야 Media Studio 변환 기능을 사용할 수 있습니다."
  fi
fi

echo "완료. 실행: ./start_manager.sh"

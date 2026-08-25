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

MISSING_PACKAGES=()
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  MISSING_PACKAGES+=(ffmpeg)
fi
if ! command -v mount.cifs >/dev/null 2>&1; then
  MISSING_PACKAGES+=(cifs-utils)
fi
if ((${#MISSING_PACKAGES[@]})); then
  echo "시스템 의존성 설치: ${MISSING_PACKAGES[*]}"
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo apt-get update
    sudo apt-get install -y "${MISSING_PACKAGES[@]}"
  else
    echo "[경고] 다음 패키지를 직접 설치해야 합니다: ${MISSING_PACKAGES[*]}"
  fi
fi

echo "완료. 실행: ./start_manager.sh"

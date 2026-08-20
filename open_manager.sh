#!/usr/bin/env bash
set -u

if ! systemctl --user is-active --quiet main_server.service; then
  systemctl --user start main_server.service
fi

for _ in $(seq 1 30); do
  if curl -fsS --max-time 1 http://127.0.0.1:8999/api/status >/dev/null 2>&1; then
    xdg-open http://127.0.0.1:8999/ >/dev/null 2>&1 &
    exit 0
  fi
  sleep 0.5
done

notify-send "Main Server" "서비스가 시작되지 않았습니다. systemctl --user status main_server.service를 확인하세요." 2>/dev/null || true
exit 1

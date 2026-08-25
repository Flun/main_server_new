@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (set "PY=.venv\Scripts\pythonw.exe") else (set "PY=python")
echo == AI Server Manager (port 8999) ==
rem 부팅/로그온 직후 시스템(GPU 드라이버, 네트워크 등) 준비 대기
timeout /t 10 >nul
:loop
rem 이미 manager가 떠 있으면(예: /api/restart의 restarter가 먼저 기동한 경우)
rem 감독만 하고 중복 spawn을 피합니다.
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8999/api^/status ^| Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  timeout /t 5 >nul
  goto loop
)
"%PY%" app.py >> logs\manager.log 2>&1
echo [Warning] Server stopped (log: logs\manager.log). Restarting in 5 seconds...
timeout /t 5 >nul
goto loop

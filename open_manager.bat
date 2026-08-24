@echo off
setlocal
cd /d "%~dp0"
rem manager가 꺼져 있으면 pythonw로 숨겨서 기동하고, 그 다음 브라우저로 이동합니다.
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 http://127.0.0.1:8999/api^/status ^| Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
  if exist ".venv\Scripts\pythonw.exe" (
    start "" /min ".venv\Scripts\pythonw.exe" app.py
  ) else (
    start "" /min pythonw app.py
  )
)
for /l %%i in (1,1,30) do (
  powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 http://127.0.0.1:8999/api^/status ^| Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 goto open
  timeout /t 1 >nul
)
echo [Error] Service did not start. Check the logs in the logs folder.
exit /b 1
:open
start "" http://127.0.0.1:8999/

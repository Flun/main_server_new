@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
"%PY%" autostart_service.py register || exit /b 1
echo Manager will now start automatically at logon (Task Scheduler: MainServer).

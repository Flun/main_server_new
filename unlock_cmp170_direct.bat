@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0unlock_cmp170_direct.ps1"
exit /b %errorlevel%

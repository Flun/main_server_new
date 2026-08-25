@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0capture_ga100ctl.ps1"
exit /b %errorlevel%

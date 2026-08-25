@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stage_cmp170_driver_once.ps1"
exit /b %errorlevel%

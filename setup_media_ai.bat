@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")
echo == Media Studio AI (voice isolation) install ==
"%PY%" -m pip install --target "%cd%\.media_ai_packages" -r media_ai_requirements.txt || exit /b 1
echo Done: installed to .media_ai_packages. Restart the server and use AI voice separation in Media Studio.

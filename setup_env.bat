@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (echo [Error] Python is not installed or not in PATH & exit /b 1)
if not exist .venv (
  echo Creating venv...
  python -m venv .venv || exit /b 1
)
echo Installing dependencies...
.venv\Scripts\python.exe -m pip install --upgrade pip || exit /b 1
.venv\Scripts\python.exe -m pip install -r requirements.txt || (
  echo [Warning] Some packages failed to install. Media AI may need a compatible Python. & exit /b 1)
echo Done. Run: start_manager.bat

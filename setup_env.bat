@echo off
setlocal
cd /d "%~dp0"
where winget >nul 2>nul || (echo [Error] winget is required. Install Microsoft App Installer. & exit /b 1)
where git >nul 2>nul
if errorlevel 1 winget install --id Git.Git --exact --source winget --accept-package-agreements --accept-source-agreements --silent || exit /b 1
where gh >nul 2>nul
if errorlevel 1 winget install --id GitHub.cli --exact --source winget --accept-package-agreements --accept-source-agreements --silent || exit /b 1
where python >nul 2>nul || (echo [Error] Python is not installed or not in PATH & exit /b 1)
if not exist .venv (
  echo Creating venv...
  python -m venv .venv || exit /b 1
)
echo Installing dependencies...
.venv\Scripts\python.exe -m pip install --upgrade pip || exit /b 1
.venv\Scripts\python.exe -m pip install -r requirements.txt || (
  echo [Warning] Some packages failed to install. Media AI may need a compatible Python. & exit /b 1)
echo Building Windows administrator helper...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File fan_helper\build.ps1 || exit /b 1
echo Done. Run: start_manager.bat

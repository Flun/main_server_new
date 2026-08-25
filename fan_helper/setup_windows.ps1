$ErrorActionPreference = 'Stop'

& (Join-Path $PSScriptRoot 'build.ps1')

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Python environment is missing. Run setup_env.bat first.'
}

# Reuse the exact bootstrap used by normal main_server startup: pinned download,
# SHA-256 + Authenticode validation, one UAC prompt, and helper task registration.
& $python -c "import pawnio_bootstrap as p; code=p._run_elevated_bootstrap(None) if p.installed_version() else (p._install() or 0); s=p.get_status(); print(s); raise SystemExit(code or (0 if s.get('status') == 'ready' else 1))"

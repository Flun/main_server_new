$ErrorActionPreference = 'Stop'
$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $baseDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python.exe -ErrorAction Stop).Source
}
$controller = Join-Path $baseDir 'cmp170_direct_unlock.py'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $MyInvocation.MyCommand.Path + '"'))
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -Wait -PassThru
    exit $process.ExitCode
}

Set-Location -LiteralPath $baseDir
& $python $controller --execute
$exitCode = $LASTEXITCODE
Write-Host ''
if ($exitCode -eq 0) {
    Write-Host 'CMP 170HX direct unlock completed.' -ForegroundColor Green
} else {
    Write-Host "CMP 170HX direct unlock failed (exit $exitCode)" -ForegroundColor Red
    Write-Host "Log: $baseDir\logs\cmp170_direct_unlock.log"
}
Read-Host 'Press Enter to close'
exit $exitCode

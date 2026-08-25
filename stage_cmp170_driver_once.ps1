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
& $python $controller --stage-once
$exitCode = $LASTEXITCODE
Write-Host ''
if ($exitCode -eq 0) {
    Write-Host '170_boot was staged and will be reused on future boots.' -ForegroundColor Green
} else {
    Write-Host "170_boot staging failed (exit $exitCode)" -ForegroundColor Red
    Write-Host "Log: $baseDir\logs\cmp170_direct_unlock.log"
}
exit $exitCode

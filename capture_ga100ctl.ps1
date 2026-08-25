$ErrorActionPreference = 'Stop'

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptPath = Join-Path $baseDir 'cmp170_ioctl_capture.py'
$python = Join-Path $baseDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python.exe -ErrorAction Stop).Source
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $argList = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', ('"' + $MyInvocation.MyCommand.Path + '"')
    )
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs -Wait -PassThru
    exit $process.ExitCode
}

Set-Location -LiteralPath $baseDir
& $python $scriptPath
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Write-Host ''
    Write-Host "캡처 실행 실패 (exit $exitCode)" -ForegroundColor Red
    Read-Host 'Enter를 누르면 창이 닫힙니다'
}
exit $exitCode

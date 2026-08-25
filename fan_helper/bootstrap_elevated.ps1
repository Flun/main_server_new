param(
    [Parameter(Mandatory=$true)][string]$HelperPath,
    [string]$InstallerPath = ''
)

$ErrorActionPreference = 'Stop'

if ($InstallerPath -and -not (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO' -ErrorAction SilentlyContinue)) {
    $install = Start-Process -FilePath $InstallerPath -ArgumentList '-install','-silent' -Wait -PassThru -WindowStyle Hidden
    if ($install.ExitCode -notin 0,3010) { throw "PawnIO installer exit code: $($install.ExitCode)" }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$tokenPath = Join-Path (Split-Path -Parent $HelperPath) 'fan_helper_secret.txt'
# Rotate the secret whenever the elevated helper task is (re)registered.
$bytes = [byte[]]::new(32)
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$token = [BitConverter]::ToString($bytes).Replace('-', '')
[IO.File]::WriteAllText($tokenPath, $token)
& icacls.exe $tokenPath /inheritance:r /grant:r "${identity}:(R,W)" 'SYSTEM:(F)' 'Administrators:(F)' | Out-Null
$existing = Get-ScheduledTask -TaskName 'MainServerFanHelper' -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName 'MainServerFanHelper' -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
}
# A previous helper binary can remain alive while its executable is upgraded.
# The lease watchdog already restores firmware control before the process exits.
Get-Process -Name 'MainServer.FanHelper' -ErrorAction SilentlyContinue | Stop-Process -Force
$action = New-ScheduledTaskAction -Execute $HelperPath -Argument "--tcp --token-file `"$tokenPath`"" -WorkingDirectory (Split-Path -Parent $HelperPath)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
Register-ScheduledTask -TaskName 'MainServerFanHelper' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'MainServerFanHelper'

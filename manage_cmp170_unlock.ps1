param(
    [ValidateSet('run', 'register', 'unregister')]
    [string]$Action,
    [string]$PythonPath,
    [string]$ControllerPath,
    [string]$WorkingDirectory,
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
$taskName = 'CMP170HXUnlock'

if (-not $Elevated) {
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath),
        '-Action', $Action,
        '-PythonPath', ('"{0}"' -f $PythonPath),
        '-ControllerPath', ('"{0}"' -f $ControllerPath),
        '-WorkingDirectory', ('"{0}"' -f $WorkingDirectory),
        '-Elevated'
    )
    try {
        $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        exit $process.ExitCode
    } catch {
        Write-Error "관리자 권한 요청이 취소되었거나 실패했습니다: $($_.Exception.Message)"
        exit 1
    }
}

if ($Action -eq 'run') {
    $process = Start-Process -FilePath $PythonPath -ArgumentList @('"' + $ControllerPath + '"', '--execute') -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -Wait -PassThru
    exit $process.ExitCode
}

if ($Action -eq 'unregister') {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    exit 0
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$actionObject = New-ScheduledTaskAction -Execute $PythonPath -Argument ('"{0}" --execute' -f $ControllerPath) -WorkingDirectory $WorkingDirectory
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $actionObject -Trigger $trigger -Principal $principal -Settings $settings -Description 'CMP 170HX 64GB unlock before GPU workloads' -Force | Out-Null
exit 0

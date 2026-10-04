# Registers the daily pipeline run in Windows Task Scheduler (current user, no admin needed).
# Usage (PowerShell, from the repo):   .\pipeline\scripts\install_daily_task.ps1            # 07:00 daily
#                                     .\pipeline\scripts\install_daily_task.ps1 -Time 21:00
#                                     .\pipeline\scripts\install_daily_task.ps1 -Remove
param([string]$Time = "07:00", [switch]$Remove)

$TaskName = "nlagg-daily"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task $TaskName"
    exit 0
}
$Pipeline = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Pipeline ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Not found: $Python  (create the venv first: see pipeline\README.md)" }

$Action = New-ScheduledTaskAction -Execute $Python -Argument "-m nlagg run-daily" -WorkingDirectory $Pipeline
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
# StartWhenAvailable: if the PC was off/asleep at $Time, run as soon as it is on again.
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun:$false -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
            -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Newsletter aggregator: capture, split, extract, cluster, compose (draft only)" -Force | Out-Null
Write-Host "Scheduled '$TaskName' daily at $Time -> $Python -m nlagg run-daily (in $Pipeline)"
Write-Host "Run it now:   Start-ScheduledTask -TaskName $TaskName"
Write-Host "Logs:         $Pipeline\out\logs\"

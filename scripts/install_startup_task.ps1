<#
Registers (or removes) a Scheduled Task so the bot starts with Windows and
keeps running in the background with no console window.

The task runs as the current user at log on, so it needs no admin rights and no
stored password. run_local.ps1 supervises uvicorn itself; the task settings here
only cover the case where the whole PowerShell process dies.

  powershell -ExecutionPolicy Bypass -File scripts\install_startup_task.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install_startup_task.ps1 -Uninstall

After installing, start it now without waiting for a reboot:
  Start-ScheduledTask -TaskName "IssueSolverBot"
#>
[CmdletBinding()]
param(
    [string]$TaskName = "IssueSolverBot",
    [int]$Port = 8010,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $repo "scripts\run_local.ps1"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed scheduled task '$TaskName'."
    } else {
        Write-Output "No scheduled task named '$TaskName'."
    }
    return
}

if (-not (Test-Path $runner)) { throw "Cannot find $runner" }

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -Port $Port" `
    -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

# S4U runs the task detached from any interactive console and needs no stored
# password. Without it the task inherits the console of whoever started it, and
# closing that console sends CTRL_CLOSE to the whole tree, killing the bot with
# STATUS_CONTROL_C_EXIT. Interactive is kept as a fallback for accounts that
# lack the "Log on as a batch job" right.
$user = "$env:USERDOMAIN\$env:USERNAME"
$registered = $false
foreach ($logonType in @("S4U", "Interactive")) {
    try {
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType $logonType -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Principal $principal `
            -Description "Runs the GrantFox issue solver bot (solver worker, discovery poller, local dashboard)." `
            -Force -ErrorAction Stop | Out-Null
        Write-Output "Installed scheduled task '$TaskName' (logon type: $logonType)."
        $registered = $true
        break
    } catch {
        Write-Output "Could not register with logon type ${logonType}: $($_.Exception.Message)"
    }
}
if (-not $registered) { throw "Failed to register scheduled task '$TaskName'." }
Write-Output "Start it now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Output "Dashboard:     http://localhost:$Port/dashboard"
Write-Output "Logs:          $(Join-Path $repo 'logs')"

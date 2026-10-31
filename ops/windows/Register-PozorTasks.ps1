# SAMPLE: registers tasks, does not start them. Uses current interactive user's sign-in.
# Review names/paths first. For unattended boot use a dedicated service identity instead.
[CmdletBinding()]
param(
    [string]$Repository = (Join-Path $PSScriptRoot '..\..'),
    [string]$TaskPrefix = 'Pozor'
)
$ErrorActionPreference = 'Stop'
$repoPath = (Resolve-Path -LiteralPath $Repository).Path
$runnerPath = Join-Path $repoPath 'ops\windows\Start-Pozor.ps1'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$definitions = @('Bot', 'Llm', 'Backup')
# Preflight all names to avoid partially replacing an existing installation.
foreach ($mode in $definitions) {
    if (Get-ScheduledTask -TaskName "$TaskPrefix-$mode" -ErrorAction SilentlyContinue) {
        throw 'Task already exists; review/update it explicitly'
    }
}
foreach ($mode in $definitions) {
    $arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -File `"$runnerPath`" -Mode $mode -Repository `"$repoPath`""
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $repoPath
    $trigger = if ($mode -eq 'Backup') {
        New-ScheduledTaskTrigger -Daily -At '03:00'
    } else {
        New-ScheduledTaskTrigger -AtLogOn -User $identity
    }
    Register-ScheduledTask -TaskName "$TaskPrefix-$mode" -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description 'pozor.me reviewed local sample'
}

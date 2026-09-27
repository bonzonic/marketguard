<#
.SYNOPSIS
    Remove the MarketGuard scheduled tasks.

.DESCRIPTION
    Stops and unregisters everything install-tasks.ps1 created. Logs, alert
    history and the config file are kept by default - they are the record of
    what happened during the run, and the run is the point of the project.
    Pass -RemoveState to delete them too.

    The archive under MARKETGUARD_DATA is never touched by this script, at all,
    under any switch.
#>
[CmdletBinding()]
param(
    [string]$StateDir = (Join-Path $env:ProgramData 'MarketGuard'),
    [string]$TaskFolder = '\MarketGuard\',
    [switch]$RemoveState,
    [switch]$RemoveEventSource
)

$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell."
}

foreach ($name in @('Recorder', 'Watchdog', 'Alert Popup')) {
    $task = Get-ScheduledTask -TaskPath $TaskFolder -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) { Write-Host "not installed: $TaskFolder$name"; continue }
    if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskPath $TaskFolder -TaskName $name }
    Unregister-ScheduledTask -TaskPath $TaskFolder -TaskName $name -Confirm:$false
    Write-Host "removed $TaskFolder$name"
}

if ($RemoveEventSource -and [System.Diagnostics.EventLog]::SourceExists('MarketGuard')) {
    Remove-EventLog -Source 'MarketGuard'
    Write-Host "removed the MarketGuard event log source"
}

if ($RemoveState -and (Test-Path -LiteralPath $StateDir)) {
    Remove-Item -LiteralPath $StateDir -Recurse -Force
    Write-Host "removed $StateDir"
} else {
    Write-Host "kept $StateDir (logs, alerts.log, marketguard.env)"
}

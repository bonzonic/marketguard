<#
.SYNOPSIS
    Make Windows Update restarts predictable instead of arbitrary.

.DESCRIPTION
    Over five weeks a forced update restart is close to certain. Two separate
    things have to be true for that to be survivable:

      1. the restart happens when we choose, not mid-demo
      2. capture resumes afterwards without anyone doing anything

    (2) is the important one and it is not handled here - it is handled by the
    Recorder task's at-startup and every-five-minutes triggers (see
    ops\install-tasks.ps1). This script only does (1).

    Active hours is the one mechanism that actually works on Windows 11 Home.
    The Windows Update for Business deferral policies under
    HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate are silently
    ignored on Home editions, so a script that wrote them would look like it
    worked and change nothing - worse than not trying.

    The window is capped by Windows at 18 hours, so some restart window always
    exists. Set it to the hours when a restart is least costly, and know when
    they are: that is when to expect a gap of a minute or two in the archive.

    Pausing updates entirely is possible for up to 5 weeks - which from
    25 Sep 2026 lands exactly on the 30 Oct deadline - but it is a deliberate
    decision to run a daily-driver machine unpatched for the duration, and it
    is a two-click job in Settings > Windows Update > Pause updates. It is not
    scripted here on purpose: the registry keys behind it are undocumented,
    and a half-applied pause that reports success is the worst outcome
    available.

.EXAMPLE
    .\ops\windows-update-policy.ps1 -Show
    .\ops\windows-update-policy.ps1 -StartHour 9 -EndHour 3
#>
[CmdletBinding()]
param(
    # Restarts are blocked between StartHour and EndHour local time, so they
    # happen outside it. Default 09:00-03:00 is the 18 hour maximum, leaving
    # 03:00-09:00 as the restart window.
    [ValidateRange(0, 23)][int]$StartHour = 9,
    [ValidateRange(0, 23)][int]$EndHour = 3,
    [switch]$Show
)

$ErrorActionPreference = 'Stop'
$key = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'

function Show-Current {
    $s = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
    Write-Host "active hours        : $($s.ActiveHoursStart):00 - $($s.ActiveHoursEnd):00 (local)"
    Write-Host "auto-adjust         : $(if ($s.SmartActiveHoursState -eq 0) { 'off (pinned)' } else { 'on - Windows may move the window' })"
    if ($s.PauseUpdatesExpiryTime) {
        Write-Host "updates paused until: $($s.PauseUpdatesExpiryTime)"
    } else {
        Write-Host "updates paused until: not paused"
    }
    Write-Host "timezone            : $((Get-TimeZone).Id)"
    $start = $s.ActiveHoursEnd
    $end = $s.ActiveHoursStart
    Write-Host ""
    Write-Host "=> expect unattended restarts between $($start):00 and $($end):00 local."
    Write-Host "   The Recorder task restarts capture at boot, so the cost is a gap of"
    Write-Host "   a minute or two, not a lost night. Confirm after any restart with:"
    Write-Host "       python status.py"
}

if ($Show) { Show-Current; exit 0 }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell - active hours live under HKLM."
}

$span = ($EndHour - $StartHour + 24) % 24
if ($span -eq 0) { throw "StartHour and EndHour must differ." }
if ($span -gt 18) {
    throw "Windows caps active hours at 18 hours; $StartHour-$EndHour is $span. Pick a narrower window."
}

Write-Host "before:"
Show-Current
Write-Host ""

New-Item -Path $key -Force | Out-Null
Set-ItemProperty -Path $key -Name 'ActiveHoursStart' -Value $StartHour -Type DWord
Set-ItemProperty -Path $key -Name 'ActiveHoursEnd' -Value $EndHour -Type DWord
# 0 pins the window. Left on, Windows silently re-derives active hours from
# usage patterns, and a window that moves is not a window you can plan around.
Set-ItemProperty -Path $key -Name 'SmartActiveHoursState' -Value 0 -Type DWord

Write-Host "after:"
Show-Current
Write-Host ""
Write-Host "Not done here, decide deliberately:"
Write-Host "  Settings > Windows Update > Pause updates can hold everything for 5 weeks,"
Write-Host "  which from late September reaches the 30 Oct deadline. That leaves the"
Write-Host "  machine unpatched for the duration - your call, not this script's."

<#
.SYNOPSIS
    Put a MarketGuard alert on screen.

.DESCRIPTION
    Run by the \MarketGuard\Alert Popup task, which triggers on Application
    event 1001 written by notify.py.

    The indirection is deliberate. The watchdog runs as SYSTEM in session 0
    and cannot draw anything the logged-on user will ever see; a task running
    in the user's session can. The event log is the only channel that crosses
    that boundary without a service, a tray app or an account somewhere.

    It shows the newest line from alerts.log rather than the event payload,
    because that file is the single source of truth that status.py also reads
    - one place to look, one format to recognise.

    MessageBox rather than a toast: toasts are silently dropped during focus
    assist, expire out of the action centre, and need an AppUserModelID to
    show a sensible name. A modal box is ugly and impossible to miss, which
    is the correct trade for "the recording stopped".
#>
[CmdletBinding()]
param(
    [string]$StateDir = (Join-Path $env:ProgramData 'MarketGuard')
)

Add-Type -AssemblyName PresentationFramework

$alerts = Join-Path $StateDir 'alerts.log'
$text = 'A MarketGuard alert fired, but alerts.log could not be read.'

if (Test-Path -LiteralPath $alerts) {
    $last = Get-Content -LiteralPath $alerts -Tail 1
    if ($last) {
        $parts = $last -split "`t", 3
        $text = if ($parts.Count -eq 3) {
            "{0}`n`n{1}`n`n(level {2})`n`nRun 'python status.py' for the full picture." -f $parts[0], $parts[2], $parts[1]
        } else { $last }
    }
}

[System.Windows.MessageBox]::Show(
    $text,
    'MarketGuard',
    [System.Windows.MessageBoxButton]::OK,
    [System.Windows.MessageBoxImage]::Warning
) | Out-Null

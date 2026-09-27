<#
.SYNOPSIS
    Install the MarketGuard recorder and watchdog as scheduled tasks.

.DESCRIPTION
    The recorder currently runs as a child of a PowerShell window. It dies on
    window close, logout, reboot and sleep, with no restart and no
    notification. The project needs it alive until 30 Oct 2026, on a machine
    somebody also uses for other things, so it has to stop being anybody's
    child process.

    Three tasks under \MarketGuard\:

      Recorder      the capture process itself, as SYSTEM so it starts before
                    anyone logs on and survives logout
      Watchdog      every 5 minutes: is data actually arriving, is there disk
                    left, is the recorder task running (watchdog.py)
      Alert Popup   in the logged-on user's session, triggered by the event
                    log entry the watchdog writes - the only way a SYSTEM
                    process can put something on screen

    Why Task Scheduler and not a Startup shortcut: a shortcut needs a logon,
    dies with the session, and cannot restart anything.

    Why three triggers on the recorder (at startup, at logon, and every five
    minutes) with MultipleInstances=IgnoreNew: any one of them alone has a
    hole. At startup misses a fast-startup resume, where Windows restores a
    hibernated kernel session rather than booting. At logon misses an
    unattended reboot. Restart-on-failure only fires on a non-zero exit, and
    the recorder's own out-of-disk abort exits zero. The repeating trigger
    closes all three, and IgnoreNew makes the overlap a no-op, so the worst
    case after any of them is five minutes of lost capture rather than five
    weeks.

    Idempotent: re-running re-registers the tasks and preserves any settings
    already in marketguard.env. Re-registering stops a running Recorder task,
    so re-run deliberately - the repeating trigger picks it back up within
    five minutes, or pass -StartNow.

.PARAMETER StartNow
    Start the Recorder task immediately. Off by default: if a recorder is
    still running in a console window, starting a second one has both writing
    the same hour. That is survivable (datafile.py deduplicates overlapping
    files on read) but it is not what you want. Stop the old one first.

.EXAMPLE
    # From an elevated PowerShell, in the repository root:
    .\ops\install-tasks.ps1 -NtfyTopic 'marketguard-7f3a91c4'
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$Python,
    [string]$DataDir,
    [string]$StateDir = (Join-Path $env:ProgramData 'MarketGuard'),
    [string]$TaskFolder = '\MarketGuard\',
    [string]$TaskUser = 'SYSTEM',
    [string]$PopupUser,

    # Alerting. ntfy needs no account at all: invent an unguessable topic,
    # install the ntfy app, subscribe to it. It is the only channel here that
    # reaches someone who is not sitting at the machine.
    [string]$NtfyTopic,
    [string]$NtfyServer,
    [string]$WebhookUrl,
    [string]$WebhookField,

    [int]$RecorderCheckMinutes = 5,
    [int]$WatchdogMinutes = 5,
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- preflight

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell. Registering a task that runs as SYSTEM, and creating the MarketGuard event log source, both require administrator rights."
}

if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
if (-not $Python)   { $Python   = Join-Path $RepoRoot 'venv\Scripts\python.exe' }
if (-not $PopupUser) {
    # The user whose session a popup should appear in. Whoever is at the
    # console when this is installed is the best available guess.
    $PopupUser = (Get-CimInstance Win32_ComputerSystem).UserName
    if (-not $PopupUser) { $PopupUser = "$env:USERDOMAIN\$env:USERNAME" }
}

foreach ($p in @(
        @{ n = 'repository'; v = $RepoRoot },
        @{ n = 'venv python'; v = $Python },
        @{ n = 'recorder.py'; v = (Join-Path $RepoRoot 'recorder.py') },
        @{ n = 'watchdog.py'; v = (Join-Path $RepoRoot 'watchdog.py') },
        @{ n = 'ops\run.ps1'; v = (Join-Path $RepoRoot 'ops\run.ps1') })) {
    if (-not (Test-Path -LiteralPath $p.v)) { throw "$($p.n) not found: $($p.v)" }
}

# A wrong data directory is the quietest failure available here: the task
# runs, the recorder records, and it fills a second archive nobody looks at.
# So resolve it now and let the human see it before anything is registered.
if (-not $DataDir) {
    $DataDir = & $Python -c "import config; print(config.DATA_DIR)"
    if ($LASTEXITCODE -ne 0 -or -not $DataDir) { throw "could not resolve the data directory via config.py" }
}
$DataDir = $DataDir.Trim()

Write-Host "repository : $RepoRoot"
Write-Host "python     : $Python"
Write-Host "data       : $DataDir"
Write-Host "state      : $StateDir"
Write-Host "run as     : $TaskUser   (popups as $PopupUser)"
Write-Host ""

if (-not (Test-Path -LiteralPath $DataDir)) {
    # Not created here on purpose. The recorder creates it on first run, and
    # this script has no business touching the archive directory.
    Write-Warning "data directory does not exist yet - the recorder will create it on first run"
}

# ------------------------------------------------------------- config file

New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $StateDir 'logs') -Force | Out-Null

$envFile = Join-Path $StateDir 'marketguard.env'
$settings = [ordered]@{
    MARKETGUARD_DATA          = $DataDir
    MARKETGUARD_STATE         = $StateDir
    MARKETGUARD_NTFY_TOPIC    = ''
    MARKETGUARD_NTFY_SERVER   = 'https://ntfy.sh'
    MARKETGUARD_WEBHOOK_URL   = ''
    MARKETGUARD_WEBHOOK_FIELD = 'text'
}
# Existing values win over the defaults above, so re-running the installer to
# pick up a code change does not quietly unsubscribe the alerting.
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        $line = $line.Trim()
        if (-not $line -or $line.StartsWith('#') -or $line -notmatch '=') { continue }
        $k, $v = $line.Split('=', 2)
        $settings[$k.Trim()] = $v.Trim().Trim('"')
    }
}
$settings['MARKETGUARD_DATA'] = $DataDir
$settings['MARKETGUARD_STATE'] = $StateDir
if ($NtfyTopic)    { $settings['MARKETGUARD_NTFY_TOPIC'] = $NtfyTopic }
if ($NtfyServer)   { $settings['MARKETGUARD_NTFY_SERVER'] = $NtfyServer }
if ($WebhookUrl)   { $settings['MARKETGUARD_WEBHOOK_URL'] = $WebhookUrl }
if ($WebhookField) { $settings['MARKETGUARD_WEBHOOK_FIELD'] = $WebhookField }

$header = @(
    '# MarketGuard machine configuration.',
    '# Read by ops\run.ps1 for the scheduled tasks and by notify.py for',
    '# anything run by hand, so both agree about where the data lives.',
    '# Plain KEY=value. No shell expansion, no quotes needed.',
    '# Readable by local users: treat the ntfy topic as public to this machine.',
    ''
)
# WriteAllLines with an explicit no-BOM encoder, because Set-Content -Encoding
# utf8 on PowerShell 5.1 emits a BOM and the BOM ends up glued to the first
# key name. config.py now reads utf-8-sig so it would survive either way, but
# a config file with invisible bytes in it is a trap for the next reader.
[IO.File]::WriteAllLines(
    $envFile,
    [string[]]($header + ($settings.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" })),
    (New-Object Text.UTF8Encoding $false))
Write-Host "wrote $envFile"

# ---------------------------------------------------------- event log source

if (-not [System.Diagnostics.EventLog]::SourceExists('MarketGuard')) {
    New-EventLog -LogName Application -Source 'MarketGuard'
    Write-Host "registered the MarketGuard event log source"
} else {
    Write-Host "event log source already registered"
}

# ----------------------------------------------------------------- the tasks

function New-RunAction([string]$Script) {
    New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $RepoRoot -Argument (
        '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass ' +
        "-File `"$(Join-Path $RepoRoot 'ops\run.ps1')`" -Script $Script " +
        "-RepoRoot `"$RepoRoot`" -Python `"$Python`" -StateDir `"$StateDir`""
    )
}

$principal = New-ScheduledTaskPrincipal -UserId $TaskUser -LogonType ServiceAccount -RunLevel Highest

# --- Recorder ---
$recorderTriggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
        -RepetitionInterval (New-TimeSpan -Minutes $RecorderCheckMinutes))
)

$recorderSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
# ExecutionTimeLimit zero is "no limit". The default is three days, which
# would silently kill the capture five weeks short of the deadline.
$recorderSettings.IdleSettings.StopOnIdleEnd = $false
$recorderSettings.Priority = 4          # default 7 runs at background I/O priority
$recorderSettings.WakeToRun = $true     # if the machine sleeps anyway, come back

Register-ScheduledTask -TaskPath $TaskFolder -TaskName 'Recorder' -Force `
    -Action (New-RunAction 'recorder.py') -Trigger $recorderTriggers `
    -Principal $principal -Settings $recorderSettings `
    -Description 'MarketGuard capture. Restarts at boot, at logon, and every few minutes if it is not already running.' | Out-Null
Write-Host "registered $TaskFolder`Recorder"

# --- Watchdog ---
$bootTrigger = New-ScheduledTaskTrigger -AtStartup
# Do not alert about a recorder that has simply not started yet.
$bootTrigger.Delay = 'PT3M'
$watchdogTriggers = @(
    $bootTrigger,
    (New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
        -RepetitionInterval (New-TimeSpan -Minutes $WatchdogMinutes))
)

# No restart-on-failure here: watchdog.py exits non-zero *by design* when it
# finds a problem, so restarting it on failure would busy-loop the reporting.
$watchdogSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$watchdogSettings.IdleSettings.StopOnIdleEnd = $false

Register-ScheduledTask -TaskPath $TaskFolder -TaskName 'Watchdog' -Force `
    -Action (New-RunAction 'watchdog.py') -Trigger $watchdogTriggers `
    -Principal $principal -Settings $watchdogSettings `
    -Description 'MarketGuard liveness and disk check. Restarts the recorder if the archive stops growing.' | Out-Null
Write-Host "registered $TaskFolder`Watchdog"

# --- Alert Popup ---
# Registered from XML because there is no cmdlet for an event-log trigger,
# and the event log is the only bridge from the SYSTEM watchdog to the
# logged-on user's screen.
$popupScript = Join-Path $RepoRoot 'ops\alert-popup.ps1'
$popupXml = @"
<?xml version="1.0"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Shows a MarketGuard alert on screen when the watchdog writes Application event 1001.</Description>
  </RegistrationInfo>
  <Triggers>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="Application"&gt;&lt;Select Path="Application"&gt;*[System[Provider[@Name='MarketGuard'] and (EventID=1001)]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$PopupUser</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "$popupScript" -StateDir "$StateDir"</Arguments>
    </Exec>
  </Actions>
</Task>
"@

Register-ScheduledTask -TaskPath $TaskFolder -TaskName 'Alert Popup' -Xml $popupXml -Force | Out-Null
Write-Host "registered $TaskFolder`Alert Popup"

# ------------------------------------------------------------------ wrap up

Write-Host ""
if (-not $settings['MARKETGUARD_NTFY_TOPIC'] -and -not $settings['MARKETGUARD_WEBHOOK_URL']) {
    Write-Warning @"
No push alerting is configured, so alerts only reach you while you are at this
machine. Pick an unguessable ntfy topic, install the ntfy app on your phone,
subscribe to it, and re-run with:
    .\ops\install-tasks.ps1 -NtfyTopic 'marketguard-<something-random>'
"@
}

if ($StartNow) {
    Start-ScheduledTask -TaskPath $TaskFolder -TaskName 'Recorder'
    Write-Host "started the Recorder task"
} else {
    Write-Host "Not started. Stop any recorder running in a console window first, then:"
    Write-Host "    Start-ScheduledTask -TaskPath '$TaskFolder' -TaskName 'Recorder'"
}
Start-ScheduledTask -TaskPath $TaskFolder -TaskName 'Watchdog'

Write-Host ""
Write-Host "Verify with:"
Write-Host "    & `"$Python`" `"$(Join-Path $RepoRoot 'status.py')`""
Write-Host "    & `"$Python`" `"$(Join-Path $RepoRoot 'notify.py')`" WARN   # test the alert path"

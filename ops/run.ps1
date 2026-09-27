<#
.SYNOPSIS
    Launch a MarketGuard Python entry point under Task Scheduler.

.DESCRIPTION
    Task Scheduler hands an action no usable working directory (it defaults to
    C:\Windows\System32), no environment beyond the machine's, and nowhere for
    stdout to go - anything the process prints is discarded. All three of
    those turn into a task that "runs" and silently does nothing, which is the
    exact failure this whole exercise exists to prevent. So every scheduled
    action goes through this wrapper, which:

      - sets the working directory to the repository
      - loads C:\ProgramData\MarketGuard\marketguard.env so both tasks and a
        human at a prompt agree about where the data lives
      - captures stdout and stderr to a dated log file
      - prunes logs older than -KeepLogDays
      - propagates the child's exit code, so Task Scheduler's restart-on-
        failure and Last Run Result mean something

    Redirection is done through cmd.exe rather than a PowerShell pipeline
    because Out-File holds a buffered writer: a process that runs for five
    weeks would leave its log looking empty for hours at a time.

.EXAMPLE
    .\run.ps1 -Script recorder.py
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Script,
    [string]$RepoRoot,
    [string]$Python,
    [string]$StateDir = (Join-Path $env:ProgramData 'MarketGuard'),
    [int]$KeepLogDays = 14
)

$ErrorActionPreference = 'Stop'

if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
if (-not $Python)   { $Python   = Join-Path $RepoRoot 'venv\Scripts\python.exe' }

$scriptPath = Join-Path $RepoRoot $Script
foreach ($p in @($RepoRoot, $Python, $scriptPath)) {
    if (-not (Test-Path -LiteralPath $p)) { throw "not found: $p" }
}

# config.py reads the env file itself, but it has to be told where to look
# before it can: a non-default -StateDir is invisible to it otherwise.
$env:MARKETGUARD_STATE = $StateDir

# Loaded here as well so that anything in the action that is not Python - a
# future wrapper, a diagnostic - sees the same settings.
$envFile = Join-Path $StateDir 'marketguard.env'
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        $line = $line.Trim()
        if (-not $line -or $line.StartsWith('#') -or $line -notmatch '=') { continue }
        $key, $value = $line.Split('=', 2)
        Set-Item -Path "Env:$($key.Trim())" -Value $value.Trim().Trim('"')
    }
}

# recorder.py already prints with flush=True, but a traceback on stderr is at
# the mercy of Python's buffering, and a crash whose traceback never reached
# the log is a crash you get to debug twice.
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONIOENCODING = 'utf-8'

$logDir = Join-Path $StateDir 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

$name = [IO.Path]::GetFileNameWithoutExtension($Script)
$log  = Join-Path $logDir ("{0}-{1}.log" -f $name, (Get-Date -Format 'yyyyMMdd-HHmmss'))

Get-ChildItem -Path $logDir -Filter "$name-*.log" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepLogDays) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

Set-Location -LiteralPath $RepoRoot

"=== $name starting $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz') ===" |
    Out-File -LiteralPath $log -Append -Encoding utf8

& cmd.exe /c "`"$Python`" `"$scriptPath`" >> `"$log`" 2>&1"
exit $LASTEXITCODE

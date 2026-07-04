param(
  [double]$CheckInterval = 30.0,
  [double]$PollInterval = 3.0,
  [int]$PageSize = 20,
  [string]$Config = "",
  [switch]$ArmLatest
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateDir = Join-Path $Root "state"
$PidFile = Join-Path $StateDir "watchdog.pid.json"
$Stdout = Join-Path $StateDir "watchdog_stdout.log"
$Stderr = Join-Path $StateDir "watchdog_stderr.log"
$Watcher = Join-Path $Root "watchdog_poll.ps1"

function Get-LiveWatchdogProcess {
  if (!(Test-Path -LiteralPath $PidFile)) {
    return $null
  }
  try {
    $record = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
    $pidValue = [int]$record.pid
  } catch {
    return $null
  }
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
  if ($null -eq $proc) {
    return $null
  }
  if ($proc.CommandLine -notlike "*watchdog_poll.ps1*") {
    return $null
  }
  return $proc
}

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

$live = Get-LiveWatchdogProcess
if ($null -ne $live) {
  [ordered]@{
    ok = $true
    status = "already_running"
    pid = $live.ProcessId
    commandLine = $live.CommandLine
    pidFile = $PidFile
  } | ConvertTo-Json -Depth 5
  exit 0
}

if (Test-Path -LiteralPath $PidFile) {
  Remove-Item -LiteralPath $PidFile -Force
}

$args = @(
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-File", $Watcher,
  "-CheckInterval", [string]$CheckInterval,
  "-PollInterval", [string]$PollInterval,
  "-PageSize", [string]$PageSize
)
if ($Config -ne "") {
  $args += @("-Config", $Config)
}
if ($ArmLatest) {
  $args += @("-ArmLatest")
}

$proc = Start-Process -FilePath "powershell" `
  -ArgumentList $args `
  -WorkingDirectory $Root `
  -RedirectStandardOutput $Stdout `
  -RedirectStandardError $Stderr `
  -WindowStyle Hidden `
  -PassThru

[ordered]@{
  pid = $proc.Id
  started_at = (Get-Date -Format o)
  command = "powershell " + ($args -join " ")
  stdout = $Stdout
  stderr = $Stderr
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $PidFile -Encoding UTF8

[ordered]@{
  ok = $true
  status = "started"
  pid = $proc.Id
  checkInterval = $CheckInterval
  pollInterval = $PollInterval
  pageSize = $PageSize
  armedLatest = [bool]$ArmLatest
  stdout = $Stdout
  stderr = $Stderr
  pidFile = $PidFile
} | ConvertTo-Json -Depth 5

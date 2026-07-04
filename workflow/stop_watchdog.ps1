$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateDir = Join-Path $Root "state"
$PidFile = Join-Path $StateDir "watchdog.pid.json"

if (!(Test-Path -LiteralPath $PidFile)) {
  [ordered]@{
    ok = $true
    status = "not_running"
    reason = "pid_file_missing"
  } | ConvertTo-Json -Depth 5
  exit 0
}

try {
  $record = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
  $pidValue = [int]$record.pid
} catch {
  Remove-Item -LiteralPath $PidFile -Force
  [ordered]@{
    ok = $true
    status = "not_running"
    reason = "pid_file_unreadable_removed"
  } | ConvertTo-Json -Depth 5
  exit 0
}

$proc = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
if ($null -eq $proc) {
  Remove-Item -LiteralPath $PidFile -Force
  [ordered]@{
    ok = $true
    status = "not_running"
    reason = "process_missing"
  } | ConvertTo-Json -Depth 5
  exit 0
}

if ($proc.CommandLine -notlike "*watchdog_poll.ps1*") {
  [ordered]@{
    ok = $false
    status = "refused"
    reason = "pid_does_not_match_watchdog"
    pid = $pidValue
    commandLine = $proc.CommandLine
  } | ConvertTo-Json -Depth 5
  exit 1
}

Stop-Process -Id $pidValue -Force
Remove-Item -LiteralPath $PidFile -Force

[ordered]@{
  ok = $true
  status = "stopped"
  pid = $pidValue
  poll_left_running = $true
} | ConvertTo-Json -Depth 5

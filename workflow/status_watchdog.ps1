$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateDir = Join-Path $Root "state"
$PidFile = Join-Path $StateDir "watchdog.pid.json"
$Heartbeat = Join-Path $StateDir "watchdog_state.json"

$pidRecord = $null
$proc = $null
if (Test-Path -LiteralPath $PidFile) {
  try {
    $pidRecord = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
    $pidValue = [int]$pidRecord.pid
    $candidate = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
    if ($null -ne $candidate -and $candidate.CommandLine -like "*watchdog_poll.ps1*") {
      $proc = $candidate
    }
  } catch {
    $pidRecord = $null
  }
}

$state = $null
if (Test-Path -LiteralPath $Heartbeat) {
  try {
    $state = Get-Content -LiteralPath $Heartbeat -Raw | ConvertFrom-Json
  } catch {
    $state = @{ unreadable = $true }
  }
}

[ordered]@{
  ok = $true
  running = ($null -ne $proc)
  pid = if ($null -ne $proc) { $proc.ProcessId } else { $null }
  started_at = if ($null -ne $pidRecord) { $pidRecord.started_at } else { $null }
  commandLine = if ($null -ne $proc) { $proc.CommandLine } else { $null }
  pidFile = $PidFile
  state = $state
} | ConvertTo-Json -Depth 8

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateDir = Join-Path $Root "state"
$PidFile = Join-Path $StateDir "poll.pid.json"
$PollState = Join-Path $StateDir "poll_state.json"

$pidRecord = $null
$proc = $null
if (Test-Path -LiteralPath $PidFile) {
  try {
    $pidRecord = Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
    $pidValue = [int]$pidRecord.pid
    $candidate = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
    if ($null -ne $candidate -and $candidate.CommandLine -like "*workflow.py*" -and $candidate.CommandLine -like "* poll*") {
      $proc = $candidate
    }
  } catch {
    $pidRecord = $null
  }
}

$state = $null
if (Test-Path -LiteralPath $PollState) {
  try {
    $state = Get-Content -LiteralPath $PollState -Raw | ConvertFrom-Json
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
  pollState = $state
} | ConvertTo-Json -Depth 8

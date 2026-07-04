param(
  [double]$CheckInterval = 30.0,
  [double]$PollInterval = 3.0,
  [int]$PageSize = 20,
  [string]$Config = "",
  [string]$Python = "",
  [switch]$ArmLatest
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $Root "..")).Path
$StateDir = Join-Path $Root "state"
$Workflow = Join-Path $Root "workflow.py"
$DefaultPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if ($Python -eq "") {
  if (Test-Path -LiteralPath $DefaultPython) {
    $Python = $DefaultPython
  } else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
      throw "python executable not found. Pass -Python or create .venv."
    }
    $Python = $pythonCommand.Source
  }
}
$PidFile = Join-Path $StateDir "poll.pid.json"
$PollStdout = Join-Path $StateDir "poll_stdout.log"
$PollStderr = Join-Path $StateDir "poll_stderr.log"
$Heartbeat = Join-Path $StateDir "watchdog_state.json"
$EventsLog = Join-Path $StateDir "watchdog_events.jsonl"

function Get-PollStatus {
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

  [pscustomobject]@{
    ok = $true
    running = ($null -ne $proc)
    pid = if ($null -ne $proc) { $proc.ProcessId } else { $null }
    started_at = if ($null -ne $pidRecord) { $pidRecord.started_at } else { $null }
    commandLine = if ($null -ne $proc) { $proc.CommandLine } else { $null }
    pidFile = $PidFile
  }
}

function Write-WatchdogEvent {
  param([Parameter(Mandatory = $true)] [hashtable]$Payload)
  $Payload["ts"] = (Get-Date -Format o)
  ($Payload | ConvertTo-Json -Depth 20 -Compress) | Add-Content -LiteralPath $EventsLog -Encoding UTF8
}

function Write-WatchdogState {
  param(
    $PollStatus,
    [string]$LastAction,
    [string]$LastError = ""
  )

  [ordered]@{
    ok = $true
    updated_at = (Get-Date -Format o)
    poll_running = if ($null -ne $PollStatus) { [bool]$PollStatus.running } else { $false }
    poll_pid = if ($null -ne $PollStatus) { $PollStatus.pid } else { $null }
    last_action = $LastAction
    last_error = $LastError
    check_interval = $CheckInterval
    poll_interval = $PollInterval
    page_size = $PageSize
    config = $Config
  } | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Heartbeat -Encoding UTF8
}

function Start-PollProcess {
  if (-not (Test-Path -LiteralPath $Python)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
      throw "python executable not found: $Python"
    }
    $script:Python = $pythonCommand.Source
  }

  if (Test-Path -LiteralPath $PidFile) {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
  }

  if ($ArmLatest) {
    $armArgs = @($Workflow)
    if ($Config -ne "") {
      $armArgs += @("--config", $Config)
    }
    $armArgs += @("poll", "--arm-latest", "--timeout", "0", "--page-size", [string]$PageSize)
    & $Python @armArgs | Out-Null
  }

  $args = @($Workflow)
  if ($Config -ne "") {
    $args += @("--config", $Config)
  }
  $args += @("poll", "--interval", [string]$PollInterval, "--page-size", [string]$PageSize)

  $proc = Start-Process -FilePath $Python `
    -ArgumentList $args `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $PollStdout `
    -RedirectStandardError $PollStderr `
    -WindowStyle Hidden `
    -PassThru

  [ordered]@{
    pid = $proc.Id
    started_at = (Get-Date -Format o)
    command = $Python + " " + ($args -join " ")
    stdout = $PollStdout
    stderr = $PollStderr
  } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $PidFile -Encoding UTF8

  [pscustomobject]@{
    ok = $true
    status = "started"
    pid = $proc.Id
    interval = $PollInterval
    pageSize = $PageSize
    armedLatest = [bool]$ArmLatest
    stdout = $PollStdout
    stderr = $PollStderr
    pidFile = $PidFile
  }
}

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
Write-WatchdogEvent @{ event = "watchdog_started"; check_interval = $CheckInterval; poll_interval = $PollInterval; page_size = $PageSize }

while ($true) {
  $status = $null
  try {
    $status = Get-PollStatus
    if ($null -eq $status -or $true -ne $status.running) {
      $startResult = Start-PollProcess
      Write-WatchdogEvent @{ event = "poll_restart_attempt"; status = $status; result = $startResult }
      $status = Get-PollStatus
      Write-WatchdogState -PollStatus $status -LastAction "poll_restart_attempt"
    } else {
      Write-WatchdogState -PollStatus $status -LastAction "poll_alive"
    }
  } catch {
    $message = $_.Exception.Message
    Write-WatchdogEvent @{ event = "watchdog_error"; error = $message }
    Write-WatchdogState -PollStatus $status -LastAction "watchdog_error" -LastError $message
  }

  Start-Sleep -Seconds ([Math]::Max(1, [int][Math]::Ceiling($CheckInterval)))
}

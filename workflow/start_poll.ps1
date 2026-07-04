param(
  [double]$Interval = 3.0,
  [int]$PageSize = 20,
  [string]$Config = "",
  [switch]$ArmLatest
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $Root "..")).Path
$Workflow = Join-Path $Root "workflow.py"
$StateDir = Join-Path $Root "state"
$PidFile = Join-Path $StateDir "poll.pid.json"
$Stdout = Join-Path $StateDir "poll_stdout.log"
$Stderr = Join-Path $StateDir "poll_stderr.log"
$UvPython = Join-Path $env:APPDATA "uv\python\cpython-3.11-windows-x86_64-none\python.exe"
$HermesPython = Join-Path $env:LOCALAPPDATA "hermes\hermes-agent\venv\Scripts\python.exe"
$PythonExe = if (Test-Path -LiteralPath $HermesPython) {
  $HermesPython
} elseif (Test-Path -LiteralPath $UvPython) {
  $UvPython
} else {
  (Get-Command python).Source
}

function Get-LivePollProcess {
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
  if ($proc.CommandLine -notlike "*workflow.py*" -or $proc.CommandLine -notlike "* poll*") {
    return $null
  }
  return $proc
}

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

$live = Get-LivePollProcess
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

if ($ArmLatest) {
  $armArgs = @($Workflow)
  if ($Config -ne "") {
    $armArgs += @("--config", $Config)
  }
  $armArgs += @("poll", "--arm-latest", "--page-size", [string]$PageSize)
  & $PythonExe @armArgs
}

$args = @($Workflow)
if ($Config -ne "") {
  $args += @("--config", $Config)
}
$args += @("poll", "--interval", [string]$Interval, "--page-size", [string]$PageSize)

$proc = Start-Process -FilePath $PythonExe `
  -ArgumentList $args `
  -WorkingDirectory $ProjectRoot `
  -RedirectStandardOutput $Stdout `
  -RedirectStandardError $Stderr `
  -WindowStyle Hidden `
  -PassThru

[ordered]@{
  pid = $proc.Id
  started_at = (Get-Date -Format o)
  command = $PythonExe + " " + ($args -join " ")
  stdout = $Stdout
  stderr = $Stderr
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $PidFile -Encoding UTF8

[ordered]@{
  ok = $true
  status = "started"
  pid = $proc.Id
  interval = $Interval
  pageSize = $PageSize
  armedLatest = [bool]$ArmLatest
  stdout = $Stdout
  stderr = $Stderr
  pidFile = $PidFile
} | ConvertTo-Json -Depth 5

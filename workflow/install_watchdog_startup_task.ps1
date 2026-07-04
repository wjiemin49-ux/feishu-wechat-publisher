param(
  [string]$TaskName = "XHSWorkflowPollWatchdog",
  [double]$CheckInterval = 30.0,
  [double]$PollInterval = 3.0,
  [int]$PageSize = 20,
  [string]$Config = "",
  [switch]$ArmLatest
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartScript = Join-Path $Root "start_watchdog.ps1"

if (!(Test-Path -LiteralPath $StartScript)) {
  throw "start_watchdog.ps1 not found: $StartScript"
}

$argParts = @(
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-File", "`"$StartScript`"",
  "-CheckInterval", [string]$CheckInterval,
  "-PollInterval", [string]$PollInterval,
  "-PageSize", [string]$PageSize
)
if ($Config -ne "") {
  $argParts += @("-Config", "`"$Config`"")
}
if ($ArmLatest) {
  $argParts += @("-ArmLatest")
}

$action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument ($argParts -join " ") `
  -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew `
  -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal `
  -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
  -LogonType Interactive `
  -RunLevel Limited

try {
  $task = Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Start the XHS Feishu workflow poll watchdog when the current user logs on." `
    -Force `
    -ErrorAction Stop

  [ordered]@{
    ok = $true
    status = "registered_scheduled_task"
    taskName = $task.TaskName
    taskPath = $task.TaskPath
    state = $task.State
    action = "powershell.exe " + ($argParts -join " ")
    user = ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
  } | ConvertTo-Json -Depth 8
  exit 0
} catch {
  $taskError = $_.Exception.Message
}

$StartupDir = [Environment]::GetFolderPath("Startup")
if ($StartupDir -eq "") {
  throw "Startup folder is unavailable. Scheduled Task error: $taskError"
}

New-Item -ItemType Directory -Force -Path $StartupDir | Out-Null
$StartupFile = Join-Path $StartupDir "$TaskName.cmd"
$startupArgs = $argParts[0..2] + @("-WindowStyle", "Hidden") + $argParts[3..($argParts.Count - 1)]
$startupLine = "powershell.exe " + ($startupArgs -join " ")
@(
  "@echo off",
  "cd /d `"$Root`"",
  $startupLine
) | Set-Content -LiteralPath $StartupFile -Encoding ASCII

[ordered]@{
  ok = $true
  status = "registered_startup_folder"
  taskName = $TaskName
  startupFile = $StartupFile
  action = $startupLine
  scheduledTaskError = $taskError
  user = ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
} | ConvertTo-Json -Depth 8

param(
  [string]$TaskName = "XHSWorkflowPollWatchdog"
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$info = $null
if ($null -ne $task) {
  try {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
  } catch {
    $info = $null
  }
}

$StartupDir = [Environment]::GetFolderPath("Startup")
$StartupFile = if ($StartupDir -ne "") { Join-Path $StartupDir "$TaskName.cmd" } else { "" }
$startupExists = ($StartupFile -ne "" -and (Test-Path -LiteralPath $StartupFile))

[ordered]@{
  ok = $true
  registered = ($null -ne $task -or $startupExists)
  scheduledTaskRegistered = ($null -ne $task)
  startupFileRegistered = $startupExists
  taskName = $TaskName
  taskPath = if ($null -ne $task) { $task.TaskPath } else { $null }
  state = if ($null -ne $task) { $task.State } else { $null }
  principal = if ($null -ne $task) { $task.Principal } else { $null }
  triggers = if ($null -ne $task) { $task.Triggers } else { $null }
  actions = if ($null -ne $task) { $task.Actions } else { $null }
  startupFile = $StartupFile
  lastRunTime = if ($null -ne $info) { $info.LastRunTime } else { $null }
  lastTaskResult = if ($null -ne $info) { $info.LastTaskResult } else { $null }
  nextRunTime = if ($null -ne $info) { $info.NextRunTime } else { $null }
} | ConvertTo-Json -Depth 20

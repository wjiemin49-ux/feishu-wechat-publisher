param(
  [string]$TaskName = "XHSWorkflowPollWatchdog"
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$removedScheduledTask = $false
if ($null -ne $task) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  $removedScheduledTask = $true
}

$StartupDir = [Environment]::GetFolderPath("Startup")
$StartupFile = if ($StartupDir -ne "") { Join-Path $StartupDir "$TaskName.cmd" } else { "" }
$removedStartupFile = $false
if ($StartupFile -ne "" -and (Test-Path -LiteralPath $StartupFile)) {
  Remove-Item -LiteralPath $StartupFile -Force
  $removedStartupFile = $true
}

[ordered]@{
  ok = $true
  status = if ($removedScheduledTask -or $removedStartupFile) { "unregistered" } else { "not_registered" }
  taskName = $TaskName
  removedScheduledTask = $removedScheduledTask
  removedStartupFile = $removedStartupFile
  startupFile = $StartupFile
} | ConvertTo-Json -Depth 5

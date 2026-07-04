param(
    [string]$TaskName = "XHSWorkflowDailyPrepublish",
    [string]$At = "09:30",
    [string]$Platforms = "wechat",
    [int]$Count = 1
)

$ErrorActionPreference = "Stop"
$Script = Join-Path $PSScriptRoot "run_daily_publish_once.ps1"
if (-not (Test-Path $Script)) {
    throw "missing daily runner: $Script"
}

$ActionArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$Script`" -Platforms `"$Platforms`" -Count $Count"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $ActionArgs
$Trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($At, "HH:mm", $null))
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Daily content generation and publish precheck; final publish still requires Feishu confirmation." -Force | Out-Null

[pscustomobject]@{
    ok = $true
    taskName = $TaskName
    at = $At
    platforms = $Platforms
    count = $Count
    finalPublishRequiresFeishuConfirm = $true
} | ConvertTo-Json -Depth 4

param(
    [string]$TaskName = "XHSWorkflowDailyPrepublish"
)

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $Task) {
    [pscustomobject]@{ ok = $true; installed = $false; taskName = $TaskName } | ConvertTo-Json -Depth 4
    exit 0
}
$Info = Get-ScheduledTaskInfo -TaskName $TaskName
[pscustomobject]@{
    ok = $true
    installed = $true
    taskName = $TaskName
    state = $Task.State.ToString()
    lastRunTime = $Info.LastRunTime
    lastTaskResult = $Info.LastTaskResult
    nextRunTime = $Info.NextRunTime
} | ConvertTo-Json -Depth 4

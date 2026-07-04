param(
    [string]$TaskPrefix = "XHSWorkflowFourDayPrepublish"
)

$Tasks = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "$TaskPrefix*" } | Sort-Object TaskName
$Rows = @()

foreach ($Task in $Tasks) {
    $Info = Get-ScheduledTaskInfo -TaskName $Task.TaskName
    $Rows += [pscustomobject]@{
        taskName = $Task.TaskName
        state = $Task.State.ToString()
        lastRunTime = if ($Info.LastRunTime.Year -gt 2000) { $Info.LastRunTime.ToString("yyyy-MM-dd HH:mm:ss") } else { "" }
        lastTaskResult = $Info.LastTaskResult
        nextRunTime = if ($Info.NextRunTime.Year -gt 2000) { $Info.NextRunTime.ToString("yyyy-MM-dd HH:mm:ss") } else { "" }
    }
}

[pscustomobject]@{
    ok = $true
    installed = ($Rows.Count -gt 0)
    taskPrefix = $TaskPrefix
    taskCount = $Rows.Count
    tasks = $Rows
} | ConvertTo-Json -Depth 6

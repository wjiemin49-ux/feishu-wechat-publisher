param(
    [string]$TaskPrefix = "XHSWorkflowFourDayPrepublish"
)

$Tasks = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "$TaskPrefix*" } | Sort-Object TaskName
$Removed = @()

foreach ($Task in $Tasks) {
    Unregister-ScheduledTask -TaskName $Task.TaskName -Confirm:$false
    $Removed += $Task.TaskName
}

[pscustomobject]@{
    ok = $true
    taskPrefix = $TaskPrefix
    removedCount = $Removed.Count
    removed = $Removed
} | ConvertTo-Json -Depth 4

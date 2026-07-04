param(
    [string]$TaskName = "XHSWorkflowDailyPrepublish"
)

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    $Removed = $true
} else {
    $Removed = $false
}

[pscustomobject]@{
    ok = $true
    taskName = $TaskName
    removed = $Removed
} | ConvertTo-Json -Depth 4

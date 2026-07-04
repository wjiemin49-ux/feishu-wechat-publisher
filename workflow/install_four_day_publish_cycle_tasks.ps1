param(
    [string]$TaskPrefix = "XHSWorkflowFourDayPrepublish",
    [string]$Day1Date = "",
    [string]$Platforms = "wechat",
    [int]$Count = 1
)

$ErrorActionPreference = "Stop"
$Runner = Join-Path $PSScriptRoot "run_daily_publish_once.ps1"
if (-not (Test-Path $Runner)) {
    throw "missing daily runner: $Runner"
}

if (-not $Day1Date) {
    $Day1Date = (Get-Date).Date.AddDays(1).ToString("yyyy-MM-dd")
}
$Day1 = [datetime]::ParseExact($Day1Date, "yyyy-MM-dd", $null)

$Slots = @(
    [pscustomobject]@{ Day = 1; Offset = 0; Time = "11:37"; Label = "D1_1137" },
    [pscustomobject]@{ Day = 1; Offset = 0; Time = "22:22"; Label = "D1_2222" },
    [pscustomobject]@{ Day = 2; Offset = 1; Time = "07:01"; Label = "D2_0701" },
    [pscustomobject]@{ Day = 2; Offset = 1; Time = "19:08"; Label = "D2_1908" },
    [pscustomobject]@{ Day = 3; Offset = 2; Time = "11:10"; Label = "D3_1110" },
    [pscustomobject]@{ Day = 3; Offset = 2; Time = "22:20"; Label = "D3_2220" },
    [pscustomobject]@{ Day = 4; Offset = 3; Time = "10:00"; Label = "D4_1000" },
    [pscustomobject]@{ Day = 4; Offset = 3; Time = "23:00"; Label = "D4_2300" }
)

$ActionArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" -Platforms `"$Platforms`" -Count $Count"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $ActionArgs
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
$Installed = @()

foreach ($Slot in $Slots) {
    $Parts = $Slot.Time.Split(":")
    $At = $Day1.AddDays($Slot.Offset).Date.AddHours([int]$Parts[0]).AddMinutes([int]$Parts[1])
    $TaskName = "{0}_{1}" -f $TaskPrefix, $Slot.Label
    $Trigger = New-ScheduledTaskTrigger -Daily -DaysInterval 4 -At $At
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Four-day content generation and publish precheck cycle; final publish still requires Feishu confirmation." -Force | Out-Null
    $Installed += [pscustomobject]@{
        taskName = $TaskName
        day = $Slot.Day
        firstRunAt = $At.ToString("yyyy-MM-dd HH:mm")
        repeatsEveryDays = 4
        platforms = $Platforms
        count = $Count
        cleanupAfter = $false
    }
}

[pscustomobject]@{
    ok = $true
    taskPrefix = $TaskPrefix
    day1Date = $Day1.ToString("yyyy-MM-dd")
    finalPublishRequiresFeishuConfirm = $true
    tasks = $Installed
} | ConvertTo-Json -Depth 6

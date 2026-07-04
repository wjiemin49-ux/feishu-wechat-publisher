param(
    [string]$MumuCli = "D:\Program Files\Netease\MuMuPlayer\nx_main\mumu-cli.exe",
    [string]$MumuIndex = "0",
    [string]$MumuRoot = "D:\Program Files\Netease\MuMuPlayer"
)

$ErrorActionPreference = "Stop"
$Result = [ordered]@{
    ok = $true
    mumuShutdownAttempted = $false
    mumuShutdownOk = $false
    actions = @()
    warnings = @()
}

if (Test-Path $MumuCli) {
    $Result.mumuShutdownAttempted = $true
    $Output = & $MumuCli control --vmindex $MumuIndex shutdown 2>&1
    $ExitCode = $LASTEXITCODE
    $Ok = ($ExitCode -eq 0)
    $Result.mumuShutdownOk = $Ok
    $Result.actions += [pscustomobject]@{
        name = "mumu_shutdown"
        ok = $Ok
        exitCode = $ExitCode
    }
} else {
    $Result.warnings += "mumu_cli_missing"
}

$Processes = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    try {
        $_.Path -and $_.Path.StartsWith($MumuRoot, [System.StringComparison]::OrdinalIgnoreCase)
    }
    catch {
        $false
    }
}

foreach ($Process in $Processes) {
    try {
        Stop-Process -Id $Process.Id -Force -ErrorAction Stop
        $Result.actions += [pscustomobject]@{
            name = "mumu_process_stop"
            ok = $true
            processName = $Process.ProcessName
            id = $Process.Id
        }
    }
    catch {
        $Result.actions += [pscustomobject]@{
            name = "mumu_process_stop"
            ok = $false
            processName = $Process.ProcessName
            id = $Process.Id
        }
    }
}

$Result | ConvertTo-Json -Depth 6

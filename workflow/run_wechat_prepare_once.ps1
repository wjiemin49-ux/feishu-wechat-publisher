param(
    [switch]$CleanupAfter
)

$ErrorActionPreference = "Stop"
$WorkflowDir = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $WorkflowDir "..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$LogDir = Join-Path $WorkflowDir "state"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Stdout = Join-Path $LogDir "wechat_prepare_$Stamp.stdout.log"
$Stderr = Join-Path $LogDir "wechat_prepare_$Stamp.stderr.log"
$CleanupLog = Join-Path $LogDir "wechat_prepare_$Stamp.cleanup.log"

$ExitCode = 0
Push-Location $RepoRoot
try {
    & $Python .\workflow\workflow.py wechat-mp-prepare 1> $Stdout 2> $Stderr
    $ExitCode = $LASTEXITCODE
}
catch {
    $ExitCode = 1
    $_ | Out-String | Set-Content -LiteralPath $Stderr -Encoding UTF8
}
finally {
    Pop-Location
    if ($CleanupAfter) {
        $CleanupScript = Join-Path $WorkflowDir "cleanup_publish_background.ps1"
        if (Test-Path $CleanupScript) {
            try {
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $CleanupScript 1> $CleanupLog 2>&1
            }
            catch {
                $_ | Out-String | Set-Content -LiteralPath $CleanupLog -Encoding UTF8
            }
        }
    }
}

exit $ExitCode

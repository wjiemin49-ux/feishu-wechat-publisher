param(
  [Parameter(Mandatory = $true)]
  [string]$ReferenceImage,

  [Parameter(Mandatory = $true)]
  [string]$Prompt,

  [Parameter(Mandatory = $true)]
  [string]$OutputImage,

  [string]$JsonlLog = "",

  [string]$CodexHome = "",

  [string]$CodexExe = "",

  [string]$Workspace = "",

  [string[]]$DisableFeatures = @("apps", "plugins", "enable_mcp_apps"),

  [string[]]$ConfigOverrides = @('model_reasoning_effort="low"'),

  [int]$NoOutputTimeoutSeconds = 480
)

$ErrorActionPreference = "Stop"

if ($Workspace -eq "") {
  $Workspace = Split-Path -Parent $MyInvocation.MyCommand.Path
}

if ($CodexHome -eq "" -and $env:CODEX_HOME) {
  $CodexHome = $env:CODEX_HOME
}

function New-RequestLine {
  param(
    [Parameter(Mandatory = $true)] $Id,
    [Parameter(Mandatory = $true)] [string]$Method,
    [Parameter(Mandatory = $true)] $Params
  )
  return (@{ id = $Id; method = $Method; params = $Params } | ConvertTo-Json -Depth 80 -Compress)
}

function Write-LogLine {
  param([string]$Line)
  if ($JsonlLog -ne "") {
    Add-Content -LiteralPath $JsonlLog -Value $Line -Encoding UTF8
  }
}

function ConvertFrom-JsonCompat {
  param([Parameter(Mandatory = $true)] [string]$Json)

  if ($PSVersionTable.PSVersion.Major -ge 6) {
    return ($Json | ConvertFrom-Json -Depth 80)
  }
  return ($Json | ConvertFrom-Json)
}

function Read-JsonLine {
  param(
    [Parameter(Mandatory = $true)] [System.Diagnostics.Process]$Process,
    [string]$UntilId = ""
  )

  while ($true) {
    $line = $Process.StandardOutput.ReadLine()
    if ($null -eq $line) {
      return $null
    }
    Write-LogLine $line

    try {
      $obj = ConvertFrom-JsonCompat $line
    } catch {
      continue
    }

    if ($UntilId -eq "") {
      return $obj
    }

    if ($null -ne $obj.id -and [string]$obj.id -eq $UntilId) {
      return $obj
    }
  }
}

function Send-RequestLine {
  param(
    [Parameter(Mandatory = $true)] [System.Diagnostics.Process]$Process,
    [Parameter(Mandatory = $true)] [string]$Line,
    [System.IO.TextWriter]$InputWriter = $null
  )

  if ($null -eq $Process) {
    throw "Codex app-server process is not available."
  }
  if ($null -eq $InputWriter) {
    $InputWriter = $Process.StandardInput
  }
  if ($null -eq $InputWriter) {
    throw "Codex app-server standard input is not available."
  }
  $InputWriter.WriteLine($Line)
  $InputWriter.Flush()
}

function Save-ImageItem {
  param(
    [Parameter(Mandatory = $true)] $Item,
    [Parameter(Mandatory = $true)] [string]$TargetPath
  )

  $targetDir = Split-Path -Parent $TargetPath
  New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

  if ($null -ne $Item.savedPath -and [string]$Item.savedPath -ne "") {
    $sourcePath = [string]$Item.savedPath
    if (Test-Path -LiteralPath $sourcePath) {
      if ((Resolve-Path -LiteralPath $sourcePath).Path -ne (Resolve-Path -LiteralPath $targetDir).Path) {
        Copy-Item -LiteralPath $sourcePath -Destination $TargetPath -Force
      }
      return @{ ok = $true; source = "savedPath"; value = $sourcePath }
    }
  }

  if ($null -ne $Item.result -and [string]$Item.result -ne "") {
    $result = [string]$Item.result
    if ($result -match '^data:image/[^;]+;base64,(.+)$') {
      $bytes = [Convert]::FromBase64String($Matches[1])
      [System.IO.File]::WriteAllBytes($TargetPath, $bytes)
      return @{ ok = $true; source = "data-uri"; value = "result" }
    }
    if ($result -match '^[A-Za-z0-9+/=\r\n]+$') {
      $bytes = [Convert]::FromBase64String(($result -replace '\s+', ''))
      [System.IO.File]::WriteAllBytes($TargetPath, $bytes)
      return @{ ok = $true; source = "base64"; value = "result" }
    }
  }

  return @{ ok = $false; source = ""; value = "" }
}

function Resolve-CodexExe {
  param([string]$Preferred)

  if ($Preferred -ne "" -and (Test-Path -LiteralPath $Preferred)) {
    return $Preferred
  }

  $roots = New-Object System.Collections.Generic.List[string]
  if ($Preferred -ne "") {
    try {
      $dir = [System.IO.FileInfo]::new($Preferred).Directory
      for ($i = 0; $i -lt 3 -and $null -ne $dir; $i++) {
        $dir = $dir.Parent
      }
      if ($null -ne $dir) {
        $roots.Add($dir.FullName)
      }
    } catch {
    }
  }
  if ($env:USERPROFILE) {
    $roots.Add((Join-Path $env:USERPROFILE ".vscode\extensions"))
    $roots.Add((Join-Path $env:USERPROFILE ".vscode-insiders\extensions"))
  }

  $seen = @{}
  foreach ($root in $roots) {
    if ($seen.ContainsKey($root) -or !(Test-Path -LiteralPath $root)) {
      continue
    }
    $seen[$root] = $true
    $candidates = @(Get-ChildItem -LiteralPath $root -Directory -Filter "openai.chatgpt-*-win32-x64" -ErrorAction SilentlyContinue |
      ForEach-Object {
        Join-Path $_.FullName "bin\windows-x86_64\codex.exe"
      } |
      Where-Object {
        Test-Path -LiteralPath $_
      } |
      Sort-Object -Descending)
    if ($candidates.Count -gt 0) {
      return $candidates[0]
    }
  }

  return $Preferred
}

function ConvertTo-ProcessArgumentString {
  param([Parameter(Mandatory = $true)] [string[]]$Arguments)

  $quoted = foreach ($arg in $Arguments) {
    if ($null -eq $arg) {
      '""'
    } elseif ($arg -notmatch '[\s"]') {
      $arg
    } else {
      '"' + ($arg -replace '"', '\"') + '"'
    }
  }
  return ($quoted -join " ")
}

function Set-ProcessStartInfoEncodingIfPresent {
  param(
    [Parameter(Mandatory = $true)] [System.Diagnostics.ProcessStartInfo]$StartInfo,
    [Parameter(Mandatory = $true)] [string]$PropertyName,
    [Parameter(Mandatory = $true)] [System.Text.Encoding]$Encoding
  )

  $property = $StartInfo.GetType().GetProperty($PropertyName)
  if ($null -ne $property -and $property.CanWrite) {
    $property.SetValue($StartInfo, $Encoding, $null)
  }
}

function Stop-ProcessCompat {
  param([System.Diagnostics.Process]$Process)

  if ($null -eq $Process) {
    return
  }

  try {
    if (!$Process.HasExited) {
      try {
        $Process.Kill($true)
      } catch {
        try {
          $Process.Kill()
        } catch {
        }
      }
      try {
        $Process.WaitForExit()
      } catch {
      }
    }
  } catch {
  }
}

$CodexExe = Resolve-CodexExe $CodexExe
if ($CodexExe -eq "") {
  $codexCommand = Get-Command codex -ErrorAction SilentlyContinue
  if ($null -ne $codexCommand) {
    $CodexExe = $codexCommand.Source
  }
}

if (!(Test-Path -LiteralPath $CodexExe)) {
  throw "Codex executable not found. Pass -CodexExe or install codex on PATH."
}
if (!(Test-Path -LiteralPath $CodexHome)) {
  throw "CODEX_HOME not found. Pass -CodexHome or set CODEX_HOME."
}
if (!(Test-Path -LiteralPath $ReferenceImage)) {
  throw "Reference image not found: $ReferenceImage"
}

if ($JsonlLog -eq "") {
  $JsonlLog = Join-Path (Split-Path -Parent $OutputImage) "app_server_events.jsonl"
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $JsonlLog) | Out-Null
if (Test-Path -LiteralPath $JsonlLog) {
  Remove-Item -LiteralPath $JsonlLog -Force
}

$env:CODEX_HOME = $CodexHome

$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $CodexExe
$processArgs = New-Object System.Collections.Generic.List[string]
foreach ($arg in @("app-server", "--stdio", "--analytics-default-enabled")) {
  $processArgs.Add($arg)
}
foreach ($override in $ConfigOverrides) {
  $processArgs.Add("-c")
  $processArgs.Add($override)
}
foreach ($feature in $DisableFeatures) {
  $processArgs.Add("--disable")
  $processArgs.Add($feature)
}
$psi.Arguments = ConvertTo-ProcessArgumentString ([string[]]$processArgs.ToArray())
$psi.WorkingDirectory = $Workspace
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
Set-ProcessStartInfoEncodingIfPresent $psi "StandardInputEncoding" $utf8NoBom
if ($PSVersionTable.PSVersion.Major -ge 6) {
  Set-ProcessStartInfoEncodingIfPresent $psi "StandardOutputEncoding" $utf8NoBom
  Set-ProcessStartInfoEncodingIfPresent $psi "StandardErrorEncoding" $utf8NoBom
}
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true

$process = $null
$inputWriter = $null
$imageResult = $null
$turnCompleted = $false
$turnFailed = $false
$threadId = ""
$agentFinalText = ""

try {
  $process = [System.Diagnostics.Process]::Start($psi)
  if ($null -eq $process) {
    throw "Failed to start Codex app-server process."
  }
  if ($null -eq $process.StandardInput) {
    throw "Codex app-server standard input is not available."
  }
  $inputWriter = $process.StandardInput
  if ($null -eq $psi.GetType().GetProperty("StandardInputEncoding")) {
    $inputWriter = [System.IO.StreamWriter]::new($process.StandardInput.BaseStream, $utf8NoBom)
  }

  $initialize = New-RequestLine 1 "initialize" @{
    clientInfo = @{ name = "vs-route-probe"; version = "0.1" }
    capabilities = @{ experimentalApi = $true }
  }
  Send-RequestLine $process $initialize $inputWriter
  $initResponse = Read-JsonLine $process "1"

  $threadReq = New-RequestLine 3 "thread/start" @{
    cwd = $Workspace
    runtimeWorkspaceRoots = @($Workspace)
    approvalPolicy = "never"
    sandbox = "danger-full-access"
    ephemeral = $true
    model = $null
    modelProvider = $null
    serviceTier = "default"
  }
  Send-RequestLine $process $threadReq $inputWriter
  $threadResponse = Read-JsonLine $process "3"
  if ($null -eq $threadResponse -or $null -eq $threadResponse.result.thread.id) {
    throw "thread/start did not return a thread id."
  }
  $threadId = [string]$threadResponse.result.thread.id

  $agentPrompt = @"
Automation image generation task.

Use the built-in image_gen / image_generation tool directly.
Do not run shell commands.
Do not read skill files, docs, config files, credentials, source files, or project files.
Do not explain the workflow.
The attached local image is a style/composition reference only.
Generate exactly one vertical PNG image from this spec:

$Prompt
"@

  $turnReq = New-RequestLine 4 "turn/start" @{
    threadId = $threadId
    input = @(
      @{ type = "text"; text = $agentPrompt; text_elements = @() },
      @{ type = "localImage"; path = $ReferenceImage; detail = "high" }
    )
    approvalPolicy = "never"
    effort = "low"
    cwd = $Workspace
    runtimeWorkspaceRoots = @($Workspace)
    serviceTier = "default"
  }
  Send-RequestLine $process $turnReq $inputWriter

  while ($true) {
    $readTask = $process.StandardOutput.ReadLineAsync()
    if (!$readTask.Wait($NoOutputTimeoutSeconds * 1000)) {
      throw "No app-server stdout for $NoOutputTimeoutSeconds seconds after turn/start."
    }
    $line = $readTask.Result
    if ($null -eq $line) {
      break
    }
    Write-LogLine $line

    try {
      $obj = ConvertFrom-JsonCompat $line
    } catch {
      continue
    }

    if ($obj.method -eq "item/completed" -and $null -ne $obj.params.item) {
      $item = $obj.params.item
      if ($item.type -eq "agentMessage" -and [string]$item.phase -eq "final_answer") {
        $agentFinalText = [string]$item.text
      }
      if ($item.type -eq "imageGeneration") {
        $saved = Save-ImageItem $item $OutputImage
        $imageResult = @{
          saved = $saved
          status = $item.status
          revisedPrompt = $item.revisedPrompt
        }
      }
    }

    if ($obj.method -eq "turn/completed") {
      $turnCompleted = $true
      if ($null -ne $obj.params.turn.error) {
        $turnFailed = $true
      }
      break
    }
  }
} finally {
  if ($null -ne $process) {
    try {
      if ($null -ne $inputWriter) {
        $inputWriter.Close()
      } elseif ($null -ne $process.StandardInput) {
        $process.StandardInput.Close()
      }
    } catch {
    }
    Stop-ProcessCompat $process
  }
}

$stderr = ""
$exitCode = $null
if ($null -ne $process) {
  try {
    if ($null -ne $process.StandardError) {
      $stderr = $process.StandardError.ReadToEnd()
    }
  } catch {
  }
  try {
    $exitCode = $process.ExitCode
  } catch {
  }
}

$summary = [ordered]@{
  ok = ($null -ne $imageResult -and $true -eq $imageResult.saved.ok -and (Test-Path -LiteralPath $OutputImage))
  outputImage = $OutputImage
  jsonlLog = $JsonlLog
  threadId = $threadId
  turnCompleted = $turnCompleted
  turnFailed = $turnFailed
  imageResult = $imageResult
  agentFinalText = $agentFinalText
  stderr = $stderr
  exitCode = $exitCode
  codexHome = $CodexHome
  codexExe = $CodexExe
}

$summary | ConvertTo-Json -Depth 20

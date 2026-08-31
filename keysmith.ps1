[CmdletBinding()]
param(
  [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
  [string]$ProjectRoot = $(if ($env:CODEX_INSTALL_PROJECT_ROOT) { $env:CODEX_INSTALL_PROJECT_ROOT } else { '' }),
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$RemainingArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptDir = $PSScriptRoot
$instructScript = Join-Path $scriptDir 'codex-instruct.py'
if (-not (Test-Path -LiteralPath $instructScript -PathType Leaf)) {
  $instructScript = Join-Path $scriptDir 'payload\codex-home\keysmith\codex-instruct.py'
}
if (-not (Test-Path -LiteralPath $instructScript -PathType Leaf)) {
  throw "codex-instruct.py is missing from package: $instructScript"
}

$pythonExe = 'python'
if (-not (Get-Command $pythonExe -ErrorAction SilentlyContinue)) {
  if (Get-Command 'py' -ErrorAction SilentlyContinue) {
    $pythonExe = 'py'
  } else {
    throw 'Python 3.10+ is required to execute codex-keysmith.'
  }
}

$effectiveArgs = [Collections.Generic.List[string]]::new()
if ($RemainingArgs) {
  foreach ($arg in $RemainingArgs) { $effectiveArgs.Add([string]$arg) }
}

$hasCodexDir = $false
$hasTargetDir = $false
$isScenarioOrScaffoldCommand = $false

foreach ($arg in $effectiveArgs) {
  if ($arg -eq '--codex-dir' -or $arg.StartsWith('--codex-dir=')) { $hasCodexDir = $true }
  if ($arg -eq '--target-dir' -or $arg.StartsWith('--target-dir=')) { $hasTargetDir = $true }
  if ($arg -in @('--scenario-list', '--deploy-scenario', '--scenario-status', '--scenario-uninstall', '--scenario-recover', '--scaffold', '--scaffold-list', '--scaffold-uninstall', '--version', '--help', '-h')) {
    $isScenarioOrScaffoldCommand = $true
  }
}

if (-not $hasCodexDir -and -not $isScenarioOrScaffoldCommand -and -not [string]::IsNullOrWhiteSpace($CodexHome)) {
  $resolvedCodexHome = [IO.Path]::GetFullPath($CodexHome)
  $effectiveArgs.Add('--codex-dir')
  $effectiveArgs.Add($resolvedCodexHome)
}

if (-not $hasTargetDir -and -not [string]::IsNullOrWhiteSpace($ProjectRoot)) {
  $resolvedProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
  $effectiveArgs.Add('--target-dir')
  $effectiveArgs.Add($resolvedProjectRoot)
}

& $pythonExe $instructScript @effectiveArgs
exit $LASTEXITCODE

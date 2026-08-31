[CmdletBinding()]
param(
  [string]$RepoRoot = '',
  [string]$PythonExe = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = Split-Path -Parent $scriptDirectory }
$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
if ([string]::IsNullOrWhiteSpace($PythonExe)) {
  if (Get-Command python -ErrorAction SilentlyContinue) { $PythonExe = 'python' }
  elseif (Get-Command py -ErrorAction SilentlyContinue) { $PythonExe = 'py' }
  else { throw 'Python is required for the package test suite, but neither python nor py is available.' }
}
$passed = 0

function Invoke-TestStep {
  param(
    [Parameter(Mandatory)][string]$Name,
    [Parameter(Mandatory)][scriptblock]$Command
  )
  $nativePreference = $ErrorActionPreference
  try {
    # unittest writes normal progress to stderr. Capture native stderr without
    # allowing Windows PowerShell to promote it to NativeCommandError.
    $ErrorActionPreference = 'Continue'
    $output = @(& $Command 2>&1 | ForEach-Object { [string]$_ })
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $nativePreference
  }
  if ($null -eq $code) { $code = 0 }
  if ($code -ne 0) {
    $output | Select-Object -Last 80 | ForEach-Object { Write-Output $_ }
    throw "Test step failed: $Name (exit $code)"
  }
  $script:passed++
  $last = $(if ($output.Count) { ($output[-1] -replace '[\r\n]+', ' ').Trim() } else { 'no-output' })
  Write-Output "TEST_STEP=PASS name=$Name exit=0 result=$last"
}

Invoke-TestStep 'metadata' {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot 'scripts\Update-PackageMetadata.ps1') -RepoRoot $RepoRoot -Check
}
Invoke-TestStep 'package' {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot 'verify.ps1')
}
Invoke-TestStep 'public-release' {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot 'tests\Test-PublicRelease.ps1') -RepoRoot $RepoRoot
}
Invoke-TestStep 'rollback-fixture' {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot 'tests\Test-Rollback.ps1')
}
Invoke-TestStep 'instruction-profile' {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot 'tests\Test-InstructionProfile.ps1') -RepoRoot $RepoRoot
}
Invoke-TestStep 'context-guardian' {
  Push-Location -LiteralPath (Join-Path $RepoRoot 'payload\project\.agents\skills\context-guardian\scripts')
  # The full contextctl suite expects an already installed control-plane repo
  # with a live registry and enabled SKILL.md. The release package intentionally
  # contains neither, so the package-level suite runs the self-contained hook
  # contract here; Test-Rollback covers the installed fixture.
  try { & $PythonExe -m unittest test_rollover_stop_hook.py } finally { Pop-Location }
}
Invoke-TestStep 'codex-continuous' {
  Push-Location -LiteralPath (Join-Path $RepoRoot 'payload\project\.agents\tools\codex-continuous')
  try { & $PythonExe -m unittest test_continuous_cli.py } finally { Pop-Location }
}
Invoke-TestStep 'codex-orchestrator' {
  $oldPythonPath = $env:PYTHONPATH
  Push-Location -LiteralPath (Join-Path $RepoRoot 'payload\project\.agents\tools\codex-orchestrator')
  try {
    $env:PYTHONPATH = (Join-Path (Get-Location).Path 'src')
    & $PythonExe -m unittest discover -s tests -p 'test_*.py'
  } finally {
    $env:PYTHONPATH = $oldPythonPath
    Pop-Location
  }
}

Write-Output "TEST_ALL=PASS steps=$passed failures=0"
exit 0

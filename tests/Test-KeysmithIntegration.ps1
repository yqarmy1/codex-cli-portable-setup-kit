[CmdletBinding()]
param(
  [string]$RepoRoot = '',
  [string]$TemporaryRoot = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
$keysmithScript = Join-Path $RepoRoot 'keysmith.ps1'
if (-not (Test-Path -LiteralPath $keysmithScript -PathType Leaf)) { throw "keysmith.ps1 is missing: $keysmithScript" }
if ([string]::IsNullOrWhiteSpace($TemporaryRoot)) { $TemporaryRoot = [IO.Path]::GetTempPath() }
$TemporaryRoot = [IO.Path]::GetFullPath($TemporaryRoot)
New-Item -ItemType Directory -Path $TemporaryRoot -Force | Out-Null

function Assert-True {
  param([Parameter(Mandatory)][bool]$Condition, [Parameter(Mandatory)][string]$Message)
  if (-not $Condition) { throw $Message }
}

function Invoke-Keysmith {
  param([string[]]$Arguments)
  $prev = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    $output = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $keysmithScript @Arguments 2>&1 | ForEach-Object { [string]$_ })
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  if ($null -eq $code) { $code = 0 }
  return [pscustomobject]@{ ExitCode = $code; Output = $output; Text = ($output -join "`n") }
}

$root = Join-Path $TemporaryRoot ('codex-keysmith-test-' + [Guid]::NewGuid().ToString('N'))
try {
  $codexRoot = Join-Path $root 'codex-home'
  $projectRoot = Join-Path $root 'target-project'
  New-Item -ItemType Directory -Path $codexRoot -Force | Out-Null
  New-Item -ItemType Directory -Path $projectRoot -Force | Out-Null
  [IO.File]::WriteAllText((Join-Path $codexRoot 'config.toml'), "model = `"gpt-5.6-sol`"`napproval_policy = `"on-request`"`n", [Text.UTF8Encoding]::new($false))

  Write-Output 'KEYSMITH_TEST_STEP=version'
  $ver = Invoke-Keysmith -Arguments @('--version')
  Assert-True -Condition ($ver.ExitCode -eq 0 -and $ver.Text.Contains('0.5.0')) -Message 'Keysmith version check failed.'

  Write-Output 'KEYSMITH_TEST_STEP=status-initial'
  $statusInit = Invoke-Keysmith -Arguments @('-CodexHome', $codexRoot, '--status', '--lang', 'en')
  Assert-True -Condition ($statusInit.ExitCode -eq 0) -Message 'Initial status check failed.'

  Write-Output 'KEYSMITH_TEST_STEP=deploy-dry-run'
  $dry = Invoke-Keysmith -Arguments @('-CodexHome', $codexRoot, '--preset', 'unrestricted', '--dry-run', '--lang', 'en')
  Assert-True -Condition ($dry.ExitCode -eq 0) -Message 'Dry-run deploy failed.'
  Assert-True -Condition (-not (Test-Path -LiteralPath (Join-Path $codexRoot 'gpt-unrestricted.md'))) -Message 'Dry run created prompt file.'

  Write-Output 'KEYSMITH_TEST_STEP=deploy-apply'
  $applied = Invoke-Keysmith -Arguments @('-CodexHome', $codexRoot, '--preset', 'unrestricted', '--yes', '--lang', 'en')
  Assert-True -Condition ($applied.ExitCode -eq 0) -Message 'Confirmed deploy failed.'
  Assert-True -Condition (Test-Path -LiteralPath (Join-Path $codexRoot 'gpt-unrestricted.md')) -Message 'Prompt file not created after deploy.'
  Assert-True -Condition (Test-Path -LiteralPath (Join-Path $codexRoot '.codex-keysmith-manifest.json')) -Message 'Manifest not created after deploy.'

  Write-Output 'KEYSMITH_TEST_STEP=status-active'
  $statusActive = Invoke-Keysmith -Arguments @('-CodexHome', $codexRoot, '--status', '--lang', 'en')
  Assert-True -Condition ($statusActive.ExitCode -eq 0 -and $statusActive.Text.Contains('active')) -Message 'Status did not report active.'

  Write-Output 'KEYSMITH_TEST_STEP=scenario-list'
  $scenarios = Invoke-Keysmith -Arguments @('--scenario-list', '--lang', 'en')
  Assert-True -Condition ($scenarios.ExitCode -eq 0 -and $scenarios.Text.Contains('example_fixture')) -Message 'Scenario list failed or missing example_fixture.'

  Write-Output 'KEYSMITH_TEST_STEP=scenario-deploy'
  $scenDeploy = Invoke-Keysmith -Arguments @('--deploy-scenario', 'example_fixture', '-ProjectRoot', $projectRoot, '--yes', '--lang', 'en')
  Assert-True -Condition ($scenDeploy.ExitCode -eq 0) -Message "Scenario deploy failed. Code: $($scenDeploy.ExitCode) Output: $($scenDeploy.Text)"
  Assert-True -Condition (Test-Path -LiteralPath (Join-Path $projectRoot '.codex-keysmith\scenario-manifest.json')) -Message 'Scenario manifest missing.'

  Write-Output 'KEYSMITH_TEST_STEP=scenario-status'
  $scenStatus = Invoke-Keysmith -Arguments @('--scenario-status', '-ProjectRoot', $projectRoot, '--lang', 'en')
  Assert-True -Condition ($scenStatus.ExitCode -eq 0 -and $scenStatus.Text.Contains('example_fixture')) -Message 'Scenario status check failed.'

  Write-Output 'KEYSMITH_TEST_STEP=uninstall'
  $uninst = Invoke-Keysmith -Arguments @('-CodexHome', $codexRoot, '--uninstall', '--yes', '--lang', 'en')
  Assert-True -Condition ($uninst.ExitCode -eq 0) -Message 'Uninstall failed.'
  Assert-True -Condition (-not (Test-Path -LiteralPath (Join-Path $codexRoot '.codex-keysmith-manifest.json'))) -Message 'Manifest remains after uninstall.'

  Write-Output 'KEYSMITH_TEST=PASS steps=9'
} finally {
  if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
}

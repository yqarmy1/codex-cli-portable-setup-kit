[CmdletBinding()]
param(
  [string]$RepoRoot = '',
  [string]$TemporaryRoot = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
$tool = Join-Path $RepoRoot 'instruction-profile.ps1'
if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw "Instruction profile tool is missing: $tool" }
if ([string]::IsNullOrWhiteSpace($TemporaryRoot)) { $TemporaryRoot = [IO.Path]::GetTempPath() }
$TemporaryRoot = [IO.Path]::GetFullPath($TemporaryRoot)
New-Item -ItemType Directory -Path $TemporaryRoot -Force | Out-Null

function Assert-True {
  param([Parameter(Mandatory)][bool]$Condition, [Parameter(Mandatory)][string]$Message)
  if (-not $Condition) { throw $Message }
}

function Invoke-Profile {
  param([string[]]$Arguments)
  $output = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $tool @Arguments 2>&1 | ForEach-Object { [string]$_ })
  if ($LASTEXITCODE -ne 0) { throw "instruction-profile.ps1 failed ($LASTEXITCODE): $($output -join [Environment]::NewLine)" }
  return $output
}

$root = Join-Path $TemporaryRoot ('codex-portable-instruction-profile-' + [Guid]::NewGuid().ToString('N'))
try {
  $codexRoot = Join-Path $root 'codex-home'
  $instructions = Join-Path $codexRoot 'instructions'
  $source = Join-Path $root 'source.md'
  New-Item -ItemType Directory -Path $instructions -Force | Out-Null
  [IO.File]::WriteAllText((Join-Path $codexRoot 'config.toml'), "model = `"test`"`nmodel_instructions_file = `"./old.md`"`n[features]`nhooks = true`n", (New-Object System.Text.UTF8Encoding($false)))
  [IO.File]::WriteAllText($source, "# managed profile`n", (New-Object System.Text.UTF8Encoding($false)))
  $managed = Join-Path $instructions 'portable-agent-instructions.md'
  $manifest = Join-Path $codexRoot '.codex-portable-setup-kit-instruction-manifest.json'

  Write-Output 'INSTRUCTION_PROFILE_TEST_STEP=deploy-preview'
  $preview = Invoke-Profile -Arguments @('-CodexHome', $codexRoot, '-SourceFile', $source)
  Assert-True -Condition (-not (Test-Path -LiteralPath $managed)) -Message 'Deploy preview wrote the instruction file.'
  Assert-True -Condition ((Get-Content -LiteralPath (Join-Path $codexRoot 'config.toml') -Raw) -match 'old\.md') -Message 'Deploy preview changed config.toml.'
  Assert-True -Condition (($preview -join "`n") -match 'INSTRUCTION_PROFILE_DEPLOY=preview') -Message 'Deploy preview did not report preview mode.'

  Write-Output 'INSTRUCTION_PROFILE_TEST_STEP=deploy-apply'
  $applied = Invoke-Profile -Arguments @('-CodexHome', $codexRoot, '-SourceFile', $source, '-Yes')
  Assert-True -Condition (Test-Path -LiteralPath $managed -PathType Leaf) -Message 'Confirmed deploy did not write the instruction file.'
  Assert-True -Condition (Test-Path -LiteralPath $manifest -PathType Leaf) -Message 'Confirmed deploy did not write the manifest.'
  Assert-True -Condition ((Get-Content -LiteralPath (Join-Path $codexRoot 'config.toml') -Raw) -match 'model_instructions_file = "\./instructions/portable-agent-instructions\.md"') -Message 'Confirmed deploy did not update config.toml.'
  Assert-True -Condition (($applied -join "`n") -match 'INSTRUCTION_PROFILE_DEPLOY=applied') -Message 'Confirmed deploy did not report success.'

  Write-Output 'INSTRUCTION_PROFILE_TEST_STEP=status'
  $status = Invoke-Profile -Arguments @('-CodexHome', $codexRoot, '-Status')
  Assert-True -Condition (($status -join "`n") -match 'INSTRUCTION_PROFILE_STATUS=active') -Message 'Status did not report an active managed profile.'

  Write-Output 'INSTRUCTION_PROFILE_TEST_STEP=uninstall-preview'
  $uninstallPreview = Invoke-Profile -Arguments @('-CodexHome', $codexRoot, '-Uninstall')
  Assert-True -Condition (Test-Path -LiteralPath $managed -PathType Leaf) -Message 'Uninstall preview removed the instruction file.'
  Assert-True -Condition (($uninstallPreview -join "`n") -match 'INSTRUCTION_PROFILE_UNINSTALL=preview') -Message 'Uninstall preview did not report preview mode.'

  Write-Output 'INSTRUCTION_PROFILE_TEST_STEP=uninstall-apply'
  $uninstalled = Invoke-Profile -Arguments @('-CodexHome', $codexRoot, '-Uninstall', '-Yes')
  Assert-True -Condition (-not (Test-Path -LiteralPath $managed)) -Message 'Confirmed uninstall did not remove the managed instruction.'
  Assert-True -Condition (-not (Test-Path -LiteralPath $manifest)) -Message 'Confirmed uninstall did not remove the manifest.'
  Assert-True -Condition ((Get-Content -LiteralPath (Join-Path $codexRoot 'config.toml') -Raw) -match 'model_instructions_file = "\./old\.md"') -Message 'Confirmed uninstall did not restore the previous reference.'
  Assert-True -Condition (($uninstalled -join "`n") -match 'INSTRUCTION_PROFILE_UNINSTALL=applied') -Message 'Confirmed uninstall did not report success.'

  Write-Output 'INSTRUCTION_PROFILE_TEST_STEP=recover-empty'
  $recover = Invoke-Profile -Arguments @('-CodexHome', $codexRoot, '-Recover')
  Assert-True -Condition (($recover -join "`n") -match 'INSTRUCTION_PROFILE_RECOVER=no-transaction') -Message 'Recover did not report the empty journal state.'
  Write-Output 'INSTRUCTION_PROFILE_TEST=PASS cases=5'
} finally {
  if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}

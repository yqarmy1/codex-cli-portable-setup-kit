[CmdletBinding()]
param(
  [string]$Receipt = '',
  [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' })
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $Receipt) {
  $pointer = Join-Path $CodexHome 'migration-backups\codex-cli-portable-setup-kit\last-receipt.txt'
  if (-not (Test-Path -LiteralPath $pointer)) {
    $legacyPointer = Join-Path $CodexHome 'migration-backups\codex-cli-same-rules\last-receipt.txt'
    if (Test-Path -LiteralPath $legacyPointer) { $pointer = $legacyPointer }
  }
  if (-not (Test-Path -LiteralPath $pointer)) { throw "Rollback receipt pointer is missing: $pointer" }
  $Receipt = (Get-Content -LiteralPath $pointer -Raw -Encoding UTF8).Trim()
}
$Receipt = [IO.Path]::GetFullPath($Receipt)
if (-not (Test-Path -LiteralPath $Receipt -PathType Leaf)) { throw "Rollback receipt is missing: $Receipt" }
$data = Get-Content -LiteralPath $Receipt -Raw -Encoding UTF8 | ConvertFrom-Json
$ops = @($data.operations)
[array]::Reverse($ops)
foreach ($op in $ops) {
  $destination = [string]$op.destination
  if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force }
  if ([bool]$op.existed) {
    $backup = [string]$op.backup
    if (-not (Test-Path -LiteralPath $backup)) { throw "Rollback backup is missing: $backup" }
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $backup -Destination $destination -Recurse -Force
  }
}
if ([bool]$data.git_hook.repository -and (Get-Command git -ErrorAction SilentlyContinue)) {
  $repo = [string]$data.project_root
  if ([bool]$data.git_hook.previous_set) {
    & git -C $repo config --local core.hooksPath ([string]$data.git_hook.previous_value)
  } else {
    & git -C $repo config --local --unset core.hooksPath 2>$null
    if ($LASTEXITCODE -notin @(0, 5)) { throw "Failed to unset Git hooksPath, exit $LASTEXITCODE" }
  }
}
Write-Output 'ROLLBACK_RESULT=PASS'
Write-Output "RECEIPT=$Receipt"
Write-Output "RESTORED_OPERATIONS=$($ops.Count)"
Write-Output 'RESTORED_BEHAVIOR=pre-install files and Git hook binding'
exit 0

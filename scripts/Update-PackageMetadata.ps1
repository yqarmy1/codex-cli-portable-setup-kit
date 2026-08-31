[CmdletBinding()]
param(
  [string]$RepoRoot = '',
  [switch]$Check
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = Split-Path -Parent $scriptDirectory }
$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
$utf8NoBom = [Text.UTF8Encoding]::new($false)

function Get-RelativeReleasePath {
  param([Parameter(Mandatory)][string]$Path)
  return [IO.Path]::GetFullPath($Path).Substring($RepoRoot.Length).TrimStart(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
  ).Replace('\', '/')
}

function Get-ComponentName {
  param([Parameter(Mandatory)][string]$RelativePath)
  if ($RelativePath.StartsWith('payload/codex-home/skills/')) { return 'codex-user-skills' }
  if ($RelativePath.StartsWith('payload/codex-home/')) { return 'codex-user' }
  if ($RelativePath.StartsWith('payload/agents-home/')) { return 'agent-user' }
  if ($RelativePath.StartsWith('payload/project/.agents/tools/')) { return 'project-tools' }
  if ($RelativePath.StartsWith('payload/project/.agents/skills/')) { return 'project-skills' }
  if ($RelativePath.StartsWith('payload/project/.codex/')) { return 'project-codex' }
  return 'project-control-plane'
}

function Get-PayloadIndexText {
  $payloadRoot = Join-Path $RepoRoot 'payload'
  $files = @(Get-ChildItem -LiteralPath $payloadRoot -Recurse -Force -File | Where-Object {
    $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -notin @('.pyc', '.pyo')
  } | Sort-Object FullName)
  $entries = foreach ($file in $files) {
    $relative = Get-RelativeReleasePath -Path $file.FullName
    [ordered]@{
      path = $relative
      component = Get-ComponentName -RelativePath $relative
      bytes = [int64]$file.Length
      sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToUpperInvariant()
    }
  }
  $document = [ordered]@{
    schema = 1
    release = '6.3.0'
    generated_at = '2026-08-31'
    file_count = $entries.Count
    total_bytes = [int64](($files | Measure-Object Length -Sum).Sum)
    files = @($entries)
  }
  return ($document | ConvertTo-Json -Depth 6) + "`n"
}

function Get-ManifestText {
  $files = @(Get-ChildItem -LiteralPath $RepoRoot -Recurse -Force -File | Where-Object {
    $relative = Get-RelativeReleasePath -Path $_.FullName
    $relative -notmatch '(^|/)\.git(/|$)' -and
    $relative -notmatch '(^|/)__pycache__(/|$)' -and
    $relative -notmatch '(^|/)\.pytest_cache(/|$)' -and
    [IO.Path]::GetExtension($relative) -notin @('.pyc', '.pyo') -and
    $relative -notin @('MANIFEST.sha256', '_chinese_scan.txt', 'install-last.log') -and
    $relative -notmatch '(^|/)\.publication(/|$)' -and
    $relative -notmatch '\.(zip|7z|rar|tar|tgz)$'
  } | Sort-Object FullName)
  $lines = foreach ($file in $files) {
    $relative = Get-RelativeReleasePath -Path $file.FullName
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToUpperInvariant()
    "$hash  $relative"
  }
  return ($lines -join "`n") + "`n"
}

$payloadIndexPath = Join-Path $RepoRoot 'PAYLOAD_INDEX.json'
$manifestPath = Join-Path $RepoRoot 'MANIFEST.sha256'
$payloadIndexText = Get-PayloadIndexText
$payloadFileCount = [int](($payloadIndexText | ConvertFrom-Json).file_count)

if ($Check) {
  if (-not (Test-Path -LiteralPath $payloadIndexPath -PathType Leaf)) { throw 'PAYLOAD_INDEX.json is missing.' }
  $currentPayloadIndex = [IO.File]::ReadAllText($payloadIndexPath, [Text.Encoding]::UTF8)
  if ($currentPayloadIndex -ne $payloadIndexText) { throw 'PAYLOAD_INDEX.json is stale. Run scripts/Update-PackageMetadata.ps1.' }
} else {
  [IO.File]::WriteAllText($payloadIndexPath, $payloadIndexText, $utf8NoBom)
}

$manifestText = Get-ManifestText
if ($Check) {
  if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'MANIFEST.sha256 is missing.' }
  $currentManifest = [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8)
  if ($currentManifest -ne $manifestText) { throw 'MANIFEST.sha256 is stale. Run scripts/Update-PackageMetadata.ps1.' }
  Write-Output "PACKAGE_METADATA=PASS mode=check payload_files=$payloadFileCount manifest_files=$(($manifestText.TrimEnd() -split "`n").Count)"
  exit 0
}

[IO.File]::WriteAllText($manifestPath, $manifestText, $utf8NoBom)
Write-Output "PACKAGE_METADATA=PASS mode=write payload_files=$payloadFileCount manifest_files=$(($manifestText.TrimEnd() -split "`n").Count)"
exit 0

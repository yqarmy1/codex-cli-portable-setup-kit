[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$InstallScript = Join-Path $PSScriptRoot 'install.ps1'
$Payload = Join-Path $PSScriptRoot 'payload'
$Manifest = Join-Path $PSScriptRoot 'MANIFEST.sha256'
$LogPath = Join-Path $PSScriptRoot 'install-last.log'

function Normalize-ProjectRootInput {
  param([AllowNull()][string]$Value)
  if ($null -eq $Value) { return '' }
  $normalized = $Value.Trim()
  while ($normalized.Length -ge 2) {
    $first = $normalized[0]
    $last = $normalized[$normalized.Length - 1]
    if (($first -eq [char]34 -and $last -eq [char]34) -or ($first -eq [char]39 -and $last -eq [char]39)) {
      $normalized = $normalized.Substring(1, $normalized.Length - 2).Trim()
      continue
    }
    break
  }
  return $normalized
}

try {
  Clear-Host
} catch {}

Write-Host '============================================================'
Write-Host ' Codex CLI Portable Setup Kit'
Write-Host '============================================================'
Write-Host ''
Write-Host 'Enter the target project folder.'
Write-Host 'You can drag the folder into this window and press Enter.'
Write-Host 'Example: C:\your-project'
Write-Host ''

if (-not (Test-Path -LiteralPath $InstallScript -PathType Leaf)) {
  Write-Host '[ERROR] install.ps1 is missing.' -ForegroundColor Red
  Write-Host 'Extract the ENTIRE ZIP first, then run install.cmd again.' -ForegroundColor Yellow
  exit 10
}
if (-not (Test-Path -LiteralPath $Payload -PathType Container)) {
  Write-Host '[ERROR] payload folder is missing.' -ForegroundColor Red
  Write-Host 'Extract the ENTIRE ZIP first, then run install.cmd again.' -ForegroundColor Yellow
  exit 11
}
if (-not (Test-Path -LiteralPath $Manifest -PathType Leaf)) {
  Write-Host '[ERROR] MANIFEST.sha256 is missing.' -ForegroundColor Red
  exit 12
}

$raw = Read-Host 'ProjectRoot'
$projectRoot = Normalize-ProjectRootInput $raw
if ([string]::IsNullOrWhiteSpace($projectRoot)) {
  Write-Host '[ERROR] ProjectRoot cannot be blank in interactive mode.' -ForegroundColor Red
  exit 13
}

try {
  $projectRoot = [IO.Path]::GetFullPath($projectRoot)
} catch {
  Write-Host "[ERROR] Invalid ProjectRoot: $($_.Exception.Message)" -ForegroundColor Red
  exit 14
}

if (-not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
  Write-Host "[ERROR] ProjectRoot does not exist: $projectRoot" -ForegroundColor Red
  exit 15
}

$env:CODEX_INSTALL_PROJECT_ROOT = $projectRoot
Write-Host ''
Write-Host "ProjectRoot: $projectRoot"
Write-Host 'Running installer...'
Write-Host "Full output is also saved to: $LogPath"
Write-Host ''

# ProjectRoot is intentionally passed only through the environment. The child
# PowerShell command line contains no user-controlled path argument, avoiding
# cmd.exe / PowerShell nested-quote and metacharacter parsing problems.
& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $InstallScript 2>&1 |
  Tee-Object -FilePath $LogPath
$rc = $LASTEXITCODE
if ($null -eq $rc) { $rc = 1 }
exit [int]$rc

[CmdletBinding()]
param(
  [string]$ProjectRoot = $(if ($env:CODEX_INSTALL_PROJECT_ROOT) { $env:CODEX_INSTALL_PROJECT_ROOT } else { (Split-Path -Parent $PSScriptRoot) }),
  [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
  [switch]$NoLaunch,
  [Parameter(ValueFromRemainingArguments=$true)][string[]]$CodexArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Codex CLI Portable Setup Kit - TURBO MAX BOUNDARY MODE" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
Write-Host "[1/3] Target Workspace: $ProjectRoot" -ForegroundColor Green

# 1. Run full installer with unrestricted preset
$installScript = Join-Path $PSScriptRoot "install.ps1"
& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $installScript `
  -ProjectRoot $ProjectRoot `
  -CodexHome $CodexHome `
  -KeysmithPreset unrestricted `
  -SkipPlugins `
  -SkipHooksIsolation

# 2. Configure maximum zero-friction autonomy (approval_policy = never)
$configPath = Join-Path $CodexHome "config.toml"
if (Test-Path -LiteralPath $configPath) {
  $configText = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
  # Switch approval_policy to never for zero prompts
  $configText = $configText -replace '(?m)^approval_policy\s*=\s*"[^"]+"', 'approval_policy = "never"'
  # Ensure elevated windows sandbox
  if ($configText -notmatch '\[windows\]') {
    $configText += "`r`n`r`n[windows]`r`nsandbox = ""elevated"""
  }
  [IO.File]::WriteAllText($configPath, $configText, [Text.UTF8Encoding]::new($false))
  Write-Host "[2/3] Maximum Autonomy Active: approval_policy = 'never' (Zero Prompts)" -ForegroundColor Green
}

# 3. Trust the workspace in Codex config
Write-Host "[3/3] Workspace Fully Trusted. Launching Codex..." -ForegroundColor Green
Write-Host ""

if (-not $NoLaunch) {
  Push-Location -LiteralPath $ProjectRoot
  try {
    if (Get-Command codex -ErrorAction SilentlyContinue) {
      if ($CodexArgs -and $CodexArgs.Count -gt 0) {
        & codex @CodexArgs
      } else {
        & codex
      }
    } else {
      Write-Host "[!] Codex CLI not found in PATH. Please run 'npm i -g @openai/codex' or install Node.js." -ForegroundColor Yellow
    }
  } finally {
    Pop-Location
  }
}

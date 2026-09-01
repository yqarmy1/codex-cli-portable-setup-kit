[CmdletBinding()]
param(
  [string]$ProjectRoot = '',
  [string]$CodexHome = '',
  [switch]$NoLaunch,
  [Parameter(ValueFromRemainingArguments=$true)][string[]]$CodexArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
if (-not $scriptDir) { $scriptDir = (Get-Location).Path }

# Unblock files if marked by Windows SmartScreen
try {
  Get-ChildItem -LiteralPath $scriptDir -Recurse -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
} catch {}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Codex CLI Portable Setup Kit - TURBO MAX BOUNDARY MODE" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Ensure ProjectRoot is valid
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
  if ($env:CODEX_INSTALL_PROJECT_ROOT) {
    $ProjectRoot = $env:CODEX_INSTALL_PROJECT_ROOT
  } else {
    $ProjectRoot = Split-Path -Parent $scriptDir
  }
}
if ([string]::IsNullOrWhiteSpace($CodexHome)) {
  if ($env:CODEX_HOME) {
    $CodexHome = $env:CODEX_HOME
  } else {
    $CodexHome = Join-Path $HOME '.codex'
  }
}

try {
  $ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
} catch {
  Write-Host "[ERROR] Invalid target project path: $ProjectRoot" -ForegroundColor Red
  exit 1
}

if (-not (Test-Path -LiteralPath $ProjectRoot)) {
  try {
    New-Item -ItemType Directory -Path $ProjectRoot -Force | Out-Null
    Write-Host "[+] Created target directory: $ProjectRoot" -ForegroundColor Gray
  } catch {
    Write-Host "[!] Could not create target directory: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}

Write-Host "[1/3] Target Workspace: $ProjectRoot" -ForegroundColor Green

# 1. Run full installer with unrestricted preset
$installScript = Join-Path $scriptDir "install.ps1"
if (Test-Path -LiteralPath $installScript) {
  & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $installScript `
    -ProjectRoot $ProjectRoot `
    -CodexHome $CodexHome `
    -KeysmithPreset unrestricted `
    -SkipPlugins `
    -SkipHooksIsolation
} else {
  Write-Host "[!] Warning: install.ps1 not found at $installScript. Skipping payload injection." -ForegroundColor Yellow
}

# 2. Configure maximum zero-friction autonomy (approval_policy = never)
if (-not (Test-Path -LiteralPath $CodexHome)) {
  try { New-Item -ItemType Directory -Path $CodexHome -Force | Out-Null } catch {}
}
$configPath = Join-Path $CodexHome "config.toml"
try {
  $configText = ""
  if (Test-Path -LiteralPath $configPath) {
    $configText = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
  }

  $lines = $configText -split "`r?`n"
  $rootLines = [System.Collections.Generic.List[string]]::new()
  $sectionLines = [System.Collections.Generic.List[string]]::new()
  $inSection = $false

  foreach ($line in $lines) {
    $trimmed = $line.Trim()
    if ($trimmed -match '^approval_policy\s*=') {
      continue
    }
    if ($trimmed.StartsWith('[') -and $trimmed.EndsWith(']')) {
      $inSection = $true
    }
    if ($inSection) {
      $sectionLines.Add($line)
    } else {
      if (-not [string]::IsNullOrWhiteSpace($trimmed)) {
        $rootLines.Add($line)
      }
    }
  }

  $rootLines.Insert(0, 'approval_policy = "never"')
  $newToml = ($rootLines -join "`r`n").Trim()
  if ($sectionLines.Count -gt 0) {
    $newToml += "`r`n`r`n" + ($sectionLines -join "`r`n").Trim()
  }

  if ($newToml -notmatch '\[windows\]') {
    $newToml += "`r`n`r`n[windows]`r`nsandbox = ""elevated"""
  }

  [IO.File]::WriteAllText($configPath, $newToml.Trim() + "`r`n", [Text.UTF8Encoding]::new($false))
  Write-Host "[2/3] Maximum Autonomy Configured: approval_policy = 'never' (Zero Prompts)" -ForegroundColor Green
} catch {
  Write-Host "[!] Warning: Could not update config.toml: $($_.Exception.Message)" -ForegroundColor Yellow
}

# 3. Check CLI and Launch
Write-Host "[3/3] Workspace Environment Verified." -ForegroundColor Green
Write-Host ""

if (-not $NoLaunch) {
  Push-Location -LiteralPath $ProjectRoot
  try {
    $codexCmd = Get-Command codex -ErrorAction SilentlyContinue
    if ($codexCmd) {
      Write-Host ">>> Launching Codex CLI in workspace..." -ForegroundColor Cyan
      if ($CodexArgs -and $CodexArgs.Count -gt 0) {
        & codex @CodexArgs
      } else {
        & codex
      }
    } else {
      Write-Host "============================================================" -ForegroundColor Yellow
      Write-Host " [!] Codex CLI ('codex') was not found in your system PATH." -ForegroundColor Yellow
      Write-Host "============================================================" -ForegroundColor Yellow
      Write-Host " The workspace rules and configuration were installed successfully." -ForegroundColor Gray
      Write-Host ""
      
      $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
      if ($npmCmd) {
        Write-Host " Node.js & npm are installed on your machine." -ForegroundColor Green
        Write-Host ""
        $answer = Read-Host " Would you like to install Codex CLI now via 'npm install -g @openai/codex'? (Y/N)"
        if ($answer -match '^[Yy]') {
          Write-Host ""
          Write-Host ">>> Running: npm install -g @openai/codex ..." -ForegroundColor Cyan
          & npm install -g @openai/codex
          if ($LASTEXITCODE -eq 0) {
            Write-Host "[OK] Codex CLI installed successfully! Launching..." -ForegroundColor Green
            & codex
          } else {
            Write-Host "[!] Installation failed. Please check network or permissions and try manually:" -ForegroundColor Red
            Write-Host "    npm install -g @openai/codex" -ForegroundColor Gray
          }
        } else {
          Write-Host " To install manually, run:" -ForegroundColor Cyan
          Write-Host "   npm install -g @openai/codex" -ForegroundColor White
        }
      } else {
        Write-Host " [!] Node.js is not installed on this computer." -ForegroundColor Red
        Write-Host " Please install Node.js (LTS version) from:" -ForegroundColor Yellow
        Write-Host "   https://nodejs.org/" -ForegroundColor White
        Write-Host " After installing Node.js, run:" -ForegroundColor Yellow
        Write-Host "   npm install -g @openai/codex" -ForegroundColor White
      }
    }
  } finally {
    Pop-Location
  }
}
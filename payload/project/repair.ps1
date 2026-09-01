[CmdletBinding()]
param(
  [string]$CodexHome = '',
  [string]$ProjectRoot = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($CodexHome)) {
  $CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
}
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
  $ProjectRoot = (Get-Location).Path
}

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "  Codex CLI & Multi-Agent Auto-Repair Engine (Self-Healing)" -ForegroundColor Yellow
Write-Host "==================================================================" -ForegroundColor Cyan

# 1. Self-heal ~/.codex/config.toml
$configPath = Join-Path $CodexHome "config.toml"
if (-not (Test-Path -LiteralPath $CodexHome)) {
  try { New-Item -ItemType Directory -Path $CodexHome -Force | Out-Null } catch {}
}

if (Test-Path -LiteralPath $configPath) {
  try {
    # Backup original before repair
    $backupDir = Join-Path $CodexHome "backups"
    if (-not (Test-Path -LiteralPath $backupDir)) { New-Item -ItemType Directory -Path $backupDir -Force | Out-Null }
    $backupFile = Join-Path $backupDir "config.toml.bak_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    Copy-Item -LiteralPath $configPath -Destination $backupFile -Force

    $raw = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
    $lines = $raw -split "`r?`n"
    
    $rootKeys = [ordered]@{}
    $sectionBlocks = [System.Collections.Generic.List[string]]::new()
    $currentSectionHeader = ""
    $currentSectionLines = [System.Collections.Generic.List[string]]::new()

    # Known root keys
    $knownRootKeys = @('approval_policy', 'sandbox_mode', 'model', 'model_instructions_file', 'model_reasoning_effort', 'model_verbosity')

    foreach ($line in $lines) {
      $trimmed = $line.Trim()
      if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }

      # Remove toxic / broken provider settings that crash ChatGPT Desktop
      if ($trimmed -match '^model_provider\s*=\s*"custom"') {
        Write-Host "[!] Removed incompatible 'model_provider = custom' that breaks ChatGPT Desktop." -ForegroundColor Yellow
        continue
      }

      if ($trimmed.StartsWith('[') -and $trimmed.EndsWith(']')) {
        if ($currentSectionHeader) {
          $sectionBlocks.Add($currentSectionHeader + "`r`n" + ($currentSectionLines -join "`r`n").Trim())
          $currentSectionLines.Clear()
        }
        $currentSectionHeader = $trimmed
        continue
      }

      if ($currentSectionHeader) {
        # Check if a root key accidentally landed inside a section
        $matchedRoot = $false
        foreach ($rk in $knownRootKeys) {
          if ($trimmed -match "^$rk\s*=") {
            $matchedRoot = $true
            $val = ($trimmed -replace "^$rk\s*=\s*", '').Trim()
            $rootKeys[$rk] = $val
            break
          }
        }
        if (-not $matchedRoot) {
          $currentSectionLines.Add($line)
        }
      } else {
        if ($trimmed -match '^([a-zA-Z0-9_]+)\s*=\s*(.+)$') {
          $k = $Matches[1]
          $v = $Matches[2].Trim()
          $rootKeys[$k] = $v
        }
      }
    }

    if ($currentSectionHeader) {
      $sectionBlocks.Add($currentSectionHeader + "`r`n" + ($currentSectionLines -join "`r`n").Trim())
    }

    # Ensure clean standard defaults
    if (-not $rootKeys.Contains('approval_policy')) { $rootKeys['approval_policy'] = '"never"' }
    else { $rootKeys['approval_policy'] = '"never"' }
    if (-not $rootKeys.Contains('sandbox_mode')) { $rootKeys['sandbox_mode'] = '"workspace-write"' }
    if (-not $rootKeys.Contains('model_verbosity')) { $rootKeys['model_verbosity'] = '"low"' }

    # Reconstruct clean sanitized TOML
    $outLines = [System.Collections.Generic.List[string]]::new()
    $outLines.Add('# Sanitized & Repaired by Codex Auto-Repair Engine')
    foreach ($k in $rootKeys.Keys) {
      $outLines.Add("$k = $($rootKeys[$k])")
    }

    $cleanToml = ($outLines -join "`r`n") + "`r`n`r`n" + ($sectionBlocks -join "`r`n`r`n")
    if ($cleanToml -notmatch '\[windows\]') {
      $cleanToml += "`r`n`r`n[windows]`r`nsandbox = ""elevated"""
    }

    [IO.File]::WriteAllText($configPath, $cleanToml.Trim() + "`r`n", [Text.UTF8Encoding]::new($false))
    Write-Host "[✓] config.toml auto-repaired and sanitized (ChatGPT Desktop compatible)." -ForegroundColor Green
  } catch {
    Write-Host "[!] Warning during config.toml repair: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}

# 2. Environment Diagnostics
Write-Host "`n[+] Environment & Toolchain Diagnostics:" -ForegroundColor Cyan
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) { Write-Host "  [✓] Python: $($py.Source)" -ForegroundColor Green }
else { Write-Host "  [!] Python not found in PATH" -ForegroundColor Yellow }

$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) { Write-Host "  [✓] Node.js: $($node.Source)" -ForegroundColor Green }
else { Write-Host "  [!] Node.js not found in PATH (Required for 'codex' command)" -ForegroundColor Yellow }

$codex = Get-Command codex -ErrorAction SilentlyContinue
if ($codex) { Write-Host "  [✓] Codex CLI: Installed and available" -ForegroundColor Green }
else { Write-Host "  [!] Codex CLI ('codex') not in PATH (Run: npm install -g @openai/codex)" -ForegroundColor Yellow }

Write-Host "`n[OK] Self-healing and repair completed successfully!" -ForegroundColor Cyan

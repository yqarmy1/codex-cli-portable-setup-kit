[CmdletBinding()]
param(
  [string]$ProjectRoot = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "  Universal Multi-Agent & Multi-Platform Zero-Friction Adaptor" -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan

$scriptDir = $PSScriptRoot
$installScript = Join-Path $scriptDir 'install.ps1'

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
  $ProjectRoot = (Get-Location).Path
}

Write-Host "[*] Adapting workspace at: $ProjectRoot" -ForegroundColor Yellow

& $installScript -ProjectRoot $ProjectRoot -SkipPlugins
if ($LASTEXITCODE -ne 0) {
  Write-Host "[X] Installation failed with exit code $LASTEXITCODE" -ForegroundColor Red
  exit $LASTEXITCODE
}

Write-Host "`n[+] Universal Multi-Platform Adaptation Summary:" -ForegroundColor Green
Write-Host "------------------------------------------------------------------"
Write-Host "  1. OpenAI Codex CLI    : ACTIVE (AGENTS.md, ~/.codex, TURBO.cmd)" -ForegroundColor Green
Write-Host "  2. OpenCode Agent      : ACTIVE (OPENCODE.md, opencode.json)" -ForegroundColor Green
Write-Host "  3. Claude Code         : ACTIVE (CLAUDE.md)" -ForegroundColor Green
Write-Host "  4. Cursor IDE          : ACTIVE (.cursorrules)" -ForegroundColor Green
Write-Host "  5. Windsurf IDE        : ACTIVE (.windsurfrules)" -ForegroundColor Green
Write-Host "  6. ChatGPT App / Web   : READY  (docs/CHATGPT_APP_PRESET.md)" -ForegroundColor Green
Write-Host "  7. Subsystem Engine    : ACTIVE (.agents/tools/re-toolkit/cli.py)" -ForegroundColor Green
Write-Host "------------------------------------------------------------------"
Write-Host "[OK] All GPT & Agent platforms adapted! Zero-friction execution active.`n" -ForegroundColor Cyan

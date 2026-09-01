[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "  Dual-Relay & Model Configuration Wizard (.env)" -ForegroundColor Yellow
Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "Configure independent API Relays for Model A (Probe) and Model B (Executive)." -ForegroundColor Gray
Write-Host "Press [Enter] on any prompt to keep the default.`n" -ForegroundColor Gray

$targetFile = Join-Path (Get-Location).Path ".env"

$defaultUrl = "https://api.openai.com/v1"
$defaultModel = "gpt-4o"

Write-Host "[1/2] Model A (Fast/Cheap Probe - e.g. gpt-4o-mini):" -ForegroundColor Green
$urlA = Read-Host "  Model A Base URL (default: $defaultUrl)"
if ([string]::IsNullOrWhiteSpace($urlA)) { $urlA = $defaultUrl }
$keyA = Read-Host "  Model A API Key (e.g. sk-...)"
$modelA = Read-Host "  Model A Model Name (default: gpt-4o-mini)"
if ([string]::IsNullOrWhiteSpace($modelA)) { $modelA = "gpt-4o-mini" }

Write-Host "`n[2/2] Model B (High-Tier Executive - e.g. gpt-4o):" -ForegroundColor Green
$urlB = Read-Host "  Model B Base URL (default: $urlA)"
if ([string]::IsNullOrWhiteSpace($urlB)) { $urlB = $urlA }
$keyB = Read-Host "  Model B API Key (default: same as Model A)"
if ([string]::IsNullOrWhiteSpace($keyB)) { $keyB = $keyA }
$modelB = Read-Host "  Model B Model Name (default: $defaultModel)"
if ([string]::IsNullOrWhiteSpace($modelB)) { $modelB = $defaultModel }

$content = @"
# Generated via CONFIG_RELAYS Wizard
MODEL_A_BASE_URL=$urlA
MODEL_A_API_KEY=$keyA
MODEL_A_MODEL=$modelA

MODEL_B_BASE_URL=$urlB
MODEL_B_API_KEY=$keyB
MODEL_B_MODEL=$modelB
"@

[IO.File]::WriteAllText($targetFile, $content.Trim(), [Text.UTF8Encoding]::new($false))
Write-Host "`n[OK] Configuration saved successfully to .env!" -ForegroundColor Cyan

[CmdletBinding()]
param(
  [Parameter(Mandatory)][ValidateSet('Baseline', 'Portable')][string]$Mode,
  [Parameter(Mandatory)][string]$ConfigPath,
  [string]$ProjectHookPath = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
$body = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
if ($Mode -eq 'Baseline') {
  foreach ($required in @('model = "gpt-5.6-sol"', 'model_instructions_file = "./gpt-5.6-sol-unrestricted-v45.md"', 'hooks = false')) {
    if (-not $body.Contains($required)) { throw "Baseline setting missing: $required" }
  }
  $hash = (Get-FileHash -LiteralPath $ConfigPath -Algorithm SHA256).Hash
  Write-Output "CONFIG_TEST=PASS mode=Baseline sha256=$hash hooks_user=false"
  exit 0
}
foreach ($required in @('model = "gpt-5.6-sol"', 'model_instructions_file = "./instructions/portable-agent-instructions.md"', 'approval_policy = "on-request"', 'sandbox_mode = "workspace-write"', 'hooks = false')) {
  if (-not $body.Contains($required)) { throw "Portable setting missing: $required" }
}
foreach ($forbidden in @('[projects.', '[mcp_servers.node_repl]', '[shell_environment_policy.set]')) {
  if ($body.Contains($forbidden)) { throw "Nonportable setting remains: $forbidden" }
}
if (-not $ProjectHookPath) { throw 'ProjectHookPath is required for Portable mode.' }
$hook = Get-Content -LiteralPath $ProjectHookPath -Raw -Encoding UTF8 | ConvertFrom-Json
$commandWindows = [string]$hook.hooks.PostCompact[0].hooks[0].commandWindows
if ($commandWindows -ne 'node ".codex\hooks\post-compact.mjs"') { throw "Hook is not portable: $commandWindows" }
$strictExit = 'not-run'
$configStatus = 'not-run'
if (Get-Command codex -ErrorAction SilentlyContinue) {
  $tempHome = Join-Path $PSScriptRoot (".codex-config-test-" + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path (Join-Path $tempHome 'instructions') -Force | Out-Null
  Copy-Item -LiteralPath $ConfigPath -Destination (Join-Path $tempHome 'config.toml')
  $instructionSource = Join-Path (Split-Path -Parent $ConfigPath) 'instructions\portable-agent-instructions.md'
  Copy-Item -LiteralPath $instructionSource -Destination (Join-Path $tempHome 'instructions\portable-agent-instructions.md')
  $old = $env:CODEX_HOME
  try {
    $env:CODEX_HOME = $tempHome
    $doctorErr = Join-Path $tempHome 'doctor.err'
    $doctorLines = @(& codex --strict-config doctor --json 2> $doctorErr)
    $strictExit = $LASTEXITCODE
    $doctorText = ($doctorLines | Out-String).Trim()
    if (-not $doctorText) {
      $detail = $(if (Test-Path -LiteralPath $doctorErr) { (Get-Content -LiteralPath $doctorErr -Raw -Encoding UTF8).Trim() } else { '' })
      throw "Codex doctor produced no JSON output (exit $strictExit). $detail"
    }
    $doctor = $doctorText | ConvertFrom-Json
    $configStatus = [string]$doctor.checks.'config.load'.status
    if ($configStatus -ne 'ok') { throw "Strict config load status is $configStatus (doctor exit $strictExit)" }
  } finally {
    $env:CODEX_HOME = $old
    Remove-Item -LiteralPath $tempHome -Recurse -Force -ErrorAction SilentlyContinue
  }
}
$hash = (Get-FileHash -LiteralPath $ConfigPath -Algorithm SHA256).Hash
Write-Output "CONFIG_TEST=PASS mode=Portable sha256=$hash config_status=$configStatus doctor_exit=$strictExit hook=relative"
exit 0

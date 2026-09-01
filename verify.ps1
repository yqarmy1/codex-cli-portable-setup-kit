[CmdletBinding()]
param(
  [switch]$Installed,
  [string]$ProjectRoot = '',
  [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
  [string]$AgentsHome = (Join-Path $HOME '.agents')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$manifest = Join-Path $PSScriptRoot 'MANIFEST.sha256'
if (-not (Test-Path -LiteralPath $manifest)) { throw "Manifest is missing: $manifest" }
$packageRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar))
$checked = 0
foreach ($line in Get-Content -LiteralPath $manifest -Encoding UTF8) {
  if (-not $line.Trim()) { continue }
  if ($line.Length -lt 67) { throw "Malformed manifest line: $line" }
  $expected = $line.Substring(0, 64).ToUpperInvariant()
  if ($expected -notmatch '^[0-9A-F]{64}$') { throw "Malformed manifest hash: $line" }
  $relative = $line.Substring(66)
  $path = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ($relative.Replace('/', [IO.Path]::DirectorySeparatorChar))))
  $prefix = $packageRoot + [IO.Path]::DirectorySeparatorChar
  if (-not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Manifest path escapes package root: $relative" }
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Manifest file is missing: $relative" }
  $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToUpperInvariant()
  if ($actual -ne $expected) { throw "Hash mismatch: $relative" }
  $checked++
}
if ($checked -eq 0) { throw 'Manifest contains no files.' }
Write-Output "PACKAGE_VERIFY=PASS files=$checked"

if ($Installed) {
  if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
  }
  $ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
  $CodexHome = [IO.Path]::GetFullPath($CodexHome)
  $AgentsHome = [IO.Path]::GetFullPath($AgentsHome)
  foreach ($path in @(
    (Join-Path $CodexHome 'config.toml'),
    (Join-Path $CodexHome 'AGENTS.md'),
    (Join-Path $CodexHome 'instructions\portable-agent-instructions.md'),
    (Join-Path $CodexHome 'rules\default.rules'),
    (Join-Path $ProjectRoot 'AGENTS.md'),
    (Join-Path $ProjectRoot '.codex\hooks.json'),
    (Join-Path $ProjectRoot '.codex\hooks\post-compact.mjs'),
    (Join-Path $ProjectRoot '.agents\git-hooks\pre-commit')
  )) { if (-not (Test-Path -LiteralPath $path)) { throw "Installed file is missing: $path" } }

  $hook = Get-Content -LiteralPath (Join-Path $ProjectRoot '.codex\hooks.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $commandWindows = [string]$hook.hooks.PostCompact[0].hooks[0].commandWindows
  if ($commandWindows -ne 'node ".codex\hooks\post-compact.mjs"') { throw "Unexpected commandWindows: $commandWindows" }

  $config = Get-Content -LiteralPath (Join-Path $CodexHome 'config.toml') -Raw -Encoding UTF8
  if ($config -notmatch '(?m)^model_instructions_file = "(\./instructions/portable-agent-instructions\.md|\./gpt-unrestricted\.md|\./gpt-contract\.md|\./gpt-persona-contract\.md)"$') { throw 'Portable model instruction path is not installed.' }
  if ($config.Contains('__USER_HOME_ESCAPED__')) { throw 'User-home placeholder was not replaced in installed config.' }
  $rules = Get-Content -LiteralPath (Join-Path $CodexHome 'rules\default.rules') -Raw -Encoding UTF8
  if ($rules.Contains('__USER_HOME_ESCAPED__')) { throw 'User-home placeholder remains in installed default.rules.' }
  if ($rules.Contains('__PROJECT_ROOT_ESCAPED__')) { throw 'Project-root placeholder remains in installed default.rules.' }
  if ($rules.Contains('__COMPUTER_NAME__')) { throw 'Computer-name placeholder remains in installed default.rules.' }
  if ($rules.Contains('__WINDOWS_USERNAME__')) { throw 'Windows-username placeholder remains in installed default.rules.' }

  foreach ($root in @((Join-Path $ProjectRoot '.agents'), (Join-Path $ProjectRoot '.codex'), (Join-Path $ProjectRoot 'AGENTS.md'), (Join-Path $ProjectRoot 'CLAUDE.md'), (Join-Path $ProjectRoot 'OPENCODE.md'), (Join-Path $ProjectRoot 'opencode.json'), (Join-Path $ProjectRoot '.cursorrules'), (Join-Path $ProjectRoot '.windsurfrules'), (Join-Path $ProjectRoot '.mcp.json'), (Join-Path $ProjectRoot 'CONFIG_RELAYS.cmd'), (Join-Path $ProjectRoot 'config_relays.ps1'), (Join-Path $ProjectRoot 'REPAIR_ALL.cmd'), (Join-Path $ProjectRoot 'repair.ps1'), (Join-Path $ProjectRoot '.env.example'))) {
    $files = @()
    if (Test-Path -LiteralPath $root -PathType Leaf) { $files = @(Get-Item -LiteralPath $root) }
    elseif (Test-Path -LiteralPath $root -PathType Container) { $files = @(Get-ChildItem -LiteralPath $root -Recurse -File -Force) }
    foreach ($file in $files) {
      if ($file.Extension.ToLowerInvariant() -notin @('.md', '.txt', '.json', '.toml', '.yaml', '.yml', '.ps1', '.py', '.js', '.mjs', '.cmd', '.sh', '')) { continue }
      try { $text = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 } catch { continue }
      if ($text.Contains('__PROJECT_ROOT_WIN__') -or $text.Contains('__PROJECT_ROOT_POSIX__')) {
        throw "Project-root placeholder remains after install: $($file.FullName)"
      }
    }
  }

  $gitState = 'not-a-repository'
  if (Get-Command git -ErrorAction SilentlyContinue) {
    & git -C $ProjectRoot rev-parse --git-dir *> $null
    if ($LASTEXITCODE -eq 0) {
      $gitState = (& git -C $ProjectRoot config --local --get core.hooksPath 2>$null | Out-String).Trim()
      if ($gitState -ne '.agents/git-hooks') { throw "Unexpected Git hooksPath: $gitState" }
    }
  }

  $strictExit = 'not-run'
  $configStatus = 'not-run'
  $projectHooks = 'not-run'
  $codexVersion = 'not-run'
  $locationPushed = $false
  if (Get-Command codex -ErrorAction SilentlyContinue) {
    $oldCodexHome = $env:CODEX_HOME
    try {
      $env:CODEX_HOME = $CodexHome
      Push-Location -LiteralPath $ProjectRoot
      $locationPushed = $true

      $versionErr = Join-Path $CodexHome ('.migration-version-' + [guid]::NewGuid().ToString('N') + '.err')
      try {
        $nativePreference = $ErrorActionPreference
        try {
          $ErrorActionPreference = 'Continue'
          $versionText = (& codex --version 2> $versionErr | Out-String).Trim()
        } finally {
          $ErrorActionPreference = $nativePreference
        }
        $versionExit = $LASTEXITCODE
        if ($versionExit -ne 0) {
          $detail = $(if (Test-Path -LiteralPath $versionErr) { (Get-Content -LiteralPath $versionErr -Raw -Encoding UTF8).Trim() } else { '' })
          throw "codex --version failed with exit $versionExit. $detail"
        }
        $codexVersion = $versionText -replace '[\r\n]+', ' '
      } finally {
        if ([IO.File]::Exists($versionErr)) { [IO.File]::Delete($versionErr) }
      }

      $doctorErr = Join-Path $CodexHome ('.migration-doctor-' + [guid]::NewGuid().ToString('N') + '.err')
      try {
        $nativePreference = $ErrorActionPreference
        try {
          $ErrorActionPreference = 'Continue'
          $doctorLines = @(& codex --strict-config doctor --json 2> $doctorErr)
        } finally {
          $ErrorActionPreference = $nativePreference
        }
        $strictExit = $LASTEXITCODE
        $doctorText = ($doctorLines | Out-String).Trim()
        if (-not $doctorText) {
          $detail = $(if (Test-Path -LiteralPath $doctorErr) { (Get-Content -LiteralPath $doctorErr -Raw -Encoding UTF8).Trim() } else { '' })
          throw "Codex doctor produced no JSON output (exit $strictExit). $detail"
        }
        try { $doctor = $doctorText | ConvertFrom-Json } catch { throw "Codex doctor returned invalid JSON: $($_.Exception.Message)" }
        $configStatus = [string]$doctor.checks.'config.load'.status
        if ($configStatus -ne 'ok') { throw "Codex strict config load status is $configStatus (doctor exit $strictExit)" }
      } finally {
        if ([IO.File]::Exists($doctorErr)) { [IO.File]::Delete($doctorErr) }
      }

      $featureErr = Join-Path $CodexHome ('.migration-features-' + [guid]::NewGuid().ToString('N') + '.err')
      try {
        $nativePreference = $ErrorActionPreference
        try {
          $ErrorActionPreference = 'Continue'
          $featureLines = @(& codex features list 2> $featureErr)
        } finally {
          $ErrorActionPreference = $nativePreference
        }
        $featureExit = $LASTEXITCODE
        if ($featureExit -ne 0) {
          $detail = $(if (Test-Path -LiteralPath $featureErr) { (Get-Content -LiteralPath $featureErr -Raw -Encoding UTF8).Trim() } else { '' })
          throw "Codex feature check failed with exit $featureExit. $detail"
        }
        $featureText = ($featureLines | Out-String)
        $projectHooks = $(if ($featureText -match '(?m)^hooks\s+\S+\s+true\s*$') { 'true' } else { 'false' })
        if ($projectHooks -ne 'true') { throw 'Project hooks were not enabled in the target project.' }
      } finally {
        if ([IO.File]::Exists($featureErr)) { [IO.File]::Delete($featureErr) }
      }
    } finally {
      if ($locationPushed) { Pop-Location }
      $env:CODEX_HOME = $oldCodexHome
    }
  }
  Write-Output "INSTALLED_VERIFY=PASS hook=relative project_hooks=$projectHooks git_hook=$gitState config_status=$configStatus doctor_exit=$strictExit codex=$codexVersion"
}
exit 0

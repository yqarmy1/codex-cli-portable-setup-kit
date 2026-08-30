[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$kit = Split-Path -Parent $PSScriptRoot
$fixtureBase = $(if ($env:TEMP) { $env:TEMP } else { [IO.Path]::GetTempPath() })
$fixture = Join-Path $fixtureBase ("codex-kit-rollback-" + [guid]::NewGuid().ToString('N'))
$codexHome = Join-Path $fixture 'home\.codex'
$agentsHome = Join-Path $fixture 'home\.agents'
$project = Join-Path $fixture 'project'
$receipt = Join-Path $fixture 'receipt.json'
try {
  New-Item -ItemType Directory -Path $codexHome, $agentsHome, $project -Force | Out-Null
  [IO.File]::WriteAllText((Join-Path $codexHome 'config.toml'), "model = `"sentinel`"`n", [Text.UTF8Encoding]::new($false))
  [IO.File]::WriteAllText((Join-Path $codexHome 'AGENTS.md'), "sentinel-global`n", [Text.UTF8Encoding]::new($false))
  [IO.File]::WriteAllText((Join-Path $project 'AGENTS.md'), "sentinel-project`n", [Text.UTF8Encoding]::new($false))
  $configHash = (Get-FileHash -LiteralPath (Join-Path $codexHome 'config.toml') -Algorithm SHA256).Hash
  $globalHash = (Get-FileHash -LiteralPath (Join-Path $codexHome 'AGENTS.md') -Algorithm SHA256).Hash
  $projectHash = (Get-FileHash -LiteralPath (Join-Path $project 'AGENTS.md') -Algorithm SHA256).Hash
  if (Get-Command git -ErrorAction SilentlyContinue) {
    & git -C $project init --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Fixture git init failed.' }
    & git -C $project config --local core.hooksPath .old-hooks
  }
  $installOutput = & (Join-Path $kit 'install.ps1') -ProjectRoot $project -CodexHome $codexHome -AgentsHome $agentsHome -ReceiptPath $receipt -SkipPlugins -SkipCodexCheck
  if ($LASTEXITCODE -ne 0 -or ($installOutput -notcontains 'INSTALL_RESULT=PASS')) { throw 'Fixture install failed.' }
  $hookRuntime = 'not-tested'
  if (Get-Command node -ErrorAction SilentlyContinue) {
    $transcript = Join-Path $project 'fixture-transcript.jsonl'
    [IO.File]::WriteAllText($transcript, "{`"type`":`"fixture`"}`n", [Text.UTF8Encoding]::new($false))
    $eventJson = [ordered]@{ hook_event_name = 'PostCompact'; cwd = $project; session_id = 'rollback-fixture-session'; transcript_path = $transcript; trigger = 'manual'; turn_id = 'fixture-turn' } | ConvertTo-Json -Compress
    $nodePath = (Get-Command node).Source
    $hookPath = Join-Path $project '.codex\hooks\post-compact.mjs'
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $nodePath
    $start.Arguments = '"' + $hookPath + '"'
    $start.UseShellExecute = $false
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    $process.StandardInput.Write($eventJson)
    $process.StandardInput.Close()
    $hookOutput = $process.StandardOutput.ReadToEnd()
    $hookError = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "Fixture hook exited with $($process.ExitCode): $hookError" }
    $hookResult = $hookOutput | ConvertFrom-Json
    if (-not [bool]$hookResult.continue) { throw 'Fixture hook did not return continue=true.' }
    if ($hookResult.PSObject.Properties.Name -contains 'systemMessage') { throw "Fixture hook reported: $($hookResult.systemMessage)" }
    $stateFiles = @(Get-ChildItem -LiteralPath (Join-Path $project '.context\runtime\codex-context-rollover') -Filter state.json -Recurse -File)
    if ($stateFiles.Count -ne 1) { throw "Fixture hook state count was $($stateFiles.Count)." }
    $hookRuntime = 'pass'
  }
  $verifyOutput = & (Join-Path $kit 'verify.ps1') -Installed -ProjectRoot $project -CodexHome $codexHome -AgentsHome $agentsHome
  if ($LASTEXITCODE -ne 0 -or -not ($verifyOutput -match '^INSTALLED_VERIFY=PASS')) { throw 'Fixture installed verification failed.' }
  $rollbackEntry = 'powershell'
  $bashPath = $null
  $bashCommand = Get-Command bash -ErrorAction SilentlyContinue
  if ($bashCommand) { $bashPath = $bashCommand.Source }
  elseif (Test-Path -LiteralPath 'C:\Program Files\Git\bin\bash.exe') { $bashPath = 'C:\Program Files\Git\bin\bash.exe' }
  if ($bashPath) {
    $rollbackEntry = 'ROLLBACK.sh'
    $shellPath = (Join-Path $kit 'ROLLBACK.sh').Replace('\', '/')
    $rollbackOutput = & $bashPath $shellPath -Receipt $receipt -CodexHome $codexHome
  } else {
    $rollbackOutput = & (Join-Path $kit 'rollback.ps1') -Receipt $receipt -CodexHome $codexHome
  }
  if ($LASTEXITCODE -ne 0 -or ($rollbackOutput -notcontains 'ROLLBACK_RESULT=PASS')) { throw 'Fixture rollback failed.' }
  if ((Get-FileHash -LiteralPath (Join-Path $codexHome 'config.toml') -Algorithm SHA256).Hash -ne $configHash) { throw 'Config was not restored.' }
  if ((Get-FileHash -LiteralPath (Join-Path $codexHome 'AGENTS.md') -Algorithm SHA256).Hash -ne $globalHash) { throw 'Global AGENTS was not restored.' }
  if ((Get-FileHash -LiteralPath (Join-Path $project 'AGENTS.md') -Algorithm SHA256).Hash -ne $projectHash) { throw 'Project AGENTS was not restored.' }
  foreach ($added in @((Join-Path $project '.agents'), (Join-Path $project '.codex'), (Join-Path $codexHome 'instructions'))) {
    if (Test-Path -LiteralPath $added) { throw "Added path survived rollback: $added" }
  }
  $gitRestored = 'not-tested'
  if (Get-Command git -ErrorAction SilentlyContinue) {
    $gitRestored = (& git -C $project config --local --get core.hooksPath | Out-String).Trim()
    if ($gitRestored -ne '.old-hooks') { throw "Git hook binding was not restored: $gitRestored" }
  }
  Write-Output "ROLLBACK_TEST=PASS entrypoint=$rollbackEntry hook_runtime=$hookRuntime installed_verify=true config_restored=true global_agents_restored=true project_agents_restored=true added_paths_removed=true git_hook_restored=true"
} finally {
  Remove-Item -LiteralPath $fixture -Recurse -Force -ErrorAction SilentlyContinue
}
exit 0

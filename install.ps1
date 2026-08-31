[CmdletBinding()]
param(
  [string]$ProjectRoot = $(if ($env:CODEX_INSTALL_PROJECT_ROOT) { $env:CODEX_INSTALL_PROJECT_ROOT } else { '' }),
  [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
  [string]$AgentsHome = (Join-Path $HOME '.agents'),
  [string]$ReceiptPath = '',
  [ValidatePattern('^[0-9A-Za-z.+-]+$')][string]$CodexVersion = '0.147.0',
  [switch]$UpgradeCodex,
  [switch]$SkipPlugins,
  [switch]$SkipCodexCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Payload = Join-Path $PSScriptRoot 'payload'

function Normalize-PathArgument {
  param([AllowNull()][string]$Value)
  if ($null -eq $Value) { return '' }
  $normalized = $Value.Trim()
  # Explorer drag-and-drop normally inserts a quoted path. Quotation marks are
  # not valid inside a Windows path and must not reach Path.GetFullPath().
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


function Get-CodexCliVersion {
  if (-not (Get-Command codex -ErrorAction SilentlyContinue)) { return $null }
  $stderrPath = Join-Path ([IO.Path]::GetTempPath()) ('.codex-version-' + [guid]::NewGuid().ToString('N') + '.err')
  try {
    $text = (& codex --version 2> $stderrPath | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { return $null }
    $match = [regex]::Match($text, '(?<!\d)(\d+\.\d+\.\d+)(?!\d)')
    if ($match.Success) { return $match.Groups[1].Value }
    return $text
  } finally {
    if ([IO.File]::Exists($stderrPath)) { [IO.File]::Delete($stderrPath) }
  }
}


function Invoke-CodexNativeCapture {
  param([Parameter(Mandatory)][string[]]$Arguments)
  $previousPreference = $ErrorActionPreference
  try {
    # Windows PowerShell 5.x can turn native stderr redirected with 2>&1 into
    # NativeCommandError when the global preference is Stop. Plugin discovery
    # and installation are optional, so capture native output without allowing
    # stderr alone to abort the entire transactional install.
    $ErrorActionPreference = 'Continue'
    $lines = @(& codex @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    $text = (($lines | ForEach-Object { [string]$_ }) -join [Environment]::NewLine).Trim()
    return [pscustomobject]@{ ExitCode = $exitCode; Output = $text }
  } catch {
    return [pscustomobject]@{ ExitCode = -1; Output = $_.Exception.Message }
  } finally {
    $ErrorActionPreference = $previousPreference
  }
}

function Test-LooksLikeProjectRoot {
  param([Parameter(Mandatory)][string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
  foreach ($marker in @('.git', '.codex', 'AGENTS.md', 'package.json', 'pyproject.toml', 'Cargo.toml', 'go.mod', '.gitignore')) {
    if (Test-Path -LiteralPath (Join-Path $Path $marker)) { return $true }
  }
  return $false
}

function Resolve-ProjectRoot {
  param([string]$Requested)
  $Requested = Normalize-PathArgument $Requested
  if (-not [string]::IsNullOrWhiteSpace($Requested)) {
    return [IO.Path]::GetFullPath($Requested)
  }

  $packageParent = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
  if (Test-LooksLikeProjectRoot $packageParent) { return $packageParent }

  $current = [IO.Path]::GetFullPath((Get-Location).Path)
  if (Test-LooksLikeProjectRoot $current) { return $current }

  throw @"
ProjectRoot could not be detected safely.
Pass the target project explicitly, for example:
  .\install.ps1 -ProjectRoot 'D:\work\your-project'
The installer intentionally refuses to treat an arbitrary parent folder (such as Downloads) as a project.
"@
}

function Assert-OutsidePackageDirectory {
  param([Parameter(Mandatory)][string]$Path)
  $packageRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar))
  $candidate = [IO.Path]::GetFullPath($Path).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar))
  if ($candidate.Equals($packageRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'ProjectRoot cannot be the installer package directory itself.'
  }
  $prefix = $packageRoot + [IO.Path]::DirectorySeparatorChar
  if ($candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'ProjectRoot cannot be inside the installer package directory.'
  }
}

function Test-PackageManifest {
  $manifest = Join-Path $PSScriptRoot 'MANIFEST.sha256'
  if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw "Manifest is missing: $manifest" }
  $packageRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd([char[]]@([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar))
  $checked = 0
  foreach ($line in Get-Content -LiteralPath $manifest -Encoding UTF8) {
    if (-not $line.Trim()) { continue }
    if ($line.Length -lt 67) { throw "Malformed manifest line: $line" }
    $expected = $line.Substring(0, 64).ToUpperInvariant()
    if ($expected -notmatch '^[0-9A-F]{64}$') { throw "Malformed manifest hash: $line" }
    $relative = $line.Substring(66)
    if ([string]::IsNullOrWhiteSpace($relative)) { throw "Manifest path is empty: $line" }
    $path = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ($relative.Replace('/', [IO.Path]::DirectorySeparatorChar))))
    $prefix = $packageRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Manifest path escapes package root: $relative" }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Manifest file is missing: $relative" }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($actual -ne $expected) { throw "Hash mismatch: $relative" }
    $checked++
  }
  if ($checked -eq 0) { throw 'Manifest contains no files.' }
  return $checked
}

$ProjectRoot = Resolve-ProjectRoot $ProjectRoot
$ProjectRoot = [IO.Path]::GetFullPath((Normalize-PathArgument $ProjectRoot))
$CodexHome = [IO.Path]::GetFullPath((Normalize-PathArgument $CodexHome))
$AgentsHome = [IO.Path]::GetFullPath((Normalize-PathArgument $AgentsHome))

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
  throw "ProjectRoot does not exist: $ProjectRoot"
}
if (-not (Test-Path -LiteralPath $Payload -PathType Container)) {
  throw "Package payload is missing: $Payload"
}
Assert-OutsidePackageDirectory $ProjectRoot
$manifestCount = Test-PackageManifest

$detectedCodexVersion = $null
if (-not $SkipCodexCheck) {
  $detectedCodexVersion = Get-CodexCliVersion
  $hasCodex = [bool](Get-Command codex -ErrorAction SilentlyContinue)
  if (-not $hasCodex -or $UpgradeCodex) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
      if ($hasCodex) {
        throw 'npm is unavailable, so the requested Codex CLI upgrade cannot be performed.'
      }
      throw 'Codex and npm are both unavailable. Install Node.js or Codex CLI, then rerun this package.'
    }
    & npm install --global ("@openai/codex@$CodexVersion")
    if ($LASTEXITCODE -ne 0) { throw "Codex CLI installation failed with exit $LASTEXITCODE" }
    if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
      throw 'Codex CLI installation completed but codex is still not visible on PATH. Open a new terminal and rerun the installer.'
    }
    $detectedCodexVersion = Get-CodexCliVersion
  }
  if ($detectedCodexVersion -and $detectedCodexVersion -notin @('0.146.0', '0.147.0')) {
    Write-Warning "Detected Codex CLI $detectedCodexVersion. This package's codex-continuous compatibility matrix is only validated with openai-codex SDK 0.144.4 plus CLI 0.146.0/0.147.0."
  }
}

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$BackupRoot = Join-Path $CodexHome "migration-backups\codex-cli-portable-setup-kit\$timestamp"
New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
if (-not $ReceiptPath) { $ReceiptPath = Join-Path $BackupRoot 'receipt.json' }
$ReceiptPath = [IO.Path]::GetFullPath($ReceiptPath)
$operations = [Collections.Generic.List[object]]::new()
$gitHook = [ordered]@{ repository = $false; previous_set = $false; previous_value = $null; applied = $false }
$pluginResults = [Collections.Generic.List[object]]::new()

function Install-ExactPath {
  param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$Destination, [Parameter(Mandatory)][string]$Label)
  $Source = [IO.Path]::GetFullPath($Source)
  $Destination = [IO.Path]::GetFullPath($Destination)
  if (-not (Test-Path -LiteralPath $Source)) { throw "Install source is missing: $Source" }

  $existed = Test-Path -LiteralPath $Destination
  $backup = Join-Path $BackupRoot $Label
  if ($existed) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
    Copy-Item -LiteralPath $Destination -Destination $backup -Recurse -Force
  }
  $operations.Add([ordered]@{ destination = $Destination; existed = $existed; backup = $(if ($existed) { $backup } else { $null }) })

  if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
  New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
  Copy-Item -LiteralPath $Source -Destination $Destination -Recurse -Force
}

function Restore-InstallState {
  $restoreErrors = [Collections.Generic.List[string]]::new()

  if ([bool]$gitHook.applied -and [bool]$gitHook.repository -and (Get-Command git -ErrorAction SilentlyContinue)) {
    try {
      if ([bool]$gitHook.previous_set) {
        & git -C $ProjectRoot config --local core.hooksPath ([string]$gitHook.previous_value)
        if ($LASTEXITCODE -ne 0) { throw "git config restore exited $LASTEXITCODE" }
      } else {
        & git -C $ProjectRoot config --local --unset core.hooksPath 2>$null
        if ($LASTEXITCODE -notin @(0, 5)) { throw "git config unset exited $LASTEXITCODE" }
      }
    } catch {
      $restoreErrors.Add("Git hooksPath restore failed: $($_.Exception.Message)")
    }
  }

  $ops = @($operations)
  [array]::Reverse($ops)
  foreach ($op in $ops) {
    try {
      $destination = [string]$op.destination
      if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force }
      if ([bool]$op.existed) {
        $backup = [string]$op.backup
        if (-not (Test-Path -LiteralPath $backup)) { throw "Backup is missing: $backup" }
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $backup -Destination $destination -Recurse -Force
      }
    } catch {
      $restoreErrors.Add("Restore failed for $([string]$op.destination): $($_.Exception.Message)")
    }
  }

  if ($restoreErrors.Count -gt 0) {
    throw ($restoreErrors -join '; ')
  }
}

try {
  Install-ExactPath (Join-Path $Payload 'codex-home\config.portable.toml') (Join-Path $CodexHome 'config.toml') 'codex-home\config.toml'
  Install-ExactPath (Join-Path $Payload 'codex-home\AGENTS.md') (Join-Path $CodexHome 'AGENTS.md') 'codex-home\AGENTS.md'
  Install-ExactPath (Join-Path $Payload 'codex-home\instructions') (Join-Path $CodexHome 'instructions') 'codex-home\instructions'
  Install-ExactPath (Join-Path $Payload 'codex-home\rules\default.rules.template') (Join-Path $CodexHome 'rules\default.rules') 'codex-home\rules\default.rules'

  $rulePath = Join-Path $CodexHome 'rules\default.rules'
  $escapedHome = ([IO.Path]::GetFullPath($HOME)).Replace('\', '\\')
  $escapedProjectRoot = $ProjectRoot.Replace('\', '\\')
  $computerName = $(if ($env:COMPUTERNAME) { $env:COMPUTERNAME } else { [Environment]::MachineName })
  $windowsUserName = $(if ($env:USERNAME) { $env:USERNAME } else { [Environment]::UserName })
  $ruleText = (Get-Content -LiteralPath $rulePath -Raw -Encoding UTF8).Replace('__USER_HOME_ESCAPED__', $escapedHome).Replace('__PROJECT_ROOT_ESCAPED__', $escapedProjectRoot).Replace('__COMPUTER_NAME__', $computerName).Replace('__WINDOWS_USERNAME__', $windowsUserName)
  [IO.File]::WriteAllText($rulePath, $ruleText, [Text.UTF8Encoding]::new($false))

  $codexSkills = Join-Path $Payload 'codex-home\skills'
  if (Test-Path -LiteralPath $codexSkills) {
    foreach ($skill in Get-ChildItem -LiteralPath $codexSkills -Directory | Sort-Object Name) {
      Install-ExactPath $skill.FullName (Join-Path $CodexHome "skills\$($skill.Name)") "codex-home\skills\$($skill.Name)"
    }
  }
  $agentSkills = Join-Path $Payload 'agents-home\skills'
  if (Test-Path -LiteralPath $agentSkills) {
    foreach ($skill in Get-ChildItem -LiteralPath $agentSkills -Directory | Sort-Object Name) {
      Install-ExactPath $skill.FullName (Join-Path $AgentsHome "skills\$($skill.Name)") "agents-home\skills\$($skill.Name)"
    }
  }

  foreach ($item in @('.agents', '.codex', 'AGENTS.md', '.gitignore')) {
    $source = Join-Path (Join-Path $Payload 'project') $item
    if (Test-Path -LiteralPath $source) {
      Install-ExactPath $source (Join-Path $ProjectRoot $item) "project\$item"
    }
  }
  $workflowSource = Join-Path $Payload 'project\.github\workflows\context-control.yml'
  if (Test-Path -LiteralPath $workflowSource) {
    Install-ExactPath $workflowSource (Join-Path $ProjectRoot '.github\workflows\context-control.yml') 'project\.github\workflows\context-control.yml'
  }

  $textExtensions = @('.md', '.txt', '.json', '.toml', '.yaml', '.yml', '.ps1', '.py', '.js', '.mjs', '.cmd', '.sh', '')
  foreach ($root in @((Join-Path $ProjectRoot '.agents'), (Join-Path $ProjectRoot '.codex'), (Join-Path $ProjectRoot 'AGENTS.md'))) {
    $files = @()
    if (Test-Path -LiteralPath $root -PathType Leaf) { $files = @(Get-Item -LiteralPath $root) }
    elseif (Test-Path -LiteralPath $root -PathType Container) { $files = @(Get-ChildItem -LiteralPath $root -Recurse -File -Force) }
    foreach ($file in $files) {
      if ($textExtensions -notcontains $file.Extension.ToLowerInvariant()) { continue }
      try { $body = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 } catch { continue }
      $updated = $body.Replace('__PROJECT_ROOT_WIN__', $ProjectRoot).Replace('__PROJECT_ROOT_POSIX__', $ProjectRoot.Replace('\', '/'))
      if ($updated -ne $body) { [IO.File]::WriteAllText($file.FullName, $updated, [Text.UTF8Encoding]::new($false)) }
    }
  }

  $configPath = Join-Path $CodexHome 'config.toml'
  $tomlProjectKey = $ProjectRoot.Replace('\', '\\').Replace('"', '\"')
  $trustBlock = "`n[projects.`"$tomlProjectKey`"]`ntrust_level = `"trusted`"`n"
  [IO.File]::AppendAllText($configPath, $trustBlock, [Text.UTF8Encoding]::new($false))

  if (Get-Command git -ErrorAction SilentlyContinue) {
    & git -C $ProjectRoot rev-parse --git-dir *> $null
    if ($LASTEXITCODE -eq 0) {
      $gitHook.repository = $true
      $previous = & git -C $ProjectRoot config --local --get core.hooksPath 2>$null
      if ($LASTEXITCODE -eq 0) { $gitHook.previous_set = $true; $gitHook.previous_value = [string]$previous }
      & git -C $ProjectRoot config --local core.hooksPath .agents/git-hooks
      if ($LASTEXITCODE -ne 0) { throw "Failed to set Git hooksPath, exit $LASTEXITCODE" }
      $gitHook.applied = $true
    }
  }

  if (-not $SkipPlugins -and (Get-Command codex -ErrorAction SilentlyContinue)) {
    $desiredPlugins = @('browser@openai-bundled', 'visualize@openai-bundled', 'sites@openai-bundled')
    $inventory = Invoke-CodexNativeCapture -Arguments @('plugin', 'list', '--available', '--json')
    $inventoryStatus = 'failed'
    $pluginStates = @{}

    if ($inventory.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($inventory.Output)) {
      try {
        $parsedInventory = $inventory.Output | ConvertFrom-Json
        foreach ($entry in @($parsedInventory.installed)) {
          if ($entry.pluginId) { $pluginStates[[string]$entry.pluginId] = 'installed' }
        }
        foreach ($entry in @($parsedInventory.available)) {
          if ($entry.pluginId -and -not $pluginStates.ContainsKey([string]$entry.pluginId)) {
            $pluginStates[[string]$entry.pluginId] = 'available'
          }
        }
        $inventoryStatus = 'ok'
      } catch {
        $inventoryStatus = 'invalid-json'
        Write-Warning "Could not parse 'codex plugin list --available --json'. Plugin installs will be attempted best-effort. $($_.Exception.Message)"
      }
    } else {
      Write-Warning "Could not query available Codex plugins (exit $($inventory.ExitCode)). Plugin installs will be attempted best-effort. $($inventory.Output)"
    }

    foreach ($selector in $desiredPlugins) {
      if ($inventoryStatus -eq 'ok') {
        if (-not $pluginStates.ContainsKey($selector)) {
          $pluginResults.Add([ordered]@{ selector = $selector; exit = $null; status = 'skipped-not-available'; result = 'Plugin is not exposed by this Codex installation/marketplace.' })
          Write-Warning "Optional plugin '$selector' is not available on this Codex installation; skipping without failing the main install."
          continue
        }
        if ($pluginStates[$selector] -eq 'installed') {
          $pluginResults.Add([ordered]@{ selector = $selector; exit = 0; status = 'already-installed'; result = 'Plugin is already installed.' })
          continue
        }
      }

      $add = Invoke-CodexNativeCapture -Arguments @('plugin', 'add', $selector, '--json')
      $status = $(if ($add.ExitCode -eq 0) { 'installed' } else { 'failed-optional' })
      $resultText = ($add.Output -replace '[\r\n]+', ' ').Trim()
      $pluginResults.Add([ordered]@{ selector = $selector; exit = $add.ExitCode; status = $status; result = $resultText })
      if ($add.ExitCode -ne 0) {
        Write-Warning "Optional plugin '$selector' could not be installed (exit $($add.ExitCode)); continuing. $resultText"
      }
    }
  }

  $receipt = [ordered]@{
    schema = 2
    installed_at = (Get-Date).ToString('o')
    package = $PSScriptRoot
    package_manifest_files = $manifestCount
    project_root = $ProjectRoot
    codex_home = $CodexHome
    agents_home = $AgentsHome
    codex_version_requested = $CodexVersion
    codex_version_detected = $detectedCodexVersion
    backup_root = $BackupRoot
    operations = @($operations)
    git_hook = $gitHook
    plugins = @($pluginResults)
  }
  New-Item -ItemType Directory -Path (Split-Path -Parent $ReceiptPath) -Force | Out-Null
  $receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
  $pointer = Join-Path $CodexHome 'migration-backups\codex-cli-portable-setup-kit\last-receipt.txt'
  New-Item -ItemType Directory -Path (Split-Path -Parent $pointer) -Force | Out-Null
  [IO.File]::WriteAllText($pointer, $ReceiptPath, [Text.UTF8Encoding]::new($false))
} catch {
  $originalError = $_.Exception.Message
  try {
    Restore-InstallState
    throw "Install failed and all recorded file/Git changes were rolled back. Original error: $originalError"
  } catch {
    if ($_.Exception.Message -like 'Install failed and all recorded*') { throw }
    throw "Install failed. Automatic rollback also encountered an error. Original error: $originalError | Rollback error: $($_.Exception.Message) | Backup root: $BackupRoot"
  }
}

Write-Output 'INSTALL_RESULT=PASS'
Write-Output "PACKAGE_VERIFY=PASS files=$manifestCount"
Write-Output "PROJECT_ROOT=$ProjectRoot"
Write-Output "CODEX_HOME=$CodexHome"
Write-Output "AGENTS_HOME=$AgentsHome"
Write-Output "RECEIPT=$ReceiptPath"
Write-Output "PLUGIN_ATTEMPTS=$($pluginResults.Count)"
exit 0

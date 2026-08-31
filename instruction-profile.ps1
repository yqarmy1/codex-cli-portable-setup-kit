[CmdletBinding(DefaultParameterSetName = 'Deploy')]
param(
  [Parameter(ParameterSetName = 'Status', Mandatory)][switch]$Status,
  [Parameter(ParameterSetName = 'Uninstall', Mandatory)][switch]$Uninstall,
  [Parameter(ParameterSetName = 'Recover', Mandatory)][switch]$Recover,
  [string]$CodexHome = '',
  [string]$SourceFile = '',
  [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*\.md$')][string]$Name = 'portable-agent-instructions.md',
  [switch]$Yes
)

<#
.SYNOPSIS
Preview-first, manifest-owned deployment of one Codex instruction Markdown file.

.DESCRIPTION
This command is deliberately limited to a single top-level
model_instructions_file reference. It never changes hooks, approval policy,
sandbox settings, credentials, or platform permissions.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:SchemaVersion = 1
$script:ManifestName = '.codex-portable-setup-kit-instruction-manifest.json'
$script:JournalName = '.codex-portable-setup-kit-instruction-transaction.json'
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Get-DefaultCodexHome {
  if (-not [string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
    return [IO.Path]::GetFullPath($env:CODEX_HOME)
  }
  return (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex')
}

function Resolve-AbsolutePath {
  param([Parameter(Mandatory)][string]$Path)
  return [IO.Path]::GetFullPath($Path)
}

function Assert-RegularFile {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Label)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "$Label is missing or is not a regular file: $Path" }
  $item = Get-Item -LiteralPath $Path -Force
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label must not be a reparse point: $Path" }
}

function Assert-Directory {
  param([Parameter(Mandatory)][string]$Path, [switch]$Create)
  if (-not (Test-Path -LiteralPath $Path)) {
    if (-not $Create) { throw "Directory is missing: $Path" }
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
  }
  $item = Get-Item -LiteralPath $Path -Force
  if (-not $item.PSIsContainer) { throw "Expected a directory: $Path" }
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Directory must not be a reparse point: $Path" }
}

function Get-FileSha256 {
  param([Parameter(Mandatory)][string]$Path)
  Assert-RegularFile -Path $Path -Label 'File'
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-BytesSha256 {
  param([Parameter(Mandatory)][byte[]]$Bytes)
  $sha = [Security.Cryptography.SHA256]::Create()
  try { return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
  finally { $sha.Dispose() }
}

function Read-Utf8Text {
  param([Parameter(Mandatory)][string]$Path)
  Assert-RegularFile -Path $Path -Label 'Text file'
  return [IO.File]::ReadAllText($Path)
}

function ConvertTo-Utf8Bytes {
  param([Parameter(Mandatory)][string]$Text)
  return $script:Utf8NoBom.GetBytes($Text)
}

function Write-AtomicBytes {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][byte[]]$Bytes)
  $parent = Split-Path -Parent $Path
  Assert-Directory -Path $parent -Create
  $temporary = Join-Path $parent ('.{0}.{1}.tmp' -f ([IO.Path]::GetFileName($Path)), [Guid]::NewGuid().ToString('N').Substring(0, 8))
  $replaceBackup = "$temporary.previous"
  try {
    try { [IO.File]::WriteAllBytes($temporary, $Bytes) }
    catch { throw "Atomic staging write failed for ${Path}: $($_.Exception.Message)" }
    if (Test-Path -LiteralPath $Path) {
      Assert-RegularFile -Path $Path -Label 'Replacement target'
      [IO.File]::Replace($temporary, $Path, $replaceBackup)
      if (Test-Path -LiteralPath $replaceBackup -PathType Leaf) { Remove-Item -LiteralPath $replaceBackup -Force }
    } else {
      [IO.File]::Move($temporary, $Path)
    }
  } finally {
    if (Test-Path -LiteralPath $temporary -PathType Leaf) { Remove-Item -LiteralPath $temporary -Force }
    if (Test-Path -LiteralPath $replaceBackup -PathType Leaf) { Remove-Item -LiteralPath $replaceBackup -Force }
  }
}

function Write-AtomicText {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Text)
  Write-AtomicBytes -Path $Path -Bytes (ConvertTo-Utf8Bytes -Text $Text)
}

function Copy-AtomicFile {
  param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$Destination)
  Assert-RegularFile -Path $Source -Label 'Backup source'
  Write-AtomicBytes -Path $Destination -Bytes ([IO.File]::ReadAllBytes($Source))
}

function Get-JsonObject {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Label)
  Assert-RegularFile -Path $Path -Label $Label
  try { return (Read-Utf8Text -Path $Path | ConvertFrom-Json) }
  catch { throw "$Label is not valid JSON: $Path" }
}

function ConvertTo-JsonText {
  param([Parameter(Mandatory)]$Object)
  return (($Object | ConvertTo-Json -Depth 8) + "`n")
}

function Get-TopLevelInstructionReference {
  param([Parameter(Mandatory)][string]$Content)
  $lines = @($Content -split '(?<=\n)')
  if ($lines.Count -eq 0) { $lines = @('') }
  $inTable = $false
  $foundReferences = @()
  $firstTable = $null
  for ($index = 0; $index -lt $lines.Count; $index++) {
    $body = $lines[$index].TrimEnd("`r", "`n")
    if ($body -match '^\s*\[\[?.+\]\]?\s*(?:#.*)?$') {
      if ($null -eq $firstTable) { $firstTable = $index }
      $inTable = $true
      continue
    }
    if (-not $inTable -and $body -match '^\s*model_instructions_file\s*=\s*(?<quote>["''])(?<value>[^"'']*)\k<quote>\s*(?:#.*)?$') {
      $foundReferences += [pscustomobject]@{ Index = $index; Value = $Matches.value }
    } elseif (-not $inTable -and $body -match '^\s*model_instructions_file\s*=') {
      throw 'Top-level model_instructions_file must be one simple quoted TOML string.'
    }
  }
  if ($foundReferences.Count -gt 1) { throw 'config.toml contains multiple top-level model_instructions_file entries.' }
  return [pscustomobject]@{
    Lines = $lines
    Match = $(if ($foundReferences.Count) { $foundReferences[0] } else { $null })
    FirstTable = $firstTable
    Newline = $(if ($Content.Contains("`r`n")) { "`r`n" } else { "`n" })
  }
}

function Set-TopLevelInstructionReference {
  param([Parameter(Mandatory)][string]$Content, [Parameter(Mandatory)][string]$Value)
  $state = Get-TopLevelInstructionReference -Content $Content
  $line = 'model_instructions_file = "{0}"' -f $Value
  if ($null -ne $state.Match) {
    $suffix = if ($state.Lines[$state.Match.Index].EndsWith("`r`n")) { "`r`n" } elseif ($state.Lines[$state.Match.Index].EndsWith("`n")) { "`n" } else { '' }
    $state.Lines[$state.Match.Index] = $line + $suffix
    return ($state.Lines -join '')
  }
  $insert = $line + $state.Newline
  if ($null -eq $state.FirstTable) { return ($Content + $(if ($Content.Length -gt 0 -and -not $Content.EndsWith("`n")) { $state.Newline } else { '' }) + $insert) }
  if ($state.FirstTable -eq 0) { return ($insert + ($state.Lines -join '')) }
  return (($state.Lines[0..($state.FirstTable - 1)] -join '') + $insert + ($state.Lines[$state.FirstTable..($state.Lines.Count - 1)] -join ''))
}

function Restore-TopLevelInstructionReference {
  param([Parameter(Mandatory)][string]$Content, [AllowNull()][string]$PreviousValue)
  if ($null -ne $PreviousValue) { return (Set-TopLevelInstructionReference -Content $Content -Value $PreviousValue) }
  $state = Get-TopLevelInstructionReference -Content $Content
  if ($null -eq $state.Match) { return $Content }
  $state.Lines[$state.Match.Index] = ''
  return ($state.Lines -join '')
}

function Get-RelativeInstructionName {
  param([Parameter(Mandatory)][string]$FileName)
  if ($FileName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*\.md$') { throw 'Managed instruction filename is invalid.' }
  return ('instructions/{0}' -f $FileName)
}

function New-BackupDirectory {
  param([Parameter(Mandatory)][string]$CodexRoot)
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  $path = Join-Path $CodexRoot ('migration-backups\codex-cli-portable-setup-kit\ip\{0}-{1}' -f $stamp, [Guid]::NewGuid().ToString('N').Substring(0, 8))
  Assert-Directory -Path $path -Create
  return $path
}

function New-JournalEntry {
  param(
    [Parameter(Mandatory)][string]$Name,
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][string]$BackupRoot,
    [AllowNull()][byte[]]$AfterBytes
  )
  $beforeExists = Test-Path -LiteralPath $Path -PathType Leaf
  if ((Test-Path -LiteralPath $Path) -and -not $beforeExists) { throw "Managed path is not a regular file: $Path" }
  $backup = $null
  $beforeHash = $null
  if ($beforeExists) {
    Assert-RegularFile -Path $Path -Label "Current $Name"
    $backup = "$Name.before"
    Copy-AtomicFile -Source $Path -Destination (Join-Path $BackupRoot $backup)
    $beforeHash = Get-FileSha256 -Path $Path
  }
  return [ordered]@{
    name = $Name
    before_exists = [bool]$beforeExists
    before_sha256 = $beforeHash
    backup = $backup
    after_exists = [bool]($null -ne $AfterBytes)
    after_sha256 = $(if ($null -ne $AfterBytes) { Get-BytesSha256 -Bytes $AfterBytes } else { $null })
  }
}

function Get-ManagedPaths {
  param([Parameter(Mandatory)][string]$CodexRoot, [Parameter(Mandatory)][string]$RelativeInstruction)
  if ($RelativeInstruction -notmatch '^instructions/[A-Za-z0-9][A-Za-z0-9._-]*\.md$') { throw 'Instruction manifest path is invalid.' }
  return [ordered]@{
    config = (Join-Path $CodexRoot 'config.toml')
    instruction = (Join-Path $CodexRoot ($RelativeInstruction -replace '/', '\'))
    manifest = (Join-Path $CodexRoot $script:ManifestName)
    journal = (Join-Path $CodexRoot $script:JournalName)
  }
}

function Get-EntryPath {
  param([Parameter(Mandatory)]$Paths, [Parameter(Mandatory)][string]$Name)
  if ($Name -notin @('config', 'instruction', 'manifest')) { throw 'Journal has an invalid entry name.' }
  return [string]$Paths[$Name]
}

function Test-EntryPhase {
  param([Parameter(Mandatory)]$Entry, [Parameter(Mandatory)][string]$Path)
  $exists = Test-Path -LiteralPath $Path -PathType Leaf
  if ((Test-Path -LiteralPath $Path) -and -not $exists) { return 'drift' }
  if (-not $exists) {
    if (-not [bool]$Entry.before_exists) { return 'before' }
    if (-not [bool]$Entry.after_exists) { return 'after' }
    return 'drift'
  }
  $actual = Get-FileSha256 -Path $Path
  if ([bool]$Entry.before_exists -and $actual -eq [string]$Entry.before_sha256) { return 'before' }
  if ([bool]$Entry.after_exists -and $actual -eq [string]$Entry.after_sha256) { return 'after' }
  return 'drift'
}

function Assert-Manifest {
  param([Parameter(Mandatory)]$Manifest)
  foreach ($property in @('schema', 'instruction_relative', 'desired_reference', 'installed_sha256', 'previous_model_instructions_file', 'backup_root')) {
    if ($null -eq $Manifest.PSObject.Properties[$property]) { throw "Instruction manifest is missing $property." }
  }
  if ([int]$Manifest.schema -ne $script:SchemaVersion) { throw 'Instruction manifest schema is unsupported.' }
  if ([string]$Manifest.instruction_relative -notmatch '^instructions/[A-Za-z0-9][A-Za-z0-9._-]*\.md$') { throw 'Instruction manifest has an unsafe path.' }
  if ([string]$Manifest.desired_reference -ne ('./' + [string]$Manifest.instruction_relative)) { throw 'Instruction manifest reference is invalid.' }
  if ([string]$Manifest.installed_sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Instruction manifest hash is invalid.' }
  Assert-Directory -Path ([string]$Manifest.backup_root)
}

function Invoke-Status {
  param([Parameter(Mandatory)][string]$CodexRoot)
  $fallbackRelative = Get-RelativeInstructionName -FileName $Name
  $paths = Get-ManagedPaths -CodexRoot $CodexRoot -RelativeInstruction $fallbackRelative
  if (Test-Path -LiteralPath $paths.journal) {
    Assert-RegularFile -Path $paths.journal -Label 'Instruction transaction journal'
    Write-Output "INSTRUCTION_PROFILE_STATUS=recovery-required journal=$($paths.journal)"
    exit 2
  }
  if (-not (Test-Path -LiteralPath $paths.manifest)) {
    Write-Output "INSTRUCTION_PROFILE_STATUS=unmanaged codex_home=$CodexRoot"
    return
  }
  $manifest = Get-JsonObject -Path $paths.manifest -Label 'Instruction manifest'
  Assert-Manifest -Manifest $manifest
  $paths = Get-ManagedPaths -CodexRoot $CodexRoot -RelativeInstruction ([string]$manifest.instruction_relative)
  try {
    Assert-RegularFile -Path $paths.config -Label 'config.toml'
    Assert-RegularFile -Path $paths.instruction -Label 'Managed instruction'
    $reference = (Get-TopLevelInstructionReference -Content (Read-Utf8Text -Path $paths.config)).Match
    if ($null -eq $reference -or $reference.Value -ne [string]$manifest.desired_reference) { throw 'model_instructions_file has drifted.' }
    if ((Get-FileSha256 -Path $paths.instruction) -ne [string]$manifest.installed_sha256) { throw 'Managed instruction content has drifted.' }
    Write-Output "INSTRUCTION_PROFILE_STATUS=active instruction=$($paths.instruction)"
  } catch {
    Write-Output "INSTRUCTION_PROFILE_STATUS=conflict detail=$($_.Exception.Message)"
    exit 1
  }
}

function Invoke-Recover {
  param([Parameter(Mandatory)][string]$CodexRoot)
  $journalPath = Join-Path $CodexRoot $script:JournalName
  if (-not (Test-Path -LiteralPath $journalPath)) { Write-Output 'INSTRUCTION_PROFILE_RECOVER=no-transaction'; return }
  $journal = Get-JsonObject -Path $journalPath -Label 'Instruction transaction journal'
  if ([int]$journal.schema -ne $script:SchemaVersion -or [string]$journal.operation -notin @('deploy', 'uninstall')) { throw 'Instruction transaction journal is unsupported.' }
  if ([string]$journal.codex_home -ne $CodexRoot) { throw 'Instruction transaction journal belongs to a different Codex home.' }
  if ([string]$journal.instruction_relative -notmatch '^instructions/[A-Za-z0-9][A-Za-z0-9._-]*\.md$') { throw 'Instruction transaction journal has an unsafe path.' }
  Assert-Directory -Path ([string]$journal.backup_root)
  $paths = Get-ManagedPaths -CodexRoot $CodexRoot -RelativeInstruction ([string]$journal.instruction_relative)
  $entries = @($journal.entries)
  if ($entries.Count -ne 3) { throw 'Instruction transaction journal has an invalid entry set.' }
  foreach ($entry in $entries) {
    $path = Get-EntryPath -Paths $paths -Name ([string]$entry.name)
    $phase = Test-EntryPhase -Entry $entry -Path $path
    if ($phase -eq 'drift') { throw "Recovery refused because $($entry.name) changed outside the recorded transaction." }
    Write-Output "INSTRUCTION_PROFILE_RECOVER_PLAN name=$($entry.name) state=$phase"
  }
  if (-not $Yes) { Write-Output 'INSTRUCTION_PROFILE_RECOVER=preview add=-Yes'; return }
  foreach ($entry in $entries) {
    $path = Get-EntryPath -Paths $paths -Name ([string]$entry.name)
    if ([bool]$entry.before_exists) {
      $backup = Join-Path ([string]$journal.backup_root) ([string]$entry.backup)
      Copy-AtomicFile -Source $backup -Destination $path
    } elseif (Test-Path -LiteralPath $path) {
      Assert-RegularFile -Path $path -Label "Recovery target $($entry.name)"
      Remove-Item -LiteralPath $path -Force
    }
  }
  Remove-Item -LiteralPath $journalPath -Force
  Write-Output 'INSTRUCTION_PROFILE_RECOVER=restored'
}

function Invoke-Deploy {
  param([Parameter(Mandatory)][string]$CodexRoot)
  $relative = Get-RelativeInstructionName -FileName $Name
  $paths = Get-ManagedPaths -CodexRoot $CodexRoot -RelativeInstruction $relative
  $source = if ([string]::IsNullOrWhiteSpace($SourceFile)) { Join-Path $PSScriptRoot 'payload\codex-home\instructions\portable-agent-instructions.md' } else { Resolve-AbsolutePath -Path $SourceFile }
  Assert-RegularFile -Path $source -Label 'Instruction source'
  Assert-Directory -Path $CodexRoot
  Assert-RegularFile -Path $paths.config -Label 'config.toml'
  Assert-Directory -Path (Split-Path -Parent $paths.instruction) -Create
  if (Test-Path -LiteralPath $paths.journal) { throw "An interrupted transaction exists; run -Recover first: $($paths.journal)" }
  if (Test-Path -LiteralPath $paths.manifest) { throw "A managed instruction profile already exists; use -Status or -Uninstall first: $($paths.manifest)" }
  if ((Test-Path -LiteralPath $paths.instruction) -and -not (Test-Path -LiteralPath $paths.instruction -PathType Leaf)) { throw "Instruction destination is not a regular file: $($paths.instruction)" }
  if (Test-Path -LiteralPath $paths.instruction) { Assert-RegularFile -Path $paths.instruction -Label 'Existing instruction' }
  $oldConfig = Read-Utf8Text -Path $paths.config
  $current = (Get-TopLevelInstructionReference -Content $oldConfig).Match
  $desired = './' + $relative
  if ($null -ne $current -and $current.Value -eq $desired) { throw 'config.toml already references this instruction without a managed manifest; refusing to take ownership.' }
  $newConfig = Set-TopLevelInstructionReference -Content $oldConfig -Value $desired
  $sourceBytes = [IO.File]::ReadAllBytes($source)
  Write-Output "INSTRUCTION_PROFILE_DEPLOY_PLAN source=$source destination=$($paths.instruction) reference=$desired"
  if (-not $Yes) { Write-Output 'INSTRUCTION_PROFILE_DEPLOY=preview add=-Yes'; return }

  $backupRoot = New-BackupDirectory -CodexRoot $CodexRoot
  $manifest = [ordered]@{
    schema = $script:SchemaVersion
    created_at = [DateTime]::UtcNow.ToString('o')
    instruction_relative = $relative
    desired_reference = $desired
    installed_sha256 = Get-BytesSha256 -Bytes $sourceBytes
    previous_model_instructions_file = $(if ($null -eq $current) { $null } else { $current.Value })
    backup_root = $backupRoot
  }
  $manifestBytes = ConvertTo-Utf8Bytes -Text (ConvertTo-JsonText -Object $manifest)
  $entries = @(
    (New-JournalEntry -Name 'config' -Path $paths.config -BackupRoot $backupRoot -AfterBytes (ConvertTo-Utf8Bytes -Text $newConfig)),
    (New-JournalEntry -Name 'instruction' -Path $paths.instruction -BackupRoot $backupRoot -AfterBytes $sourceBytes),
    (New-JournalEntry -Name 'manifest' -Path $paths.manifest -BackupRoot $backupRoot -AfterBytes $manifestBytes)
  )
  $journal = [ordered]@{ schema = $script:SchemaVersion; operation = 'deploy'; codex_home = $CodexRoot; instruction_relative = $relative; backup_root = $backupRoot; entries = $entries }
  Write-AtomicText -Path $paths.journal -Text (ConvertTo-JsonText -Object $journal)
  try {
    Write-AtomicBytes -Path $paths.instruction -Bytes $sourceBytes
    Write-AtomicText -Path $paths.config -Text $newConfig
    Write-AtomicBytes -Path $paths.manifest -Bytes $manifestBytes
    Remove-Item -LiteralPath $paths.journal -Force
  } catch {
    [Console]::Error.WriteLine("Instruction deployment stopped; journal retained for -Recover: $($paths.journal)")
    throw
  }
  Write-Output "INSTRUCTION_PROFILE_DEPLOY=applied manifest=$($paths.manifest)"
}

function Invoke-Uninstall {
  param([Parameter(Mandatory)][string]$CodexRoot)
  $initialManifestPath = Join-Path $CodexRoot $script:ManifestName
  if (-not (Test-Path -LiteralPath $initialManifestPath)) { Write-Output 'INSTRUCTION_PROFILE_UNINSTALL=not-managed'; return }
  $manifest = Get-JsonObject -Path $initialManifestPath -Label 'Instruction manifest'
  Assert-Manifest -Manifest $manifest
  $relative = [string]$manifest.instruction_relative
  $paths = Get-ManagedPaths -CodexRoot $CodexRoot -RelativeInstruction $relative
  if (Test-Path -LiteralPath $paths.journal) { throw "An interrupted transaction exists; run -Recover first: $($paths.journal)" }
  Assert-RegularFile -Path $paths.config -Label 'config.toml'
  Assert-RegularFile -Path $paths.instruction -Label 'Managed instruction'
  if ((Get-FileSha256 -Path $paths.instruction) -ne [string]$manifest.installed_sha256) { throw 'Managed instruction content has drifted; uninstall refused.' }
  $oldConfig = Read-Utf8Text -Path $paths.config
  $current = (Get-TopLevelInstructionReference -Content $oldConfig).Match
  if ($null -eq $current -or $current.Value -ne [string]$manifest.desired_reference) { throw 'model_instructions_file has drifted; uninstall refused.' }
  $newConfig = Restore-TopLevelInstructionReference -Content $oldConfig -PreviousValue $manifest.previous_model_instructions_file
  $restoreBytes = $null
  $previousInstruction = Join-Path ([string]$manifest.backup_root) 'instruction.before'
  if (Test-Path -LiteralPath $previousInstruction) { Assert-RegularFile -Path $previousInstruction -Label 'Original instruction backup'; $restoreBytes = [IO.File]::ReadAllBytes($previousInstruction) }
  Write-Output "INSTRUCTION_PROFILE_UNINSTALL_PLAN instruction=$($paths.instruction) restore_reference=$($manifest.previous_model_instructions_file)"
  if (-not $Yes) { Write-Output 'INSTRUCTION_PROFILE_UNINSTALL=preview add=-Yes'; return }

  $backupRoot = New-BackupDirectory -CodexRoot $CodexRoot
  $entries = @(
    (New-JournalEntry -Name 'config' -Path $paths.config -BackupRoot $backupRoot -AfterBytes (ConvertTo-Utf8Bytes -Text $newConfig)),
    (New-JournalEntry -Name 'instruction' -Path $paths.instruction -BackupRoot $backupRoot -AfterBytes $restoreBytes),
    (New-JournalEntry -Name 'manifest' -Path $paths.manifest -BackupRoot $backupRoot -AfterBytes $null)
  )
  $journal = [ordered]@{ schema = $script:SchemaVersion; operation = 'uninstall'; codex_home = $CodexRoot; instruction_relative = $relative; backup_root = $backupRoot; entries = $entries }
  Write-AtomicText -Path $paths.journal -Text (ConvertTo-JsonText -Object $journal)
  try {
    if ($null -ne $restoreBytes) { Write-AtomicBytes -Path $paths.instruction -Bytes $restoreBytes }
    else { Remove-Item -LiteralPath $paths.instruction -Force }
    Write-AtomicText -Path $paths.config -Text $newConfig
    Remove-Item -LiteralPath $paths.manifest -Force
    Remove-Item -LiteralPath $paths.journal -Force
  } catch {
    [Console]::Error.WriteLine("Instruction uninstall stopped; journal retained for -Recover: $($paths.journal)")
    throw
  }
  Write-Output 'INSTRUCTION_PROFILE_UNINSTALL=applied'
}

$codexRoot = Resolve-AbsolutePath -Path $(if ([string]::IsNullOrWhiteSpace($CodexHome)) { Get-DefaultCodexHome } else { $CodexHome })
if ($Status) { Invoke-Status -CodexRoot $codexRoot; exit 0 }
if ($Recover) { Invoke-Recover -CodexRoot $codexRoot; exit 0 }
if ($Uninstall) { Invoke-Uninstall -CodexRoot $codexRoot; exit 0 }
Invoke-Deploy -CodexRoot $codexRoot

[CmdletBinding()]
param(
  [string]$RepoRoot = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
  $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
}
$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
$failures = [Collections.Generic.List[string]]::new()

function Assert-PublicRelease {
  param(
    [Parameter(Mandatory)][bool]$Condition,
    [Parameter(Mandatory)][string]$Message
  )
  if (-not $Condition) { $script:failures.Add($Message) }
}

$requiredFiles = @(
  'README.md',
  'LICENSE',
  'SECURITY.md',
  'CONTRIBUTING.md',
  'CHANGELOG.md',
  'PAYLOAD_INDEX.json',
  'docs/ARCHITECTURE.md',
  'docs/EXECUTION_PROFILE.md',
  'docs/PROMOTION_PLAYBOOK.md',
  '.github/workflows/verify.yml'
)
foreach ($relative in $requiredFiles) {
  Assert-PublicRelease -Condition (Test-Path -LiteralPath (Join-Path $RepoRoot $relative) -PathType Leaf) -Message "Required public-release file is missing: $relative"
}

$readmePath = Join-Path $RepoRoot 'README.md'
$readme = Get-Content -LiteralPath $readmePath -Raw -Encoding UTF8
foreach ($requiredText in @(
  '<h1 align="center">Codex CLI Portable Setup Kit</h1>',
  'Less talk. More execution.',
  'inspect, edit, test, fix, and finish',
  'actions/workflows/verify.yml/badge.svg',
  '## Highlights',
  '## What this profile changes',
  'Action over advice',
  'Do the work, then report the result.',
  '## Quick start',
  '## Why not copy `.codex` manually?',
  '## What gets installed',
  '## How it works',
  '## Rollback',
  '## Security model',
  '## Official Codex documentation',
  'community-maintained'
)) {
  Assert-PublicRelease -Condition $readme.Contains($requiredText) -Message "README is missing required text: $requiredText"
}
Assert-PublicRelease -Condition ($readme.IndexOf('## Quick start') -lt $readme.IndexOf('## How it works')) -Message 'README must put the working quick start before implementation detail.'

$promotionPath = Join-Path $RepoRoot 'docs/PROMOTION_PLAYBOOK.md'
$promotion = Get-Content -LiteralPath $promotionPath -Raw -Encoding UTF8
foreach ($requiredText in @(
  '## High-star project narrative',
  '### GitHub About description',
  'One outcome. Three proof points. One call to action.',
  'Less talk. More execution.',
  'A Codex profile for people tired of agents that explain the work instead of doing it.',
  'Show HN: A Codex profile that does the work before talking about it',
  '## Fifteen-minute launch checklist',
  '## 48-hour launch plan'
)) {
  Assert-PublicRelease -Condition $promotion.Contains($requiredText) -Message "Promotion playbook is missing required text: $requiredText"
}

$architecturePath = Join-Path $RepoRoot 'docs/ARCHITECTURE.md'
$architecture = Get-Content -LiteralPath $architecturePath -Raw -Encoding UTF8
Assert-PublicRelease -Condition $architecture.Contains('execution-first behavior profile') -Message 'Architecture must describe the execution-first behavior profile as the product.'
Assert-PublicRelease -Condition (-not $architecture.Contains('verifiable Windows migration package')) -Message 'Architecture still presents migration as the product.'

$attributesPath = Join-Path $RepoRoot '.gitattributes'
$attributes = Get-Content -LiteralPath $attributesPath -Raw -Encoding UTF8
Assert-PublicRelease -Condition $attributes.Contains('* -text') -Message 'Git attributes must preserve exact bytes so MANIFEST.sha256 survives archives and clones.'
Assert-PublicRelease -Condition (-not $attributes.Contains('eol=')) -Message 'Git attributes must not rewrite line endings covered by MANIFEST.sha256.'

$workflowPath = Join-Path $RepoRoot '.github/workflows/verify.yml'
$workflow = Get-Content -LiteralPath $workflowPath -Raw -Encoding UTF8
Assert-PublicRelease -Condition $workflow.Contains("python-version: '3.11'") -Message 'CI must use the Python 3.11 runtime required by the pinned orchestrator probe.'

$metadataScriptPath = Join-Path $RepoRoot 'scripts/Update-PackageMetadata.ps1'
$metadataScript = Get-Content -LiteralPath $metadataScriptPath -Raw -Encoding UTF8
Assert-PublicRelease -Condition $metadataScript.Contains("release = '6.2.0'") -Message 'Package metadata generator must target release 6.2.0.'
$changelog = Get-Content -LiteralPath (Join-Path $RepoRoot 'CHANGELOG.md') -Raw -Encoding UTF8
Assert-PublicRelease -Condition $changelog.Contains('## [6.2.0] - 2026-08-31') -Message 'Changelog is missing release 6.2.0.'

$configPath = Join-Path $RepoRoot 'payload/codex-home/config.portable.toml'
$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
Assert-PublicRelease -Condition $config.Contains('approval_policy = "on-request"') -Message 'Portable config must default to approval_policy="on-request".'
Assert-PublicRelease -Condition $config.Contains('sandbox_mode = "workspace-write"') -Message 'Portable config must default to sandbox_mode="workspace-write".'
Assert-PublicRelease -Condition $config.Contains('localeOverride = "en-US"') -Message 'Portable config must default to the English locale.'
Assert-PublicRelease -Condition $config.Contains('model_verbosity = "low"') -Message 'Execution-first profile must use low model verbosity.'

$instructionsPath = Join-Path $RepoRoot 'payload/codex-home/instructions/portable-agent-instructions.md'
Assert-PublicRelease -Condition (Test-Path -LiteralPath $instructionsPath -PathType Leaf) -Message 'English portable agent instructions are missing.'
Assert-PublicRelease -Condition (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'payload/codex-home/instructions/gpt-5.6-sol-unrestricted-v45.md'))) -Message 'Legacy unrestricted agent instructions must not ship in the public edition.'
$instructions = $(if (Test-Path -LiteralPath $instructionsPath -PathType Leaf) { Get-Content -LiteralPath $instructionsPath -Raw -Encoding UTF8 } else { '' })
Assert-PublicRelease -Condition (-not $instructions.Contains('Current: TARGET / RESULT / NEXT')) -Message 'Agent instructions still force the Current/Result/Next response prefix.'
foreach ($requiredText in @(
  'Treat actionable requests as work to perform, not advice to describe.',
  'Use tools to inspect, edit, run, and verify the real target.',
  'Do not stop at a plan, progress update, or promise',
  'Keep interim narration brief'
)) {
  Assert-PublicRelease -Condition $instructions.Contains($requiredText) -Message "Portable instructions are missing execution-first behavior: $requiredText"
}

$projectAgentsPath = Join-Path $RepoRoot 'payload/project/AGENTS.md'
$projectAgents = Get-Content -LiteralPath $projectAgentsPath -Raw -Encoding UTF8
Assert-PublicRelease -Condition $projectAgents.Contains('Do the work before describing the work.') -Message 'Project AGENTS.md is missing the execution-first rule.'

$rulesPath = Join-Path $RepoRoot 'payload/codex-home/rules/default.rules.template'
$rules = Get-Content -LiteralPath $rulesPath -Raw -Encoding UTF8
Assert-PublicRelease -Condition $rules.Contains('# Public starter rules') -Message 'Rules template is not the public starter profile.'
Assert-PublicRelease -Condition (-not $rules.Contains('decision="allow"')) -Message 'Public starter rules must not silently auto-allow commands.'
Assert-PublicRelease -Condition ($rules -notmatch '(?i)root@|private[_-]?key|vps[_-]?key') -Message 'Rules template contains environment-specific remote-access material.'
Assert-PublicRelease -Condition ($rules -notmatch '(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)') -Message 'Rules template contains an environment-specific IPv4 address.'

Assert-PublicRelease -Condition (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'SOURCE_HASHES.json'))) -Message 'SOURCE_HASHES.json exposes local source paths and must not ship in the public edition.'

$textExtensions = @('.md', '.txt', '.ps1', '.cmd', '.bat', '.sh', '.py', '.mjs', '.js', '.json', '.toml', '.yaml', '.yml', '.gitignore', '.template', '.rules', '.patch', '')
$textFiles = @(Get-ChildItem -LiteralPath $RepoRoot -Recurse -Force -File | Where-Object {
  $_.FullName -notmatch '[\\/]\.git[\\/]' -and
  $_.Name -ne '_chinese_scan.txt' -and
  $textExtensions -contains $_.Extension.ToLowerInvariant()
})
$hanFiles = [Collections.Generic.List[string]]::new()
$absoluteUserPathFiles = [Collections.Generic.List[string]]::new()
foreach ($file in $textFiles) {
  try { $body = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 } catch { continue }
  $relative = $file.FullName.Substring($RepoRoot.Length).TrimStart([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
  if ($body -match '[\u3400-\u9FFF]') { $hanFiles.Add($relative) }
  if ($body -match '(?i)C:\\Users\\[^\\\s"'']+') { $absoluteUserPathFiles.Add($relative) }
}
Assert-PublicRelease -Condition ($hanFiles.Count -eq 0) -Message ("Non-English Han text remains in: " + ($hanFiles -join ', '))
Assert-PublicRelease -Condition ($absoluteUserPathFiles.Count -eq 0) -Message ("Absolute Windows user paths remain in: " + ($absoluteUserPathFiles -join ', '))

if ($failures.Count -gt 0) {
  $failures | ForEach-Object { Write-Output "PUBLIC_RELEASE_FAILURE=$_" }
  Write-Output "PUBLIC_RELEASE_TEST=FAIL failures=$($failures.Count) scanned_files=$($textFiles.Count)"
  exit 1
}

Write-Output "PUBLIC_RELEASE_TEST=PASS failures=0 scanned_files=$($textFiles.Count) language=English defaults=public-safe response_prefix=normal"
exit 0

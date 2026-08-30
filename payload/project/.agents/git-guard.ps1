[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$maxBlobBytes = 100MB
$blocked = New-Object System.Collections.Generic.List[string]
$staged = @(& git diff --cached --no-renames --name-only --diff-filter=ACMR)
$repoRoot = (& git rev-parse --show-toplevel).Trim()

foreach ($relativePath in $staged) {
    if ([string]::IsNullOrWhiteSpace($relativePath)) {
        continue
    }
    $normalized = $relativePath.Replace('\', '/')
    $leaf = [IO.Path]::GetFileName($normalized)
    $isExample = $normalized -match '(?i)(^|/)\.env\.example$' -or
        $normalized -match '(?i)\.example$'
    $looksSensitive = -not $isExample -and (
        $normalized -match '(?i)(^|/)(private|secrets?|credentials?)(/|$)' -or
        $normalized -match '(?i)(^|/)\.env($|\.)' -or
        $normalized -match '(?i)\.(pem|key|pfx|p12|db|sqlite|sqlite3)$' -or
        $leaf -match '(?i)(account|cookie|credential|secret|sso|cpa|token).*\.(json|txt)$'
    )
    if ($looksSensitive) {
        $blocked.Add("sensitive path: $normalized")
        continue
    }
    $fullPath = Join-Path $repoRoot $relativePath
    if (Test-Path -LiteralPath $fullPath -PathType Leaf) {
        $size = (Get-Item -LiteralPath $fullPath -Force).Length
    }
    else {
        $size = 0
    }
    if ($size -gt $maxBlobBytes) {
        $blocked.Add("blob exceeds 100 MiB: $normalized ($size bytes)")
    }
}

$contentPatterns = @(
    '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'AKIA[0-9A-Z]{16}',
    'gh[pousr]_[A-Za-z0-9]{30,}',
    'sk-[A-Za-z0-9]{20,}'
)
foreach ($pattern in $contentPatterns) {
    $matches = @(& git grep --cached -I -l -E -- $pattern 2>$null)
    foreach ($match in $matches) {
        $blocked.Add("credential-like content: $match")
    }
}

if ($blocked.Count -gt 0) {
    Write-Error ("Commit blocked:`n - " + ($blocked -join "`n - "))
    exit 1
}

Write-Host "GIT_GUARD=PASS staged=$($staged.Count)"
exit 0

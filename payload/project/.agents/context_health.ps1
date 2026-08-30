[CmdletBinding()]
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$Strict
)

$scriptPath = Join-Path $PSScriptRoot "skills\context-guardian\scripts\contextctl.py"
& python $scriptPath --root $Root audit
$exitCode = $LASTEXITCODE

if ($Strict -and $exitCode -ne 0) {
    exit $exitCode
}


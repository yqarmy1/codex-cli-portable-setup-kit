param(
    [string]$ProjectRoot = (Get-Location).Path,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$workspaceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..\..')).Path
$toolRoot = Join-Path $workspaceRoot '.workspace\tools\codex-continuous'
$venvRoot = Join-Path $toolRoot '.venv'
$pythonExe = Join-Path $venvRoot 'Scripts\python.exe'
$clientScript = Join-Path $workspaceRoot '.agents\tools\codex-continuous\continuous_cli.py'
$requiredSdkVersion = '0.144.4'
$requiredPromptToolkitVersion = '3.0.53'

New-Item -ItemType Directory -Force -Path $toolRoot | Out-Null
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    python -m venv $venvRoot
}

$dependencyStatus = & $pythonExe -c "import importlib.metadata as m, re; normalize = lambda name: re.sub(r'[-_.]+', '-', name).casefold(); installed = {normalize(d.metadata.get('Name') or ''): d.version for d in m.distributions()}; print('ready' if installed.get('openai-codex') == '$requiredSdkVersion' and installed.get('prompt-toolkit') == '$requiredPromptToolkitVersion' else 'missing')"
if ($dependencyStatus -ne 'ready') {
    & $pythonExe -m pip install --disable-pip-version-check --quiet --upgrade "openai-codex==$requiredSdkVersion" "prompt_toolkit==$requiredPromptToolkitVersion"
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to prepare the legacy Codex Continuous runtime.'
    }
}

$resolvedProject = (Resolve-Path -LiteralPath $ProjectRoot).Path
& $pythonExe $clientScript --project $resolvedProject @CliArgs
exit $LASTEXITCODE

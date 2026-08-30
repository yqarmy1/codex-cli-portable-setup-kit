param(
    [string]$ProjectRoot = (Get-Location).Path,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$bootstrap = Join-Path $PSScriptRoot 'bootstrap-legacy.ps1'
& $bootstrap -ProjectRoot $ProjectRoot -CliArgs $CliArgs
exit $LASTEXITCODE

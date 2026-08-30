[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $WorkspaceRoot,

    [Parameter(Mandatory = $true)]
    [string] $ProjectRoot,

    [switch] $StopServer
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ProcessCreationUtc {
    param([Parameter(Mandatory = $true)] $Process)
    if ($Process.CreationDate -is [DateTime]) {
        return ([DateTime]$Process.CreationDate).ToUniversalTime()
    }
    return ([Management.ManagementDateTimeConverter]::ToDateTime(
        [string]$Process.CreationDate
    )).ToUniversalTime()
}

function Get-ProjectKey {
    param([Parameter(Mandatory = $true)][string] $Root)
    $Canonical = ([System.IO.Path]::GetFullPath($Root)).ToLowerInvariant().Replace('\', '/')
    $Sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $Hash = $Sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Canonical))
        return (([System.BitConverter]::ToString($Hash)).Replace('-', '').ToLowerInvariant()).Substring(0, 24)
    }
    finally {
        $Sha256.Dispose()
    }
}

function Stop-RecordedProcess {
    param(
        [Parameter(Mandatory = $true)][string] $RecordFile,
        [Parameter(Mandatory = $true)][ValidateSet('temporal-server', 'worker')][string] $Role,
        [Parameter(Mandatory = $true)][string] $ExpectedRecordedExecutable,
        [Parameter(Mandatory = $true)][string] $ExpectedLiveExecutable,
        [Parameter(Mandatory = $true)][string] $ExpectedWorkspaceRoot,
        [Parameter(Mandatory = $true)][string] $ExpectedProjectRoot
    )
    if (-not (Test-Path -LiteralPath $RecordFile -PathType Leaf)) {
        return [ordered]@{ role = $Role; state = 'absent' }
    }
    $RecordInfo = Get-Item -LiteralPath $RecordFile
    if ($RecordInfo.Length -gt 32768) {
        throw "Refusing oversized PID record: $RecordFile"
    }
    $Record = Get-Content -LiteralPath $RecordFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([int]$Record.schema_version -ne 1 -or [string]$Record.role -cne $Role) {
        throw "Refusing PID record with unexpected schema/role: $RecordFile"
    }
    if ([string]$Record.project_root -cne $ExpectedProjectRoot) {
        throw "Refusing PID record for another project: $RecordFile"
    }
    if ([string]$Record.workspace_root -cne $ExpectedWorkspaceRoot) {
        throw "Refusing PID record for another workspace: $RecordFile"
    }
    $ProcessId = [int64]$Record.pid
    if ($ProcessId -le 0) {
        throw "Refusing invalid PID: $ProcessId"
    }
    $RecordedExe = [System.IO.Path]::GetFullPath([string]$Record.executable)
    if ($RecordedExe -cne $ExpectedRecordedExecutable) {
        throw "Refusing unexpected executable in $RecordFile"
    }

    $Process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId"
    if ($null -eq $Process) {
        Remove-Item -LiteralPath $RecordFile -Force
        return [ordered]@{ role = $Role; state = 'stale_record_removed'; pid = $ProcessId }
    }
    $LiveExe = [System.IO.Path]::GetFullPath([string]$Process.ExecutablePath)
    if ($LiveExe -cne $ExpectedLiveExecutable) {
        throw "PID $ProcessId was reused by another executable; refusing to stop it."
    }
    $RecordedStart = [DateTimeOffset]::Parse([string]$Record.started_utc).UtcDateTime
    $LiveStart = Get-ProcessCreationUtc -Process $Process
    if ([Math]::Abs(($RecordedStart - $LiveStart).TotalSeconds) -gt 10) {
        throw "PID $ProcessId creation time does not match; refusing to stop it."
    }
    $CommandLine = [string]$Process.CommandLine
    $RequiredText = if ($Role -eq 'worker') {
        '-m codex_orchestrator.worker'
    }
    else {
        'server start-dev'
    }
    if (-not $CommandLine.Contains($RequiredText)) {
        throw "PID $ProcessId command line does not match $Role; refusing to stop it."
    }

    Stop-Process -Id $ProcessId -Force
    Wait-Process -Id $ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    if ($null -ne (Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId")) {
        throw "$Role PID $ProcessId did not exit within the bounded stop window."
    }
    Remove-Item -LiteralPath $RecordFile -Force
    return [ordered]@{ role = $Role; state = 'stopped'; pid = $ProcessId }
}

$WorkspaceRoot = (Resolve-Path -LiteralPath $WorkspaceRoot -ErrorAction Stop).Path
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path
$WorkspacePrefix = $WorkspaceRoot.TrimEnd('\') + '\'
if ($ProjectRoot -cne $WorkspaceRoot -and -not $ProjectRoot.StartsWith(
    $WorkspacePrefix, [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw 'ProjectRoot must equal or be below WorkspaceRoot.'
}
$ProjectKey = Get-ProjectKey -Root $ProjectRoot
$RuntimeRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $WorkspaceRoot '.workspace\tools\codex-orchestrator')
)
$VenvPython = [System.IO.Path]::GetFullPath(
    (Join-Path $RuntimeRoot '.venv\Scripts\python.exe')
)
$TemporalExe = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot 'bin\temporal.exe'))
$ManifestFile = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot 'runtime-manifest.json'))
$Manifest = Get-Content -LiteralPath $ManifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
$WorkerLiveExecutable = [System.IO.Path]::GetFullPath(
    [string]$Manifest.python_base_executable
)
$WorkerPidRecord = Join-Path $RuntimeRoot "pids\worker-$ProjectKey.json"
$TemporalPidRecord = Join-Path $RuntimeRoot 'pids\temporal-server.json'

# Stop the Worker first so no new activity can be dispatched while the local
# Temporal server is being taken down.
$WorkerResult = Stop-RecordedProcess `
    -RecordFile $WorkerPidRecord `
    -Role 'worker' `
    -ExpectedRecordedExecutable $VenvPython `
    -ExpectedLiveExecutable $WorkerLiveExecutable `
    -ExpectedWorkspaceRoot $WorkspaceRoot `
    -ExpectedProjectRoot $ProjectRoot
$TemporalResult = [ordered]@{ role = 'temporal-server'; state = 'not_requested' }
if ($StopServer) {
    $OtherWorkerRecords = @(Get-ChildItem `
        -LiteralPath (Join-Path $RuntimeRoot 'pids') `
        -Filter 'worker-*.json' `
        -File `
        -ErrorAction SilentlyContinue)
    if ($OtherWorkerRecords.Count -gt 0) {
        throw 'Other project Worker PID records remain; refusing to stop the shared server.'
    }
    $TemporalResult = Stop-RecordedProcess `
        -RecordFile $TemporalPidRecord `
        -Role 'temporal-server' `
        -ExpectedRecordedExecutable $TemporalExe `
        -ExpectedLiveExecutable $TemporalExe `
        -ExpectedWorkspaceRoot $WorkspaceRoot `
        -ExpectedProjectRoot $WorkspaceRoot
}

[ordered]@{
    ok = $true
    worker = $WorkerResult
    temporal_server = $TemporalResult
} | ConvertTo-Json -Depth 4 -Compress

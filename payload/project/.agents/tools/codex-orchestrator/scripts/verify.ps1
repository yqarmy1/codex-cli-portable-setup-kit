[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $WorkspaceRoot,

    [Parameter(Mandatory = $true)]
    [string] $ProjectRoot,

    [switch] $Live,

    [switch] $RequireLauncherReady
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TemporalCliVersion = '1.8.2'
$TemporalArchiveSha256 = '72e02498fa7849657c369377f7de69a8709b3d2183b6f2749f6c8bd54a984501'
$TemporalSdkVersion = '1.30.0'
$CodexSdkVersion = '0.144.4'
$TemporalAddress = '127.0.0.1:7233'
$TaskQueuePrefix = 'codex-orchestrator-v1'

function Resolve-ExistingPath {
    param([Parameter(Mandatory = $true)][string] $LiteralPath)
    return (Resolve-Path -LiteralPath $LiteralPath -ErrorAction Stop).Path
}

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)] $Actual,
        [Parameter(Mandatory = $true)] $Expected,
        [Parameter(Mandatory = $true)][string] $Name
    )
    if ([string]$Actual -cne [string]$Expected) {
        throw "$Name mismatch: expected '$Expected', got '$Actual'"
    }
}

function Get-ProjectKey {
    param([Parameter(Mandatory = $true)][string] $Root)
    $Canonical = ([System.IO.Path]::GetFullPath($Root)).ToLowerInvariant().Replace('\', '/')
    $Sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $Bytes = [System.Text.Encoding]::UTF8.GetBytes($Canonical)
        $Hash = $Sha256.ComputeHash($Bytes)
        return (([System.BitConverter]::ToString($Hash)).Replace('-', '').ToLowerInvariant()).Substring(0, 24)
    }
    finally {
        $Sha256.Dispose()
    }
}

function Get-ProcessCreationUtc {
    param([Parameter(Mandatory = $true)] $Process)
    if ($Process.CreationDate -is [DateTime]) {
        return ([DateTime]$Process.CreationDate).ToUniversalTime()
    }
    return ([Management.ManagementDateTimeConverter]::ToDateTime(
        [string]$Process.CreationDate
    )).ToUniversalTime()
}

function Get-PidRecordStatus {
    param(
        [Parameter(Mandatory = $true)][string] $RecordFile,
        [Parameter(Mandatory = $true)][ValidateSet('temporal-server', 'worker')][string] $Role,
        [Parameter(Mandatory = $true)][string] $ExpectedRecordedExecutable,
        [Parameter(Mandatory = $true)][string] $ExpectedLiveExecutable,
        [Parameter(Mandatory = $true)][string] $ExpectedWorkspaceRoot,
        [Parameter(Mandatory = $true)][string] $ExpectedProjectRoot,
        [Parameter(Mandatory = $true)][string] $ExpectedDb
    )
    if (-not (Test-Path -LiteralPath $RecordFile -PathType Leaf)) {
        return [ordered]@{ role = $Role; state = 'absent' }
    }
    $RecordInfo = Get-Item -LiteralPath $RecordFile
    if ($RecordInfo.Length -gt 32768) {
        throw "PID record is unexpectedly large: $RecordFile"
    }
    $Record = Get-Content -LiteralPath $RecordFile -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-Equal -Actual $Record.schema_version -Expected 1 -Name "$Role PID schema"
    Assert-Equal -Actual $Record.role -Expected $Role -Name "$Role PID role"
    Assert-Equal -Actual $Record.workspace_root -Expected $ExpectedWorkspaceRoot -Name "$Role workspace root"
    Assert-Equal -Actual $Record.project_root -Expected $ExpectedProjectRoot -Name "$Role project root"
    if ([int64]$Record.pid -le 0) {
        throw "$Role PID is not positive."
    }
    $RecordedExe = [System.IO.Path]::GetFullPath([string]$Record.executable)
    Assert-Equal -Actual $RecordedExe -Expected $ExpectedRecordedExecutable -Name "$Role recorded executable"

    $Process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $([int64]$Record.pid)"
    if ($null -eq $Process) {
        throw "$Role PID record is stale: $RecordFile"
    }
    $LiveExe = [System.IO.Path]::GetFullPath([string]$Process.ExecutablePath)
    Assert-Equal -Actual $LiveExe -Expected $ExpectedLiveExecutable -Name "$Role live executable"
    $CommandLine = [string]$Process.CommandLine
    if ($Role -eq 'temporal-server') {
        foreach ($RequiredText in @(
            'server start-dev', '--ip 127.0.0.1', '--port 7233',
            '--ui-ip 127.0.0.1', '--ui-port 8233', '--db-filename', $ExpectedDb
        )) {
            if (-not $CommandLine.Contains($RequiredText)) {
                throw "Temporal PID command line is missing pinned text: $RequiredText"
            }
        }
    }
    else {
        foreach ($RequiredText in @(
            '-m codex_orchestrator.worker', '--workspace-root', $ExpectedWorkspaceRoot,
            '--project-root', $ExpectedProjectRoot,
            '--temporal-address 127.0.0.1:7233', "--task-queue $TaskQueue"
        )) {
            if (-not $CommandLine.Contains($RequiredText)) {
                throw "Worker PID command line is missing pinned text: $RequiredText"
            }
        }
    }
    $RecordedStart = [DateTimeOffset]::Parse([string]$Record.started_utc).UtcDateTime
    $LiveStart = Get-ProcessCreationUtc -Process $Process
    if ([Math]::Abs(($RecordedStart - $LiveStart).TotalSeconds) -gt 10) {
        throw "$Role PID creation time does not match the recorded owner."
    }
    return [ordered]@{ role = $Role; state = 'active'; pid = [int64]$Record.pid }
}

$CandidateRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$WorkspaceRoot = Resolve-ExistingPath -LiteralPath $WorkspaceRoot
$ProjectRoot = Resolve-ExistingPath -LiteralPath $ProjectRoot
if (-not (Test-Path -LiteralPath $WorkspaceRoot -PathType Container)) {
    throw "WorkspaceRoot is not a directory: $WorkspaceRoot"
}
if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw "ProjectRoot is not a directory: $ProjectRoot"
}
$WorkspacePrefix = $WorkspaceRoot.TrimEnd('\') + '\'
if ($ProjectRoot -cne $WorkspaceRoot -and -not $ProjectRoot.StartsWith(
    $WorkspacePrefix, [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw 'ProjectRoot must equal or be below WorkspaceRoot.'
}
$ProjectKey = Get-ProjectKey -Root $ProjectRoot
$TaskQueue = "$TaskQueuePrefix-$ProjectKey"
$RuntimeRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $WorkspaceRoot '.workspace\tools\codex-orchestrator')
)
$VenvPython = Resolve-ExistingPath -LiteralPath (Join-Path $RuntimeRoot '.venv\Scripts\python.exe')
$TemporalExe = Resolve-ExistingPath -LiteralPath (Join-Path $RuntimeRoot 'bin\temporal.exe')
$TemporalDb = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot 'temporal\state.db'))
$ManifestFile = Resolve-ExistingPath -LiteralPath (Join-Path $RuntimeRoot 'runtime-manifest.json')
$TemporalPidRecord = Join-Path $RuntimeRoot 'pids\temporal-server.json'
$WorkerPidRecord = Join-Path $RuntimeRoot "pids\worker-$ProjectKey.json"

$Manifest = Get-Content -LiteralPath $ManifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
Assert-Equal -Actual $Manifest.schema_version -Expected 1 -Name 'manifest schema'
Assert-Equal -Actual $Manifest.workspace_root -Expected $WorkspaceRoot -Name 'manifest workspace root'
Assert-Equal -Actual $Manifest.runtime_root -Expected $RuntimeRoot -Name 'manifest runtime root'
Assert-Equal -Actual $Manifest.temporal_cli_version -Expected $TemporalCliVersion -Name 'Temporal CLI version'
Assert-Equal -Actual $Manifest.temporal_archive_sha256 -Expected $TemporalArchiveSha256 -Name 'Temporal archive hash'
Assert-Equal -Actual $Manifest.temporalio_version -Expected $TemporalSdkVersion -Name 'Temporal Python SDK version'
Assert-Equal -Actual $Manifest.openai_codex_version -Expected $CodexSdkVersion -Name 'Codex SDK version'
Assert-Equal -Actual $Manifest.temporal_address -Expected $TemporalAddress -Name 'Temporal address'
Assert-Equal -Actual $Manifest.temporal_ui_address -Expected '127.0.0.1:8233' -Name 'Temporal UI address'
Assert-Equal -Actual $Manifest.temporal_db -Expected $TemporalDb -Name 'Temporal DB path'
Assert-Equal -Actual $Manifest.task_queue_prefix -Expected $TaskQueuePrefix -Name 'task queue prefix'
if ([string]$Manifest.python_version -notmatch '^3\.11\.') {
    throw "Manifest Python version is not 3.11.x: $($Manifest.python_version)"
}
$ManifestPython = [System.IO.Path]::GetFullPath([string]$Manifest.python_executable)
$ManifestBasePython = Resolve-ExistingPath -LiteralPath ([string]$Manifest.python_base_executable)
Assert-Equal -Actual $ManifestPython -Expected $VenvPython -Name 'manifest venv Python'

$TemporalExeHash = (Get-FileHash -LiteralPath $TemporalExe -Algorithm SHA256).Hash.ToLowerInvariant()
Assert-Equal -Actual $TemporalExeHash -Expected ([string]$Manifest.temporal_executable_sha256) -Name 'Temporal executable hash'
$TemporalVersionText = (& $TemporalExe --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $TemporalVersionText -notmatch '(^|\s)v?1\.8\.2($|\s)') {
    throw "Unexpected Temporal CLI version output: $TemporalVersionText"
}

$PackageVersionsJson = & $VenvPython -m codex_orchestrator.runtime_probe
if ($LASTEXITCODE -ne 0) {
    throw 'Python package verification failed.'
}
$PackageVersions = $PackageVersionsJson | ConvertFrom-Json
Assert-Equal -Actual $PackageVersions.ok -Expected $true -Name 'Python runtime probe'
if ([string]$PackageVersions.python -notmatch '^3\.11\.') {
    throw "Candidate Python is not 3.11.x: $($PackageVersions.python)"
}
Assert-Equal -Actual $PackageVersions.temporalio -Expected $TemporalSdkVersion -Name 'installed temporalio'
Assert-Equal -Actual $PackageVersions.'openai-codex' -Expected $CodexSdkVersion -Name 'installed openai-codex'
Assert-Equal `
    -Actual ([System.IO.Path]::GetFullPath([string]$PackageVersions.python_executable)) `
    -Expected $VenvPython `
    -Name 'runtime probe venv Python'
Assert-Equal `
    -Actual ([System.IO.Path]::GetFullPath([string]$PackageVersions.python_base_executable)) `
    -Expected $ManifestBasePython `
    -Name 'runtime probe base Python'

$WorkerCheckJson = & $VenvPython -m codex_orchestrator.worker `
    --workspace-root $WorkspaceRoot `
    --project-root $ProjectRoot `
    --temporal-address $TemporalAddress `
    --task-queue $TaskQueue `
    --check-config
if ($LASTEXITCODE -ne 0) {
    throw 'Worker configuration check failed.'
}
$WorkerCheck = $WorkerCheckJson | ConvertFrom-Json
Assert-Equal -Actual $WorkerCheck.ok -Expected $true -Name 'worker configuration'

$RuntimeInfoJson = & $VenvPython -m codex_orchestrator.cli `
    --workspace-root $WorkspaceRoot `
    --project-root $ProjectRoot `
    --temporal-address $TemporalAddress `
    --task-queue $TaskQueue `
    runtime-info
if ($LASTEXITCODE -ne 0) {
    throw 'CLI runtime-info check failed.'
}
$RuntimeInfo = $RuntimeInfoJson | ConvertFrom-Json
Assert-Equal -Actual $RuntimeInfo.temporal_db -Expected $TemporalDb -Name 'CLI Temporal DB path'
Assert-Equal -Actual $RuntimeInfo.task_queue -Expected $TaskQueue -Name 'project task queue'
$ServerArguments = @($RuntimeInfo.server_arguments)
if ($ServerArguments -notcontains '--db-filename' -or $ServerArguments -notcontains $TemporalDb) {
    throw 'CLI server plan does not require the persistent DB.'
}
if ($ServerArguments -notcontains '--ip' -or $ServerArguments -notcontains '127.0.0.1') {
    throw 'CLI server plan is not pinned to loopback.'
}

$TemporalPid = Get-PidRecordStatus `
    -RecordFile $TemporalPidRecord `
    -Role 'temporal-server' `
    -ExpectedRecordedExecutable $TemporalExe `
    -ExpectedLiveExecutable $TemporalExe `
    -ExpectedWorkspaceRoot $WorkspaceRoot `
    -ExpectedProjectRoot $WorkspaceRoot `
    -ExpectedDb $TemporalDb
$WorkerPid = Get-PidRecordStatus `
    -RecordFile $WorkerPidRecord `
    -Role 'worker' `
    -ExpectedRecordedExecutable $VenvPython `
    -ExpectedLiveExecutable $ManifestBasePython `
    -ExpectedWorkspaceRoot $WorkspaceRoot `
    -ExpectedProjectRoot $ProjectRoot `
    -ExpectedDb $TemporalDb

if ($Live) {
    Assert-Equal -Actual $TemporalPid.state -Expected 'active' -Name 'Temporal PID state'
    Assert-Equal -Actual $WorkerPid.state -Expected 'active' -Name 'worker PID state'
    & $TemporalExe --address $TemporalAddress --command-timeout 5s `
        operator namespace describe --namespace default | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Bounded live Temporal namespace check failed.'
    }
}

$LauncherBlockers = @(
    'user_output_sink_not_implemented',
    'app_message_ingress_not_implemented',
    'payload_encryption_not_configured'
)
if ($RequireLauncherReady) {
    throw ('Launcher replacement gate is intentionally blocked: ' + ($LauncherBlockers -join ', '))
}

[ordered]@{
    ok = $true
    control_plane_ok = $true
    launcher_replacement_ready = $false
    launcher_blockers = $LauncherBlockers
    user_visible_assistant_output = $false
    mode = $(if ($Live) { 'live' } else { 'offline' })
    temporal_cli_version = $TemporalCliVersion
    temporalio_version = $TemporalSdkVersion
    openai_codex_version = $CodexSdkVersion
    temporal_pid = $TemporalPid
    worker_pid = $WorkerPid
    live_checks = $(if ($Live) { 1 } else { 0 })
    polling_loops = 0
} | ConvertTo-Json -Depth 5 -Compress

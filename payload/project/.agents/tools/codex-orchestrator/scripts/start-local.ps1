[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $WorkspaceRoot,

    [Parameter(Mandatory = $true)]
    [string] $ProjectRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TemporalAddress = '127.0.0.1:7233'
$TaskQueuePrefix = 'codex-orchestrator-v1'

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

function Quote-ProcessArgument {
    param([Parameter(Mandatory = $true)][string] $Value)
    if ($Value.Contains('"')) {
        throw 'Process arguments containing a quote are unsupported.'
    }
    if ($Value -match '\s') {
        return '"' + $Value + '"'
    }
    return $Value
}

function Join-ProcessArguments {
    param([Parameter(Mandatory = $true)][string[]] $ArgumentList)
    return (($ArgumentList | ForEach-Object { Quote-ProcessArgument -Value $_ }) -join ' ')
}

function Invoke-TemporalHealthProbe {
    param(
        [Parameter(Mandatory = $true)][string] $Executable,
        [Parameter(Mandatory = $true)][string] $Address,
        [Parameter(Mandatory = $true)][string] $CommandTimeout
    )
    # Connection refusal is expected while start-dev is booting.  Capture only
    # the native exit code instead of letting stderr bypass the bounded retry.
    $PreviousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'SilentlyContinue'
        & $Executable --address $Address --command-timeout $CommandTimeout `
            operator namespace describe --namespace default 2>&1 | Out-Null
        return [int]$LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousErrorAction
    }
}

function Write-NewPidRecord {
    param(
        [Parameter(Mandatory = $true)][string] $RecordFile,
        [Parameter(Mandatory = $true)][hashtable] $Record
    )
    if (Test-Path -LiteralPath $RecordFile) {
        throw "Refusing to replace an existing PID record: $RecordFile"
    }
    $TempFile = $RecordFile + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText(
            $TempFile,
            ($Record | ConvertTo-Json -Depth 5),
            $Utf8NoBom
        )
        [System.IO.File]::Move($TempFile, $RecordFile)
    }
    finally {
        if (Test-Path -LiteralPath $TempFile -PathType Leaf) {
            Remove-Item -LiteralPath $TempFile -Force
        }
    }
}

function Remove-OwnedPidRecord {
    param(
        [Parameter(Mandatory = $true)][string] $RecordFile,
        [Parameter(Mandatory = $true)][int64] $ProcessId
    )
    if (-not (Test-Path -LiteralPath $RecordFile -PathType Leaf)) {
        return
    }
    try {
        $Record = Get-Content -LiteralPath $RecordFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([int64]$Record.pid -eq $ProcessId) {
            Remove-Item -LiteralPath $RecordFile -Force
        }
    }
    catch {
        # Preserve an unreadable record as evidence rather than deleting it.
    }
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
$TaskQueue = "$TaskQueuePrefix-$ProjectKey"
$RuntimeRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $WorkspaceRoot '.workspace\tools\codex-orchestrator')
)
$VenvPython = (Resolve-Path -LiteralPath (Join-Path $RuntimeRoot '.venv\Scripts\python.exe')).Path
$TemporalExe = (Resolve-Path -LiteralPath (Join-Path $RuntimeRoot 'bin\temporal.exe')).Path
$TemporalDb = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot 'temporal\state.db'))
$LogsDir = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot 'logs'))
$PidsDir = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot 'pids'))
$TemporalPidRecord = Join-Path $PidsDir 'temporal-server.json'
$WorkerPidRecord = Join-Path $PidsDir "worker-$ProjectKey.json"
$VerifyScript = Join-Path $PSScriptRoot 'verify.ps1'

# This verifies pins, hashes, loopback, persistence, imports, and the absence of
# stale PID records.  It performs no live RPC unless -Live is explicit.
$OfflineJson = & $VerifyScript `
    -WorkspaceRoot $WorkspaceRoot `
    -ProjectRoot $ProjectRoot
if ($LASTEXITCODE -ne 0) {
    throw 'Offline verification failed.'
}
$Offline = $OfflineJson | ConvertFrom-Json
if ([string]$Offline.worker_pid.state -ne 'absent') {
    throw "The project Worker is already recorded as $($Offline.worker_pid.state)."
}

$TemporalArguments = @(
    'server', 'start-dev',
    '--ip', '127.0.0.1',
    '--port', '7233',
    '--ui-ip', '127.0.0.1',
    '--ui-port', '8233',
    '--db-filename', $TemporalDb
)
$WorkerArguments = @(
    '-m', 'codex_orchestrator.worker',
    '--workspace-root', $WorkspaceRoot,
    '--project-root', $ProjectRoot,
    '--temporal-address', $TemporalAddress,
    '--task-queue', $TaskQueue
)

$TemporalProcess = $null
$WorkerProcess = $null
$WorkerRecordedProcessId = $null
$StartedTemporalHere = $false
try {
    if ([string]$Offline.temporal_pid.state -eq 'absent') {
        $TemporalProcess = Start-Process `
            -FilePath $TemporalExe `
            -ArgumentList (Join-ProcessArguments -ArgumentList $TemporalArguments) `
            -WorkingDirectory $RuntimeRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $LogsDir 'temporal-server.out.log') `
            -RedirectStandardError (Join-Path $LogsDir 'temporal-server.err.log') `
            -PassThru
        $StartedTemporalHere = $true
        Write-NewPidRecord -RecordFile $TemporalPidRecord -Record @{
            schema_version = 1
            role = 'temporal-server'
            pid = [int64]$TemporalProcess.Id
            executable = $TemporalExe
            arguments = $TemporalArguments
            workspace_root = $WorkspaceRoot
            project_root = $WorkspaceRoot
            started_utc = $TemporalProcess.StartTime.ToUniversalTime().ToString('o')
        }

        $TemporalReady = $false
        for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
            if ($TemporalProcess.HasExited) {
                throw "Temporal server exited with code $($TemporalProcess.ExitCode)."
            }
            $HealthExitCode = Invoke-TemporalHealthProbe `
                -Executable $TemporalExe `
                -Address $TemporalAddress `
                -CommandTimeout '1s'
            if ($HealthExitCode -eq 0) {
                $TemporalReady = $true
                break
            }
            Start-Sleep -Milliseconds 500
        }
        if (-not $TemporalReady) {
            throw 'Temporal server did not pass the bounded readiness gate.'
        }
    }
    else {
        $HealthExitCode = Invoke-TemporalHealthProbe `
            -Executable $TemporalExe `
            -Address $TemporalAddress `
            -CommandTimeout '5s'
        if ($HealthExitCode -ne 0) {
            throw 'The recorded workspace Temporal server failed the one-shot health check.'
        }
    }

    $WorkerProcess = Start-Process `
        -FilePath $VenvPython `
        -ArgumentList (Join-ProcessArguments -ArgumentList $WorkerArguments) `
        -WorkingDirectory $RuntimeRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogsDir "worker-$ProjectKey.out.log") `
        -RedirectStandardError (Join-Path $LogsDir "worker-$ProjectKey.err.log") `
        -PassThru

    $WorkerRecorded = $false
    for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
        if (Test-Path -LiteralPath $WorkerPidRecord -PathType Leaf) {
            $Record = Get-Content -LiteralPath $WorkerPidRecord -Raw -Encoding UTF8 | ConvertFrom-Json
            $WorkerRecordedProcessId = [int64]$Record.pid
            if ($WorkerRecordedProcessId -le 0) {
                throw 'Worker wrote an invalid PID record.'
            }
            $WorkerRecorded = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $WorkerRecorded) {
        $WorkerProcess.Refresh()
        $LauncherDetail = if ($WorkerProcess.HasExited) {
            "launcher exited with code $($WorkerProcess.ExitCode)"
        }
        else {
            'launcher remained alive'
        }
        throw "Worker did not create its PID record within the bounded gate; $LauncherDetail."
    }

    & $VerifyScript `
        -WorkspaceRoot $WorkspaceRoot `
        -ProjectRoot $ProjectRoot `
        -Live | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Live verification failed.'
    }

    [ordered]@{
        ok = $true
        launcher_replacement_ready = $false
        temporal_pid = $(
            if ($StartedTemporalHere) { [int64]$TemporalProcess.Id }
            else { [int64]$Offline.temporal_pid.pid }
        )
        temporal_reused = -not $StartedTemporalHere
        worker_pid = $WorkerRecordedProcessId
        worker_launcher_pid = [int64]$WorkerProcess.Id
        workflow_task_queue = $TaskQueue
        temporal_address = $TemporalAddress
        temporal_ui = 'http://127.0.0.1:8233'
        temporal_db = $TemporalDb
        readiness_attempt_limit = 20
        worker_record_attempt_limit = 40
    } | ConvertTo-Json -Compress
}
catch {
    $StartupError = $_
    try {
        & (Join-Path $PSScriptRoot 'stop-local.ps1') `
            -WorkspaceRoot $WorkspaceRoot `
            -ProjectRoot $ProjectRoot | Out-Null
    }
    catch {
        # Preserve the original startup failure. Any record that cannot be
        # validated remains as evidence and will block the next start.
    }
    if ($null -ne $WorkerProcess) {
        if (-not $WorkerProcess.HasExited) {
            Stop-Process -Id $WorkerProcess.Id -Force -ErrorAction SilentlyContinue
        }
    }
    if ($StartedTemporalHere -and $null -ne $TemporalProcess) {
        if (-not $TemporalProcess.HasExited) {
            Stop-Process -Id $TemporalProcess.Id -Force -ErrorAction SilentlyContinue
        }
        if ($TemporalProcess.HasExited) {
            Remove-OwnedPidRecord -RecordFile $TemporalPidRecord -ProcessId $TemporalProcess.Id
        }
    }
    throw $StartupError
}

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $WorkspaceRoot,

    [string] $PythonExe,

    [string] $TemporalArchive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TemporalCliVersion = '1.8.2'
$TemporalArchiveName = 'temporal_cli_1.8.2_windows_amd64.zip'
$TemporalArchiveSha256 = '72e02498fa7849657c369377f7de69a8709b3d2183b6f2749f6c8bd54a984501'
$TemporalDownloadUrl = 'https://github.com/temporalio/cli/releases/download/v1.8.2/temporal_cli_1.8.2_windows_amd64.zip'
$TemporalSdkVersion = '1.30.0'
$CodexSdkVersion = '0.144.4'
$CryptographyVersion = '49.0.0'
$TaskQueuePrefix = 'codex-orchestrator-v2'

function Resolve-ExistingPath {
    param([Parameter(Mandatory = $true)][string] $LiteralPath)
    return (Resolve-Path -LiteralPath $LiteralPath -ErrorAction Stop).Path
}

function Assert-ChildPath {
    param(
        [Parameter(Mandatory = $true)][string] $Child,
        [Parameter(Mandatory = $true)][string] $Parent
    )
    $parentPrefix = $Parent.TrimEnd('\') + '\'
    if (-not $Child.StartsWith($parentPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes the expected root: $Child"
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string] $Executable,
        [Parameter(Mandatory = $true)][string[]] $ArgumentList
    )
    & $Executable @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Executable"
    }
}

function Set-PrivatePayloadAcl {
    param(
        [Parameter(Mandatory = $true)][string] $LiteralPath,
        [switch] $Directory
    )
    $CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $Propagation = $(if ($Directory) { '(OI)(CI)' } else { '' })
    $AclArguments = @(
        $LiteralPath,
        '/inheritance:r',
        '/grant:r',
        "*$($CurrentSid):$($Propagation)(F)",
        '*S-1-5-18:' + $Propagation + '(F)',
        '*S-1-5-32-544:' + $Propagation + '(F)'
    )
    & $script:IcaclsExe @AclArguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to apply the private payload-key ACL.'
    }
}

$CandidateRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$WorkspaceRoot = Resolve-ExistingPath -LiteralPath $WorkspaceRoot
if (-not (Test-Path -LiteralPath $WorkspaceRoot -PathType Container)) {
    throw "WorkspaceRoot is not a directory: $WorkspaceRoot"
}
if ($WorkspaceRoot.Contains('"')) {
    throw 'WorkspaceRoot containing a quote is unsupported.'
}

$RuntimeRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $WorkspaceRoot '.workspace\tools\codex-orchestrator')
)
Assert-ChildPath -Child $RuntimeRoot -Parent $WorkspaceRoot
$VenvRoot = Join-Path $RuntimeRoot '.venv'
$VenvPython = Join-Path $VenvRoot 'Scripts\python.exe'
$BinDir = Join-Path $RuntimeRoot 'bin'
$TemporalExe = Join-Path $BinDir 'temporal.exe'
$TemporalDir = Join-Path $RuntimeRoot 'temporal'
$TemporalDb = Join-Path $TemporalDir 'state-v2.db'
$SecretsDir = Join-Path $RuntimeRoot 'secrets'
$PayloadKeyFile = Join-Path $SecretsDir 'temporal-payload-aes256.key'
$LogsDir = Join-Path $RuntimeRoot 'logs'
$PidsDir = Join-Path $RuntimeRoot 'pids'
$LocksDir = Join-Path $RuntimeRoot 'locks'
$DownloadsDir = Join-Path $RuntimeRoot 'downloads'
$ManifestFile = Join-Path $RuntimeRoot 'runtime-manifest.json'
$RequirementsLock = Join-Path $CandidateRoot 'requirements.lock'
$IcaclsExe = Join-Path ([Environment]::SystemDirectory) 'icacls.exe'
$IcaclsExe = Resolve-ExistingPath -LiteralPath $IcaclsExe

foreach ($directory in @(
    $RuntimeRoot, $BinDir, $TemporalDir, $SecretsDir, $LogsDir, $PidsDir,
    $LocksDir, $DownloadsDir
)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    Assert-ChildPath -Child ([System.IO.Path]::GetFullPath($directory)) -Parent $WorkspaceRoot
}
Set-PrivatePayloadAcl -LiteralPath $SecretsDir -Directory

$ExistingPidRecords = @(Get-ChildItem `
    -LiteralPath $PidsDir `
    -Filter '*.json' `
    -File `
    -ErrorAction SilentlyContinue)
if ($ExistingPidRecords.Count -gt 0) {
    throw 'Refusing bootstrap while PID records exist; verify and stop the recorded runtime first.'
}

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    if ([string]::IsNullOrWhiteSpace($PythonExe)) {
        throw 'PythonExe is required the first time the candidate venv is created.'
    }
    $PythonExe = Resolve-ExistingPath -LiteralPath $PythonExe
    $BasePythonVersion = & $PythonExe -c 'import platform; print(platform.python_version())'
    if ($LASTEXITCODE -ne 0 -or $BasePythonVersion -notmatch '^3\.11\.') {
        throw "Bootstrap requires CPython 3.11.x; got $BasePythonVersion"
    }
    Invoke-Checked -Executable $PythonExe -ArgumentList @('-m', 'venv', $VenvRoot)
}

$VenvPython = Resolve-ExistingPath -LiteralPath $VenvPython
$VenvPythonVersion = & $VenvPython -c 'import platform; print(platform.python_version())'
if ($LASTEXITCODE -ne 0 -or $VenvPythonVersion -notmatch '^3\.11\.') {
    throw "Candidate venv must use CPython 3.11.x; got $VenvPythonVersion"
}

# No dependency is resolved implicitly: every runtime distribution is listed in
# requirements.lock, and optional dependency trees are not installed.
Invoke-Checked -Executable $VenvPython -ArgumentList @(
    '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
    '--timeout', '30', '--retries', '1', '--only-binary=:all:', '--no-deps',
    '--requirement', $RequirementsLock
)
Invoke-Checked -Executable $VenvPython -ArgumentList @(
    '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
    '--no-deps', '--no-build-isolation', '--editable', $CandidateRoot
)

$PinnedVersionsJson = & $VenvPython -m codex_orchestrator.runtime_probe
if ($LASTEXITCODE -ne 0) {
    throw 'Pinned Python SDK verification failed.'
}
$PinnedVersions = $PinnedVersionsJson | ConvertFrom-Json
if (-not [bool]$PinnedVersions.ok) {
    throw 'Pinned Python SDK probe returned ok=false.'
}

if ([string]::IsNullOrWhiteSpace($TemporalArchive)) {
    $TemporalArchive = Join-Path $DownloadsDir $TemporalArchiveName
    $NeedDownload = $true
    if (Test-Path -LiteralPath $TemporalArchive -PathType Leaf) {
        $CachedHash = (Get-FileHash -LiteralPath $TemporalArchive -Algorithm SHA256).Hash.ToLowerInvariant()
        $NeedDownload = $CachedHash -ne $TemporalArchiveSha256
    }
    if ($NeedDownload) {
        $DownloadTemp = Join-Path $DownloadsDir ($TemporalArchiveName + '.download')
        try {
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 60 -Uri $TemporalDownloadUrl -OutFile $DownloadTemp
            $DownloadedHash = (Get-FileHash -LiteralPath $DownloadTemp -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($DownloadedHash -ne $TemporalArchiveSha256) {
                throw "Temporal archive SHA-256 mismatch: $DownloadedHash"
            }
            Move-Item -LiteralPath $DownloadTemp -Destination $TemporalArchive -Force
        }
        finally {
            if (Test-Path -LiteralPath $DownloadTemp -PathType Leaf) {
                Remove-Item -LiteralPath $DownloadTemp -Force
            }
        }
    }
}
else {
    $TemporalArchive = Resolve-ExistingPath -LiteralPath $TemporalArchive
}

$ArchiveHash = (Get-FileHash -LiteralPath $TemporalArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($ArchiveHash -ne $TemporalArchiveSha256) {
    throw "Temporal archive SHA-256 mismatch: $ArchiveHash"
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$TempTemporalExe = Join-Path $BinDir 'temporal.exe.bootstrap'
try {
    $Zip = [System.IO.Compression.ZipFile]::OpenRead($TemporalArchive)
    try {
        $Entries = @($Zip.Entries | Where-Object { $_.FullName -eq 'temporal.exe' })
        if ($Entries.Count -ne 1) {
            throw 'Pinned archive does not contain exactly one root temporal.exe.'
        }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile(
            $Entries[0], $TempTemporalExe, $true
        )
    }
    finally {
        $Zip.Dispose()
    }
    Move-Item -LiteralPath $TempTemporalExe -Destination $TemporalExe -Force
}
finally {
    if (Test-Path -LiteralPath $TempTemporalExe -PathType Leaf) {
        Remove-Item -LiteralPath $TempTemporalExe -Force
    }
}

$TemporalVersionText = (& $TemporalExe --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $TemporalVersionText -notmatch '(^|\s)v?1\.8\.2($|\s)') {
    throw "Unexpected Temporal CLI version output: $TemporalVersionText"
}
$TemporalExeHash = (Get-FileHash -LiteralPath $TemporalExe -Algorithm SHA256).Hash.ToLowerInvariant()

$Manifest = [ordered]@{
    schema_version = 1
    created_utc = [DateTime]::UtcNow.ToString('o')
    workspace_root = $WorkspaceRoot
    runtime_root = $RuntimeRoot
    python_version = $VenvPythonVersion
    python_executable = [string]$PinnedVersions.python_executable
    python_base_executable = [string]$PinnedVersions.python_base_executable
    temporal_cli_version = $TemporalCliVersion
    temporal_archive = $TemporalArchiveName
    temporal_archive_sha256 = $TemporalArchiveSha256
    temporal_executable_sha256 = $TemporalExeHash
    temporalio_version = [string]$PinnedVersions.temporalio
    openai_codex_version = [string]$PinnedVersions.'openai-codex'
    temporal_address = '127.0.0.1:7233'
    temporal_ui_address = '127.0.0.1:8233'
    temporal_db = $TemporalDb
    task_queue_prefix = 'codex-orchestrator-v1'
}
$ManifestTemp = Join-Path $RuntimeRoot 'runtime-manifest.json.bootstrap'
try {
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $ManifestTemp,
        ($Manifest | ConvertTo-Json -Depth 4),
        $Utf8NoBom
    )
    Move-Item -LiteralPath $ManifestTemp -Destination $ManifestFile -Force
}
finally {
    if (Test-Path -LiteralPath $ManifestTemp -PathType Leaf) {
        Remove-Item -LiteralPath $ManifestTemp -Force
    }
}

[ordered]@{
    ok = $true
    started_services = $false
    runtime_root = $RuntimeRoot
    manifest = $ManifestFile
    next = "Run scripts\verify.ps1 -WorkspaceRoot `"$WorkspaceRoot`" -ProjectRoot `"$WorkspaceRoot`" before scripts\start-local.ps1."
} | ConvertTo-Json -Compress

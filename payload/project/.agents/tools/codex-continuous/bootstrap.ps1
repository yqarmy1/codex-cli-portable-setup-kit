param(
    [string]$ProjectRoot = (Get-Location).Path,
    [switch]$Doctor,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$resolvedProject = (Resolve-Path -LiteralPath $ProjectRoot).Path
$installRoot = Join-Path $env:LOCALAPPDATA 'Programs\CodexClaudeBridge'
$claudeVersion = '2.1.220'
$claudeExeHash = 'af5bf1f1b2aadffc768eccd787084c6fdf9ba81624cbe96c1c6d9ac1a1550231'
$claudeRoot = Join-Path $installRoot "claude\v$claudeVersion"
$claudeExe = Join-Path $claudeRoot 'claude.exe'
$proxyVersion = 'v0.1.30'
$proxyArchiveHash = '7ee1e9c275de326e97ea7914f9eafa74ed7fb6bfa60223e3fafc0e0daf02e233'
$proxyExeHash = '5827c326a74023d24d95958850efa354f54a32e4876a9dccbefd4cd5df977687'
$proxyRoot = Join-Path $installRoot "proxy\$proxyVersion"
$proxyExe = Join-Path $proxyRoot 'claude-code-proxy.exe'
$emptyMcp = Join-Path $installRoot 'empty-mcp.json'
$bundledEmptyMcp = Join-Path $PSScriptRoot 'empty-mcp.json'
$emptyMcpHash = 'd8e397af03b5b032f21d0aa967086f0c78b33c87b76f2e9898ae0a144df7de02'
$proxyConfigRoot = Join-Path $env:APPDATA 'claude-code-proxy'
$claudeConfigRoot = Join-Path $installRoot 'claude-config'

function Set-PrivateDirectoryAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    $current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $admins = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $acl = [System.Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($current)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($current, $system, $admins)) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Test-PrivateDirectoryAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return $false
    }
    $allowed = @(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
        'S-1-5-18',
        'S-1-5-32-544'
    )
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        return $false
    }
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne 'Allow') {
            continue
        }
        $sid = $rule.IdentityReference.Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
        if ($sid -notin $allowed) {
            return $false
        }
    }
    return $true
}

Set-PrivateDirectoryAcl -Path $installRoot
Set-PrivateDirectoryAcl -Path $proxyConfigRoot
Set-PrivateDirectoryAcl -Path $claudeConfigRoot

function Install-PinnedProxy {
    $tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('ccp-install-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tempRoot | Out-Null
    try {
        $archive = Join-Path $tempRoot 'claude-code-proxy-windows-amd64.zip'
        $url = "https://github.com/raine/claude-code-proxy/releases/download/$proxyVersion/claude-code-proxy-windows-amd64.zip"
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive
        $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $proxyArchiveHash) {
            throw 'claude-code-proxy archive hash mismatch.'
        }
        New-Item -ItemType Directory -Force -Path $proxyRoot | Out-Null
        Expand-Archive -LiteralPath $archive -DestinationPath $proxyRoot -Force
    } finally {
        $resolvedTemp = [IO.Path]::GetFullPath($tempRoot)
        $systemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        if ($resolvedTemp.StartsWith($systemTemp, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedTemp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

$proxyPresent = Test-Path -LiteralPath $proxyExe -PathType Leaf
if (-not $proxyPresent -and -not $Doctor) {
    Install-PinnedProxy
    $proxyPresent = $true
}
if ($proxyPresent) {
    $actualProxyHash = (Get-FileHash -LiteralPath $proxyExe -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualProxyHash -ne $proxyExeHash) {
        throw 'claude-code-proxy executable hash mismatch.'
    }
    if ((& $proxyExe --version) -ne 'claude-code-proxy 0.1.30') {
        throw 'Unexpected claude-code-proxy version.'
    }
}

if (-not (Test-Path -LiteralPath $claudeExe -PathType Leaf)) {
    throw 'Protected official Claude Code executable is missing.'
}
$actualClaudeHash = (Get-FileHash -LiteralPath $claudeExe -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualClaudeHash -ne $claudeExeHash) {
    throw 'Official Claude Code executable hash mismatch.'
}
if ((& $claudeExe --version) -ne "$claudeVersion (Claude Code)") {
    throw 'Unexpected Claude Code version.'
}
if (-not (Test-Path -LiteralPath $emptyMcp -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $bundledEmptyMcp -PathType Leaf)) {
        throw 'Bundled empty MCP configuration is missing.'
    }
    Copy-Item -LiteralPath $bundledEmptyMcp -Destination $emptyMcp
}
$actualEmptyMcpHash = (Get-FileHash -LiteralPath $emptyMcp -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualEmptyMcpHash -ne $emptyMcpHash) {
    throw 'Protected empty MCP configuration hash mismatch.'
}

$env:CCP_CONFIG_DIR = $proxyConfigRoot
$env:CCP_BIND_ADDRESS = '127.0.0.1'
$env:CCP_ALIAS_PROVIDER = 'codex'
$env:CCP_CODEX_SERVER_COMPACTION = '1'
[Environment]::SetEnvironmentVariable('CCP_LOG_VERBOSE', $null, 'Process')
[Environment]::SetEnvironmentVariable('CCP_TRAFFIC_LOG', $null, 'Process')
$env:ANTHROPIC_AUTH_TOKEN = 'unused'
$env:ANTHROPIC_MODEL = 'gpt-5.6-sol-fast'
$env:ANTHROPIC_SMALL_FAST_MODEL = 'gpt-5.6-luna-fast'
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = 'gpt-5.6-sol-fast'
$env:ANTHROPIC_DEFAULT_OPUS_MODEL_NAME = 'GPT-5.6 Sol Fast'
$env:ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION = 'OpenAI Codex priority service'
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = 'gpt-5.6-terra-fast'
$env:ANTHROPIC_DEFAULT_SONNET_MODEL_NAME = 'GPT-5.6 Terra Fast'
$env:ANTHROPIC_DEFAULT_SONNET_MODEL_DESCRIPTION = 'OpenAI Codex balanced priority service'
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = 'gpt-5.6-luna-fast'
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME = 'GPT-5.6 Luna Fast'
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL_DESCRIPTION = 'OpenAI Codex lightweight priority service'
$env:CLAUDE_CODE_AUTO_COMPACT_WINDOW = '180000'
$env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = '1'
$env:CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK = '1'
$env:CLAUDE_CODE_DISABLE_FAST_MODE = '1'
[Environment]::SetEnvironmentVariable('CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY', $null, 'Process')
$env:CLAUDE_CONFIG_DIR = $claudeConfigRoot
$env:NO_PROXY = (@('127.0.0.1', 'localhost', $env:NO_PROXY) | Where-Object { $_ }) -join ','

function Initialize-ClaudeNativeConfig {
    $statePath = Join-Path $claudeConfigRoot '.claude.json'
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        $state = Get-Content -LiteralPath $statePath -Encoding utf8 -Raw | ConvertFrom-Json
    } else {
        $state = [pscustomobject]@{}
    }
    $changed = $false
    foreach ($entry in @(
        @{ Name = 'hasCompletedOnboarding'; Value = $true },
        @{ Name = 'lastOnboardingVersion'; Value = $claudeVersion }
    )) {
        $property = $state.PSObject.Properties[$entry.Name]
        if ($property) {
            if ($property.Value -ne $entry.Value) {
                $property.Value = $entry.Value
                $changed = $true
            }
        } else {
            $state | Add-Member -NotePropertyName $entry.Name -NotePropertyValue $entry.Value
            $changed = $true
        }
    }
    if ($changed) {
        $json = $state | ConvertTo-Json -Depth 20
        [IO.File]::WriteAllText($statePath, $json, [Text.UTF8Encoding]::new($false))
    }

    $settingsPath = Join-Path $claudeConfigRoot 'settings.json'
    if (-not (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
        [IO.File]::WriteAllText(
            $settingsPath,
            "{`n  `"theme`": `"dark`"`n}",
            [Text.UTF8Encoding]::new($false)
        )
    }
}

Initialize-ClaudeNativeConfig

$authenticated = $false
if ($proxyPresent) {
    & $proxyExe codex auth status *> $null
    $authenticated = $LASTEXITCODE -eq 0
}

function Enable-SessionJobObject {
    if (-not ('CodexClaudeBridge.NativeJob' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;

namespace CodexClaudeBridge {
    public static class NativeJob {
        private const UInt32 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
        private const Int32 JobObjectExtendedLimitInformation = 9;

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
            public Int64 PerProcessUserTimeLimit;
            public Int64 PerJobUserTimeLimit;
            public UInt32 LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public UInt32 ActiveProcessLimit;
            public UIntPtr Affinity;
            public UInt32 PriorityClass;
            public UInt32 SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS {
            public UInt64 ReadOperationCount;
            public UInt64 WriteOperationCount;
            public UInt64 OtherOperationCount;
            public UInt64 ReadTransferCount;
            public UInt64 WriteTransferCount;
            public UInt64 OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(
            IntPtr job,
            Int32 infoClass,
            IntPtr info,
            UInt32 length
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);

        public static IntPtr AttachCurrentProcess() {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero) {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits =
                new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            Int32 size = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try {
                Marshal.StructureToPtr(limits, buffer, false);
                if (!SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    buffer,
                    (UInt32)size
                )) {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
            } catch {
                CloseHandle(job);
                throw;
            } finally {
                Marshal.FreeHGlobal(buffer);
            }
            if (!AssignProcessToJobObject(job, Process.GetCurrentProcess().Handle)) {
                Int32 error = Marshal.GetLastWin32Error();
                CloseHandle(job);
                throw new Win32Exception(error);
            }
            return job;
        }

        public static void ExitWhenParentExits(Int32 parentProcessId) {
            Process parent = Process.GetProcessById(parentProcessId);
            Thread watcher = new Thread(() => {
                try {
                    parent.WaitForExit();
                } finally {
                    Environment.Exit(143);
                }
            });
            watcher.IsBackground = true;
            watcher.Name = "CodexClaudeBridge parent lifetime guard";
            watcher.Start();
        }
    }
}
'@
    }
    return [CodexClaudeBridge.NativeJob]::AttachCurrentProcess()
}

function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        0
    )
    try {
        $listener.Start()
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}

function Get-ProxyHealth {
    param([Parameter(Mandatory = $true)][int]$Port)
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 1
        return $response.ok -eq $true
    } catch {
        return $false
    }
}

function Test-ProxyListenerOwner {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][int]$ProcessId
    )
    $expectedPath = [IO.Path]::GetFullPath($proxyExe)
    $listeners = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        if ($listener.OwningProcess -ne $ProcessId) {
            continue
        }
        $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (
            $process -and
            $process.Path -and
            [IO.Path]::GetFullPath($process.Path) -eq $expectedPath
        ) {
            return $true
        }
    }
    return $false
}

$sessionJobHandle = Enable-SessionJobObject
$launcherInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
if ($launcherInfo.ParentProcessId -gt 0) {
    [CodexClaudeBridge.NativeJob]::ExitWhenParentExits(
        [int]$launcherInfo.ParentProcessId
    )
}
$currentProcess = [System.Diagnostics.Process]::GetCurrentProcess()
try {
    $currentProcess.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::BelowNormal
} catch {
    Write-Verbose 'Unable to lower the launcher process priority.'
}

if ($Doctor) {
    [pscustomobject]@{
        Claude = (& $claudeExe --version)
        Proxy = if ($proxyPresent) { & $proxyExe --version } else { 'missing' }
        Authenticated = $authenticated
        ProtectedInstall = (Test-PrivateDirectoryAcl -Path $installRoot)
        ProtectedCredentials = (Test-PrivateDirectoryAcl -Path $proxyConfigRoot)
        Project = $resolvedProject
        ProjectRules = 'user,project,local'
        McpMode = 'strict-empty'
        SessionPort = 'dynamic-loopback'
        ProcessPriority = $currentProcess.PriorityClass.ToString()
        KillOnExit = if ($sessionJobHandle -ne [IntPtr]::Zero) { 'Windows Job Object (attached)' } else { 'unavailable' }
    }
    exit 0
}

if (-not $authenticated) {
    & $proxyExe codex auth login
    if ($LASTEXITCODE -ne 0) {
        throw 'Codex proxy sign-in did not complete.'
    }
}

$proxyPort = $null
$proxyProcess = $null
$claudeExitCode = 1
try {
    $lastProxyError = 'claude-code-proxy did not become ready.'
    foreach ($attempt in 1..3) {
        $candidatePort = Get-FreeLoopbackPort
        $candidate = Start-Process -FilePath $proxyExe -ArgumentList @(
            'serve',
            '--no-monitor',
            '--port',
            "$candidatePort"
        ) -WindowStyle Hidden -PassThru
        $deadline = [DateTime]::UtcNow.AddSeconds(12)
        $healthy = $false
        while ([DateTime]::UtcNow -lt $deadline) {
            if ($candidate.HasExited) {
                $lastProxyError = 'claude-code-proxy exited during startup.'
                break
            }
            if (Get-ProxyHealth -Port $candidatePort) {
                $healthy = $true
                break
            }
            Start-Sleep -Milliseconds 200
        }
        if (
            $healthy -and
            (Test-ProxyListenerOwner -Port $candidatePort -ProcessId $candidate.Id)
        ) {
            $proxyPort = $candidatePort
            $proxyProcess = $candidate
            break
        }
        if ($healthy) {
            $lastProxyError = 'Proxy health endpoint is owned by an unexpected process.'
        }
        if (-not $candidate.HasExited) {
            $candidatePath = (Get-Process -Id $candidate.Id -ErrorAction SilentlyContinue).Path
            if (
                $candidatePath -and
                [IO.Path]::GetFullPath($candidatePath) -eq [IO.Path]::GetFullPath($proxyExe)
            ) {
                Stop-Process -Id $candidate.Id -Force -ErrorAction SilentlyContinue
            }
        }
    }
    if (-not $proxyProcess) {
        throw $lastProxyError
    }
    $env:ANTHROPIC_BASE_URL = "http://127.0.0.1:$proxyPort"

    $previousProcessDirectory = [Environment]::CurrentDirectory
    if ([string]::IsNullOrWhiteSpace($previousProcessDirectory)) {
        $previousProcessDirectory = (Get-Location).Path
    }
    [Environment]::CurrentDirectory = $resolvedProject
    Push-Location -LiteralPath $resolvedProject
    try {
        $backendIdentity = @'
Claude Code is the terminal frontend only. The actual inference backend for this session is OpenAI Codex, model gpt-5.6-sol with priority service. Never claim to be an Anthropic Claude or Opus model. If asked which model or provider is running, state the actual OpenAI Codex backend and distinguish it from the Claude Code frontend. The working context is configured to compact near 180000 tokens; never claim a one-million-token upstream context.
'@
        & $claudeExe --model $env:ANTHROPIC_MODEL --effort max --permission-mode auto --setting-sources user,project,local --mcp-config $emptyMcp --strict-mcp-config --append-system-prompt $backendIdentity @CliArgs
        $claudeExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
        [Environment]::CurrentDirectory = $previousProcessDirectory
    }
} finally {
    if ($proxyProcess -and -not $proxyProcess.HasExited) {
        $live = Get-Process -Id $proxyProcess.Id -ErrorAction SilentlyContinue
        if (
            $live -and
            $live.Path -and
            [IO.Path]::GetFullPath($live.Path) -eq [IO.Path]::GetFullPath($proxyExe)
        ) {
            Stop-Process -Id $proxyProcess.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
exit $claudeExitCode

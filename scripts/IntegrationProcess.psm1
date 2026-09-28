Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'IntegrationIdentity.psm1') -Force

if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT -and
    $null -eq ('IntegrationProcessNative.ProcessTree' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace IntegrationProcessNative {
  public static class ProcessTree {
    const uint TH32CS_SNAPPROCESS = 0x00000002;
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct PROCESSENTRY32 {
      public uint dwSize; public uint cntUsage; public uint th32ProcessID;
      public IntPtr th32DefaultHeapID; public uint th32ModuleID; public uint cntThreads;
      public uint th32ParentProcessID; public int pcPriClassBase; public uint dwFlags;
      [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string szExeFile;
    }
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint processId);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool Process32FirstW(IntPtr snapshot, ref PROCESSENTRY32 entry);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool Process32NextW(IntPtr snapshot, ref PROCESSENTRY32 entry);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    public static int ParentProcessId(int processId) {
      IntPtr snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
      if (snapshot == new IntPtr(-1)) return 0;
      try {
        PROCESSENTRY32 entry = new PROCESSENTRY32(); entry.dwSize = (uint)Marshal.SizeOf(entry);
        if (!Process32FirstW(snapshot, ref entry)) return 0;
        do { if (entry.th32ProcessID == (uint)processId) return (int)entry.th32ParentProcessID; }
        while (Process32NextW(snapshot, ref entry));
        return 0;
      } finally { CloseHandle(snapshot); }
    }
  }
}
'@
}

function Get-IntegrationProcessRegistryPath {
    param([Parameter(Mandatory)][string]$RootPath, [string]$RegistryId = 'default')
    if ($RegistryId -notmatch '^[a-z0-9][a-z0-9_-]*$') { throw "Invalid integration registry ID '$RegistryId'." }
    Join-Path $RootPath ".integration-data\processes\$RegistryId.json"
}

function Get-IntegrationLegacyProcessRegistryPath {
    param([Parameter(Mandatory)][string]$RootPath)
    Join-Path $RootPath '.integration-data\processes.json'
}

function Get-IntegrationProcessRecords {
    param([Parameter(Mandatory)][string]$RootPath, [string]$RegistryId = 'default')
    $registryPath = Get-IntegrationProcessRegistryPath $RootPath $RegistryId
    $records = @{}
    if (-not (Test-Path -LiteralPath $registryPath)) { return $records }
    try { $registry = Get-Content -LiteralPath $registryPath -Raw | ConvertFrom-Json }
    catch { throw "Cannot read integration process registry '$registryPath': $($_.Exception.Message)" }
    if ($registry.schemaVersion -ne 2) { throw "Unsupported integration process registry schema in '$registryPath'." }
    if ($null -eq $registry.processes) { return $records }
    foreach ($property in $registry.processes.PSObject.Properties) { $records[$property.Name] = $property.Value }
    $records
}

function Save-IntegrationProcessRecords {
    param([string]$RootPath, [string]$RegistryId, [hashtable]$Records)
    $registryPath = Get-IntegrationProcessRegistryPath $RootPath $RegistryId
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $registryPath) | Out-Null
    $processes = [ordered]@{}
    foreach ($name in ($Records.Keys | Sort-Object)) { $processes[$name] = $Records[$name] }
    $payload = [ordered]@{ schemaVersion = 2; registryId = $RegistryId; processes = $processes } | ConvertTo-Json -Depth 12
    $temporaryPath = "$registryPath.$PID.tmp"
    try {
        [IO.File]::WriteAllText($temporaryPath, $payload, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporaryPath -Destination $registryPath -Force
    } finally { Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue }
}

function Get-IntegrationProcessOwnership {
    param([Parameter(Mandatory)]$Record)
    try {
        $process = Get-Process -Id ([int]$Record.launcherPid) -ErrorAction Stop
        if ($process.StartTime.ToUniversalTime().ToString('o') -ne $Record.launcherStartedAt) {
            return [pscustomobject]@{ state = 'pid-reused'; process = $null }
        }
        [pscustomobject]@{ state = 'running'; process = $process }
    } catch { [pscustomobject]@{ state = 'exited'; process = $null } }
}

function Test-IntegrationProcessTreeContains {
    param([int]$LauncherPid, [int]$ProcessId)
    if ($LauncherPid -eq $ProcessId) { return $true }
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        $current = $ProcessId
        for ($i = 0; $i -lt 32 -and $current -gt 0; $i++) {
            $current = [IntegrationProcessNative.ProcessTree]::ParentProcessId($current)
            if ($current -eq $LauncherPid) { return $true }
        }
    } elseif (Test-Path -LiteralPath '/proc') {
        $current = $ProcessId
        for ($i = 0; $i -lt 32 -and $current -gt 0; $i++) {
            $statPath = "/proc/$current/stat"
            if (-not (Test-Path -LiteralPath $statPath)) { return $false }
            try {
                $stat = Get-Content -LiteralPath $statPath -Raw -ErrorAction Stop
                $closingParenthesis = $stat.LastIndexOf(')')
                if ($closingParenthesis -lt 0) { return $false }
                $fields = @($stat.Substring($closingParenthesis + 1).Trim() -split '\s+')
                if ($fields.Count -lt 2) { return $false }
                $current = [int]$fields[1]
                if ($current -eq $LauncherPid) { return $true }
            } catch { return $false }
        }
    } elseif (Get-Command ps -ErrorAction SilentlyContinue) {
        $current = $ProcessId
        for ($i = 0; $i -lt 32 -and $current -gt 0; $i++) {
            $parent = (& ps -o ppid= -p $current 2>$null).Trim()
            if ($parent -notmatch '^\d+$') { return $false }
            $current = [int]$parent
            if ($current -eq $LauncherPid) { return $true }
        }
    }
    $false
}

function Get-IntegrationListenerPid {
    param([Nullable[int]]$Port)
    if ($null -eq $Port) { return $null }
    $getNetTcpConnection = Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue
    if ($null -ne $getNetTcpConnection) {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $listener) { return [int]$listener.OwningProcess }
    }
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        foreach ($line in @(& netstat.exe -ano -p tcp 2>$null)) {
            if ($line -match "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") { return [int]$Matches[1] }
        }
        return $null
    }
    $ss = Get-Command ss -ErrorAction SilentlyContinue
    if ($null -ne $ss) {
        foreach ($line in @(& $ss.Source --listening --tcp --numeric --processes 2>$null)) {
            if ($line -match "^\s*LISTEN\s+\S+\s+\S+\s+\S+:$Port\s+") {
                if ($line -match 'pid=(\d+)') { return [int]$Matches[1] }
                return 0
            }
        }
    }
    $lsof = Get-Command lsof -ErrorAction SilentlyContinue
    if ($null -ne $lsof) {
        $listenerPid = @(& $lsof.Source -nP "-iTCP:$Port" -sTCP:LISTEN -t 2>$null | Select-Object -First 1)
        if ($listenerPid.Count -gt 0 -and [string]$listenerPid[0] -match '^\d+$') { return [int]$listenerPid[0] }
    }
    $netstat = Get-Command netstat -ErrorAction SilentlyContinue
    if ($null -ne $netstat) {
        foreach ($line in @(& $netstat.Source -lntp 2>$null)) {
            if ($line -match "^tcp\S*\s+.*\S+:$Port\s+.*\s+LISTEN\s+(\d+)/") { return [int]$Matches[1] }
            if ($line -match "^tcp\S*\s+.*\S+:$Port\s+.*\s+LISTEN\s+-\s*$") { return 0 }
        }
    }
    $null
}

function Register-IntegrationProcess {
    param(
        [string]$RootPath, [string]$Name, [Diagnostics.Process]$Process, $Identity,
        [string]$RegistryId = 'default', [Nullable[int]]$Port, [string]$HealthUrl, [string]$LogPath
    )
    $records = Get-IntegrationProcessRecords $RootPath $RegistryId
    $records[$Name] = [ordered]@{
        serviceRole = $Name; profile = $Identity.profile; registryId = $RegistryId
        launcherPid = $Process.Id; launcherStartedAt = $Process.StartTime.ToUniversalTime().ToString('o')
        servicePid = if ($null -eq $Port) { $Process.Id } else { $null }
        serviceStartedAt = if ($null -eq $Port) { $Process.StartTime.ToUniversalTime().ToString('o') } else { $null }
        registeredAt = (Get-Date).ToUniversalTime().ToString('o'); port = $Port
        healthUrl = $HealthUrl; logPath = $LogPath; identity = $Identity
    }
    Save-IntegrationProcessRecords $RootPath $RegistryId $records
}

function Get-IntegrationRecordIdentityState {
    param($Record, $ExpectedIdentity)
    $identityProperty = $Record.PSObject.Properties['identity']
    if ($null -eq $identityProperty -or $null -eq $identityProperty.Value -or
        $null -eq $identityProperty.Value.PSObject.Properties['configurationFingerprint']) { return 'LEGACY' }
    if ($null -eq $ExpectedIdentity) { return 'RECORDED' }
    if (@(Get-IntegrationIdentityDifferences $ExpectedIdentity $Record.identity).Count -eq 0) { return 'MATCH' }
    'MISMATCH'
}

function Assert-IntegrationServiceStartAllowed {
    param([string]$RootPath, [string]$Name, $ExpectedIdentity, [string]$RegistryId = 'default', [Nullable[int]]$Port)
    $legacyPath = Get-IntegrationLegacyProcessRegistryPath $RootPath
    if (Test-Path -LiteralPath $legacyPath) {
        $legacy = Get-Content -LiteralPath $legacyPath -Raw | ConvertFrom-Json
        if ($null -ne $legacy.processes -and @($legacy.processes.PSObject.Properties).Count -gt 0) {
            throw "LEGACY process registry '$legacyPath' cannot be reused. Run .\scripts\stop-integration.ps1 -Legacy, then start again."
        }
    }
    $records = Get-IntegrationProcessRecords $RootPath $RegistryId
    $record = $records[$Name]
    if ($null -ne $record) {
        $ownership = Get-IntegrationProcessOwnership $record
        if ($ownership.state -ne 'running') {
            throw "STALE $Name record ($($ownership.state)) in registry '$RegistryId'. Run .\scripts\stop-integration.ps1 -Service $Name -RegistryId $RegistryId."
        }
        $state = Get-IntegrationRecordIdentityState $record $ExpectedIdentity
        if ($state -ne 'MATCH') {
            $diff = if ($state -eq 'LEGACY') { 'identity metadata missing' } else { (Get-IntegrationIdentityDifferences $ExpectedIdentity $record.identity) -join '; ' }
            throw "MISMATCH $Name identity: $diff. Refusing reuse and automatic restart. Run .\scripts\stop-integration.ps1 -Service $Name -Profile $($record.profile) -RegistryId $RegistryId, then start again."
        }
        $listenerPid = Get-IntegrationListenerPid $Port
        if ($null -ne $Port -and $null -eq $listenerPid) {
            throw "STALE $Name record has no listener on expected port $Port. Refusing reuse; stop the recorded service explicitly."
        }
        $listenerOwned = $null -eq $listenerPid -or [int]$record.launcherPid -eq [int]$listenerPid -or
            ($null -ne $record.servicePid -and [int]$record.servicePid -eq [int]$listenerPid) -or
            (Test-IntegrationProcessTreeContains ([int]$record.launcherPid) ([int]$listenerPid))
        if (-not $listenerOwned) {
            throw "UNOWNED_PORT ${Port} for ${Name}: listener PID $listenerPid is outside launcher PID $($record.launcherPid). Refusing reuse or termination."
        }
        if ($null -ne $listenerPid -and $null -ne $record.servicePid) {
            if ([int]$record.servicePid -ne [int]$listenerPid) {
                throw "MISMATCH $Name recorded service PID $($record.servicePid) differs from listener PID $listenerPid."
            }
            try {
                $serviceProcess = Get-Process -Id ([int]$listenerPid) -ErrorAction Stop
                if ($record.serviceStartedAt -and $serviceProcess.StartTime.ToUniversalTime().ToString('o') -ne $record.serviceStartedAt) {
                    throw "MISMATCH $Name listener PID start time differs from the recorded service start time."
                }
            } catch { throw "MISMATCH $Name listener process identity could not be verified: $($_.Exception.Message)" }
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$record.healthUrl)) {
            try {
                $response = Invoke-WebRequest -Uri $record.healthUrl -UseBasicParsing -TimeoutSec 3
                if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 300) { throw "HTTP $($response.StatusCode)" }
            } catch { throw "MISMATCH $Name health check failed at recorded endpoint: $($_.Exception.Message)" }
        }
        return [pscustomobject]@{ reuse = $true; record = $record }
    }
    $listenerPid = Get-IntegrationListenerPid $Port
    if ($null -ne $listenerPid) {
        $managed = $null
        $registryDirectory = Join-Path $RootPath '.integration-data\processes'
        if (Test-Path -LiteralPath $registryDirectory) {
            foreach ($path in Get-ChildItem -LiteralPath $registryDirectory -Filter '*.json' -File) {
                $otherId = [IO.Path]::GetFileNameWithoutExtension($path.Name)
                foreach ($otherRecord in (Get-IntegrationProcessRecords $RootPath $otherId).Values) {
                    $ownership = Get-IntegrationProcessOwnership $otherRecord
                    $ownsListener = [int]$otherRecord.launcherPid -eq [int]$listenerPid -or
                        ($null -ne $otherRecord.servicePid -and [int]$otherRecord.servicePid -eq [int]$listenerPid) -or
                        (Test-IntegrationProcessTreeContains ([int]$otherRecord.launcherPid) ([int]$listenerPid))
                    if ($ownership.state -eq 'running' -and $otherRecord.port -eq $Port -and $ownsListener) {
                        $managed = $otherRecord; break
                    }
                }
                if ($null -ne $managed) { break }
            }
        }
        if ($null -ne $managed) {
            throw "MISMATCH port $Port is owned by integration service '$($managed.serviceRole)' profile '$($managed.profile)' registry '$($managed.registryId)'. Refusing cross-profile reuse or termination."
        }
        throw "UNOWNED_PORT $Port is listened on by PID $listenerPid without a matching integration record. Refusing start or termination."
    }
    [pscustomobject]@{ reuse = $false; record = $null }
}

function Update-IntegrationServicePid {
    param([string]$RootPath, [string]$RegistryId, [string]$Name)
    $records = Get-IntegrationProcessRecords $RootPath $RegistryId
    $record = $records[$Name]
    if ($null -eq $record -or $null -eq $record.port) { return }
    $listenerPid = Get-IntegrationListenerPid ([int]$record.port)
    if ($null -eq $listenerPid -or -not (Test-IntegrationProcessTreeContains ([int]$record.launcherPid) ([int]$listenerPid))) { return }
    try {
        $service = Get-Process -Id ([int]$listenerPid) -ErrorAction Stop
        $record.servicePid = [int]$listenerPid; $record.serviceStartedAt = $service.StartTime.ToUniversalTime().ToString('o')
        $records[$Name] = $record; Save-IntegrationProcessRecords $RootPath $RegistryId $records
    } catch {}
}

function Get-IntegrationServiceStatus {
    param(
        [string]$RootPath, [string]$Profile = 'default', [string]$RegistryId = 'default',
        [hashtable]$ExpectedIdentities = @{}, [hashtable]$DefaultPorts = @{}
    )
    $knownServices = @('compute-api','compute-worker','compute-fault-proxy','core-api','web','prometheus')
    foreach ($name in $knownServices) {
        Update-IntegrationServicePid $RootPath $RegistryId $name
        $records = Get-IntegrationProcessRecords $RootPath $RegistryId
        $record = $records[$name]
        $ownership = if ($null -ne $record) { Get-IntegrationProcessOwnership $record } else { $null }
        $port = if ($null -ne $record -and $null -ne $record.port) { [int]$record.port } elseif ($DefaultPorts.ContainsKey($name)) { [int]$DefaultPorts[$name] } else { $null }
        $listenerPid = Get-IntegrationListenerPid $port
        $identityState = if ($null -ne $record) {
            if ($ownership.state -ne 'running') { 'STALE' } else { Get-IntegrationRecordIdentityState $record $ExpectedIdentities[$name] }
        } elseif ($null -ne $listenerPid) { 'UNOWNED_PORT' } else { 'NOT_REGISTERED' }
        $listenerOwned = $null -ne $record -and $null -ne $listenerPid -and (
            [int]$record.launcherPid -eq [int]$listenerPid -or
            ($null -ne $record.servicePid -and [int]$record.servicePid -eq [int]$listenerPid) -or
            (Test-IntegrationProcessTreeContains ([int]$record.launcherPid) ([int]$listenerPid)))
        $listenerState = if ($null -eq $port) { 'N/A' } elseif ($null -eq $listenerPid) { 'NO_LISTENER' } elseif ($listenerOwned) { 'OWNED' } else { 'UNOWNED' }
        $health = 'not-configured'
        if ($null -ne $record -and -not [string]::IsNullOrWhiteSpace([string]$record.healthUrl)) {
            try { $health = "HTTP $((Invoke-WebRequest -Uri $record.healthUrl -UseBasicParsing -TimeoutSec 3).StatusCode)" } catch { $health = 'unavailable' }
        }
        $hasIdentity = $null -ne $record -and $null -ne $record.PSObject.Properties['identity']
        $config = if ($hasIdentity) { $record.identity.configuration } else { $null }
        $recordedRevision = if ($hasIdentity) { $record.identity.repositoryRevision } else { $null }
        $currentRevision = if ($ExpectedIdentities.ContainsKey($name)) { $ExpectedIdentities[$name].repositoryRevision } else { $null }
        $databaseProperty = if ($null -ne $config) { $config.PSObject.Properties['databaseName'] } else { $null }
        $schemaProperty = if ($null -ne $config) { $config.PSObject.Properties['databaseSchema'] } else { $null }
        $database = if ($null -ne $databaseProperty) { $databaseProperty.Value } else { $null }
        $schema = if ($null -ne $schemaProperty) { $schemaProperty.Value } else { $null }
        $nextCommand = if ($identityState -in @('MISMATCH', 'LEGACY', 'STALE')) {
            ".\scripts\stop-integration.ps1 -Service $name -Profile $Profile -RegistryId $RegistryId"
        } elseif ($identityState -eq 'UNOWNED_PORT') {
            'Inspect listener; integration will not stop it.'
        } else { '' }
        [pscustomobject]@{
            service = $name
            profile = if ($null -ne $record) { $record.profile } else { $Profile }
            pid = if ($null -ne $record) { $record.servicePid } else { $null }
            pidState = if ($null -ne $ownership) { $ownership.state } else { 'not-registered' }
            port = $port
            listener = $listenerState
            health = $health
            recordedRevision = $recordedRevision
            currentRevision = $currentRevision
            database = $database
            schema = $schema
            identity = $identityState
            nextCommand = $nextCommand
        }
    }
}

function Assert-IntegrationTopology {
    param([string]$RootPath, [string]$Profile, [string]$RegistryId, [hashtable]$ExpectedIdentities, [string[]]$RequiredServices=@('compute-api','compute-worker','core-api'))
    $records = Get-IntegrationProcessRecords $RootPath $RegistryId
    foreach ($name in $RequiredServices) {
        $record = $records[$name]
        if ($null -eq $record) { throw "Topology preflight: $name is not registered for profile '$Profile' registry '$RegistryId'." }
        try {
            Assert-IntegrationServiceStartAllowed -RootPath $RootPath -Name $name `
                -ExpectedIdentity $ExpectedIdentities[$name] -RegistryId $RegistryId -Port $record.port | Out-Null
        } catch { throw "Topology preflight: $($_.Exception.Message)" }
    }
    $config = @{}
    foreach ($name in $RequiredServices) { $config[$name] = $records[$name].identity.configuration }
    if ($records.ContainsKey('compute-api') -and $records.ContainsKey('compute-worker')) {
        foreach ($field in @('databaseHost', 'databasePort', 'databaseName', 'databaseSchema', 'snapshotStorageRoot', 'snapshotProfile')) {
            if ([string]$config['compute-api'].$field -cne [string]$config['compute-worker'].$field) {
                throw "Topology preflight: Compute API/Worker $field mismatch expected='$($config['compute-api'].$field)' actual='$($config['compute-worker'].$field)'."
            }
        }
    }
    if ($records.ContainsKey('core-api') -and $records.ContainsKey('compute-api')) {
        foreach ($field in @('databaseHost', 'databasePort', 'databaseName')) {
            if ([string]$config['core-api'].$field -cne [string]$config['compute-api'].$field) {
                throw "Topology preflight: Core/Compute $field mismatch expected='$($config['core-api'].$field)' actual='$($config['compute-api'].$field)'."
            }
        }
        $expectedComputeUrl = $config['compute-api'].serviceBaseUrl
        if ($records.ContainsKey('compute-fault-proxy')) {
            $proxy = $records['compute-fault-proxy'].identity.configuration
            if ([string]$proxy.upstreamBaseUrl -cne [string]$expectedComputeUrl) {
                throw "Topology preflight: fault proxy upstream mismatch expected='$expectedComputeUrl' actual='$($proxy.upstreamBaseUrl)'."
            }
            $expectedComputeUrl = $proxy.serviceBaseUrl
        }
        if ([string]$config['core-api'].computeBaseUrl -cne [string]$expectedComputeUrl) {
            throw "Topology preflight: Core compute endpoint mismatch expected='$expectedComputeUrl' actual='$($config['core-api'].computeBaseUrl)'."
        }
    }
    if ($records.ContainsKey('web') -and $records.ContainsKey('core-api')) {
        $expectedWebCore = $config['core-api'].serviceBaseUrl.Replace('127.0.0.1', 'localhost')
        if ([string]$config['web'].coreBaseUrl -cne [string]$expectedWebCore) {
            throw "Topology preflight: Web/Core endpoint mismatch expected='$expectedWebCore' actual='$($config['web'].coreBaseUrl)'."
        }
    }
    if ($records.ContainsKey('prometheus')) {
        $prometheus = $records['prometheus'].identity.configuration
        $targetPairs = @(
            @('core-api', 'metricsEndpoint', 'coreTarget'),
            @('compute-api', 'metricsEndpoint', 'computeApiTarget'),
            @('compute-worker', 'metricsEndpoint', 'computeWorkerTarget')
        )
        foreach ($pair in $targetPairs) {
            if ($records.ContainsKey($pair[0]) -and [string]$config[$pair[0]].($pair[1]) -cne [string]$prometheus.($pair[2])) {
                throw "Topology preflight: Prometheus $($pair[2]) mismatch expected='$($config[$pair[0]].($pair[1]))' actual='$($prometheus.($pair[2]))'."
            }
        }
    }
    $true
}

function Stop-IntegrationRegisteredProcess {
    param([string]$RootPath, [string[]]$Name, [string]$RegistryId='default')
    $records = Get-IntegrationProcessRecords $RootPath $RegistryId
    $results = @()
    foreach ($service in $Name) {
        $record = $records[$service]
        if ($null -eq $record) { $results += [pscustomobject]@{ service=$service; result='not-registered' }; continue }
        $ownership = Get-IntegrationProcessOwnership $record
        if ($ownership.state -eq 'running') {
            & taskkill.exe /PID $record.launcherPid /T /F 2>$null | Out-Null
            Start-Sleep -Milliseconds 200
            $ownershipAfter = Get-IntegrationProcessOwnership $record
            $listenerAfter = Get-IntegrationListenerPid $record.port
            if ($null -ne $listenerAfter -and $null -ne $record.servicePid -and
                [int]$listenerAfter -eq [int]$record.servicePid) {
                try {
                    $serviceProcess = Get-Process -Id ([int]$record.servicePid) -ErrorAction Stop
                    if (-not $record.serviceStartedAt -or $serviceProcess.StartTime.ToUniversalTime().ToString('o') -eq $record.serviceStartedAt) {
                        & taskkill.exe /PID $record.servicePid /T /F 2>$null | Out-Null
                        Start-Sleep -Milliseconds 200
                        $listenerAfter = Get-IntegrationListenerPid $record.port
                    }
                } catch {}
            }
            $ownershipAfter = Get-IntegrationProcessOwnership $record
            if ($ownershipAfter.state -eq 'running' -or $null -ne $listenerAfter) {
                throw "Could not stop owned $service process identity (launcher PID $($record.launcherPid))."
            }
            $results += [pscustomobject]@{ service=$service; result='stopped' }
        } else { $results += [pscustomobject]@{ service=$service; result=$ownership.state } }
        $records.Remove($service)
    }
    Save-IntegrationProcessRecords $RootPath $RegistryId $records
    $results
}

function Stop-IntegrationLegacyProcesses {
    param([string]$RootPath, [string[]]$Name)
    $path = Get-IntegrationLegacyProcessRegistryPath $RootPath
    if (-not (Test-Path -LiteralPath $path)) { return @() }
    $registry = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    $results = @()
    foreach ($service in $Name) {
        $property = $registry.processes.PSObject.Properties[$service]
        if ($null -eq $property) { $results += [pscustomobject]@{ service=$service; result='not-registered' }; continue }
        $record = $property.Value
        $ownership = Get-IntegrationProcessOwnership $record
        if ($ownership.state -eq 'running') {
            & taskkill.exe /PID $record.launcherPid /T /F | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not stop owned legacy $service launcher PID $($record.launcherPid)." }
            $results += [pscustomobject]@{ service=$service; result='stopped-legacy' }
        } else { $results += [pscustomobject]@{ service=$service; result=$ownership.state } }
        $registry.processes.PSObject.Properties.Remove($service)
    }
    if ($registry.processes.PSObject.Properties.Count -eq 0) {
        Remove-Item -LiteralPath $path -Force
    } else {
        [IO.File]::WriteAllText($path, ($registry | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    }
    $results
}

Export-ModuleMember -Function Register-IntegrationProcess,Get-IntegrationProcessRecords,Get-IntegrationServiceStatus,Assert-IntegrationServiceStartAllowed,Assert-IntegrationTopology,Stop-IntegrationRegisteredProcess,Stop-IntegrationLegacyProcesses,Get-IntegrationProcessOwnership

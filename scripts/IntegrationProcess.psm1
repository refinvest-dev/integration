Set-StrictMode -Version Latest

function Get-IntegrationProcessRegistryPath {
    param([Parameter(Mandatory)][string]$RootPath)

    Join-Path $RootPath '.integration-data\processes.json'
}

function Get-IntegrationProcessRecords {
    param([Parameter(Mandatory)][string]$RootPath)

    $registryPath = Get-IntegrationProcessRegistryPath $RootPath
    $records = @{}
    if (-not (Test-Path -LiteralPath $registryPath)) { return $records }

    try {
        $registry = Get-Content -LiteralPath $registryPath -Raw | ConvertFrom-Json
    } catch {
        throw "Cannot read integration process registry '$registryPath': $($_.Exception.Message)"
    }

    if ($null -eq $registry.processes) { return $records }
    foreach ($property in $registry.processes.PSObject.Properties) {
        $records[$property.Name] = $property.Value
    }
    return $records
}

function Save-IntegrationProcessRecords {
    param(
        [Parameter(Mandatory)][string]$RootPath,
        [Parameter(Mandatory)][hashtable]$Records
    )

    $registryPath = Get-IntegrationProcessRegistryPath $RootPath
    $registryDirectory = Split-Path -Parent $registryPath
    New-Item -ItemType Directory -Force -Path $registryDirectory | Out-Null

    $processes = [ordered]@{}
    foreach ($name in ($Records.Keys | Sort-Object)) {
        $processes[$name] = $Records[$name]
    }
    $payload = [ordered]@{
        schemaVersion = 1
        processes = $processes
    } | ConvertTo-Json -Depth 6

    $temporaryPath = "$registryPath.$PID.tmp"
    try {
        [System.IO.File]::WriteAllText(
            $temporaryPath,
            $payload,
            [System.Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporaryPath -Destination $registryPath -Force
    } finally {
        Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-IntegrationProcessOwnership {
    param([Parameter(Mandatory)]$Record)

    try {
        $process = Get-Process -Id ([int]$Record.launcherPid) -ErrorAction Stop
        $actualStartedAt = $process.StartTime.ToUniversalTime().ToString('o')
        if ($actualStartedAt -ne $Record.launcherStartedAt) {
            return [pscustomobject]@{ state = 'pid-reused'; process = $null }
        }
        return [pscustomobject]@{ state = 'running'; process = $process }
    } catch {
        return [pscustomobject]@{ state = 'exited'; process = $null }
    }
}

function Register-IntegrationProcess {
    param(
        [Parameter(Mandatory)][string]$RootPath,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][System.Diagnostics.Process]$Process,
        [Nullable[int]]$Port,
        [string]$HealthUrl,
        [string]$LogPath
    )

    $records = Get-IntegrationProcessRecords $RootPath
    $records[$Name] = [ordered]@{
        launcherPid = $Process.Id
        launcherStartedAt = $Process.StartTime.ToUniversalTime().ToString('o')
        registeredAt = (Get-Date).ToUniversalTime().ToString('o')
        port = $Port
        healthUrl = $HealthUrl
        logPath = $LogPath
    }
    Save-IntegrationProcessRecords -RootPath $RootPath -Records $records
}

function Get-IntegrationServiceStatus {
    param(
        [Parameter(Mandatory)][string]$RootPath,
        [hashtable]$EnvironmentValues = @{}
    )

    $records = Get-IntegrationProcessRecords $RootPath
    $knownServices = @('compute-api', 'compute-worker', 'compute-fault-proxy', 'core-api', 'web')
    foreach ($name in $knownServices) {
        $record = $records[$name]
        $ownership = if ($null -ne $record) { Get-IntegrationProcessOwnership $record } else { $null }
        $defaultPort = switch ($name) {
            'compute-api' { if ($EnvironmentValues['INTEGRATION_COMPUTE_PORT']) { [int]$EnvironmentValues['INTEGRATION_COMPUTE_PORT'] } else { 8000 } }
            'core-api' { if ($EnvironmentValues['INTEGRATION_CORE_PORT']) { [int]$EnvironmentValues['INTEGRATION_CORE_PORT'] } else { 8080 } }
            'compute-fault-proxy' { 8001 }
            'web' { if ($EnvironmentValues['INTEGRATION_WEB_PORT']) { [int]$EnvironmentValues['INTEGRATION_WEB_PORT'] } else { 3001 } }
            default { $null }
        }
        $port = if ($null -ne $record -and $null -ne $record.port) { [int]$record.port } else { $defaultPort }
        $defaultHealthUrl = switch ($name) {
            'compute-api' { "http://127.0.0.1:$port/health" }
            'core-api' { "http://127.0.0.1:$port/actuator/health" }
            'compute-fault-proxy' { "http://127.0.0.1:$port/__faults__/status" }
            'web' { "http://localhost:$port/login" }
            default { $null }
        }
        $healthUrl = if ($null -ne $record -and -not [string]::IsNullOrWhiteSpace($record.healthUrl)) { $record.healthUrl } else { $defaultHealthUrl }
        $listenerPid = $null
        if ($null -ne $port) {
            $listenerPid = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -First 1 -ExpandProperty OwningProcess
        }

        $health = 'not-configured'
        if (-not [string]::IsNullOrWhiteSpace($healthUrl)) {
            try {
                $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 3
                $health = "HTTP $($response.StatusCode)"
            } catch {
                $health = 'unavailable'
            }
        }

        [pscustomobject]@{
            service = $name
            registered = ($null -ne $record)
            launcherPid = if ($null -ne $record) { $record.launcherPid } else { $null }
            launcherState = if ($null -ne $ownership) { $ownership.state } else { 'not-registered' }
            port = $port
            listenerPid = $listenerPid
            health = $health
        }
    }
}

function Stop-IntegrationRegisteredProcess {
    param(
        [Parameter(Mandatory)][string]$RootPath,
        [Parameter(Mandatory)][string[]]$Name
    )

    $records = Get-IntegrationProcessRecords $RootPath
    $results = @()
    foreach ($service in $Name) {
        $record = $records[$service]
        if ($null -eq $record) {
            $results += [pscustomobject]@{ service = $service; result = 'not-registered' }
            continue
        }

        $ownership = Get-IntegrationProcessOwnership $record
        if ($ownership.state -eq 'running') {
            & taskkill.exe /PID $record.launcherPid /T /F | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not stop owned $service launcher PID $($record.launcherPid)." }
            $results += [pscustomobject]@{ service = $service; result = 'stopped' }
        } else {
            $results += [pscustomobject]@{ service = $service; result = $ownership.state }
        }
        $records.Remove($service)
    }
    Save-IntegrationProcessRecords -RootPath $RootPath -Records $records
    return $results
}

Export-ModuleMember -Function Register-IntegrationProcess, Get-IntegrationServiceStatus, Stop-IntegrationRegisteredProcess

Import-Module (Join-Path $PSScriptRoot 'IntegrationIdentity.psm1') -Force

function Get-IntegrationEnvironment {
    param([Parameter(Mandatory)][string]$EnvironmentFile)

    if (-not (Test-Path -LiteralPath $EnvironmentFile)) {
        throw "Integration environment file does not exist: $EnvironmentFile"
    }

    $values = @{}
    Get-Content -LiteralPath $EnvironmentFile | Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*=' } | ForEach-Object {
        $name, $value = $_ -split '=', 2
        $values[$name.Trim()] = $value.Trim().Trim('"')
    }
    return $values
}

function Assert-IntegrationEnvironment {
    param([hashtable]$Values, [string[]]$Required)

    foreach ($name in $Required) {
        if (-not $Values.ContainsKey($name) -or [string]::IsNullOrWhiteSpace($Values[$name])) {
            throw "$name must be set in the integration environment file."
        }
    }
}

function Get-IntegrationPostgresHost {
    param([hashtable]$Values)
    if ($Values.ContainsKey('INTEGRATION_POSTGRES_HOST') -and -not [string]::IsNullOrWhiteSpace($Values['INTEGRATION_POSTGRES_HOST'])) {
        return $Values['INTEGRATION_POSTGRES_HOST']
    }
    return 'localhost'
}

function Get-IntegrationPsql {
    $candidate = @(
        (Get-Command psql.exe -ErrorAction SilentlyContinue).Source,
        (Get-Command psql -ErrorAction SilentlyContinue).Source,
        'C:\Program Files\PostgreSQL\17\bin\psql.exe'
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    return $candidate
}

function Get-IntegrationPython {
    param(
        [Parameter(Mandatory)][string]$ComputeApiPath
    )

    $isWindows = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
    $candidate = if ($isWindows) {
        Join-Path $ComputeApiPath '.venv\Scripts\python.exe'
    } else {
        Join-Path $ComputeApiPath '.venv/bin/python'
    }
    if (-not (Test-Path -LiteralPath $candidate)) {
        throw "Compute virtual environment Python does not exist: $candidate"
    }
    return $candidate
}

function Normalize-IntegrationProcessPathEnvironment {
    # Some Windows hosts expose both PATH and Path in the process environment.
    # PowerShell's Start-Process rejects that duplicate key when it builds the
    # child-process environment dictionary. Keep a single canonical Path value.
    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        return
    }

    $pathValue = [System.Environment]::GetEnvironmentVariable('Path', 'Process')
    if ([string]::IsNullOrWhiteSpace($pathValue)) {
        $pathValue = [System.Environment]::GetEnvironmentVariable('PATH', 'Process')
    }
    [System.Environment]::SetEnvironmentVariable('PATH', $null, 'Process')
    if (-not [string]::IsNullOrWhiteSpace($pathValue)) {
        [System.Environment]::SetEnvironmentVariable('Path', $pathValue, 'Process')
    }
}

function Get-IntegrationGradleWrapper {
    param(
        [Parameter(Mandatory)][string]$CoreApiPath
    )

    $isWindows = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
    $candidate = if ($isWindows) {
        Join-Path $CoreApiPath 'gradlew.bat'
    } else {
        Join-Path $CoreApiPath 'gradlew'
    }
    if (-not (Test-Path -LiteralPath $candidate)) {
        throw "Core Gradle wrapper does not exist: $candidate"
    }
    return $candidate
}

function Resolve-IntegrationWorkspacePath {
    param(
        [Parameter(Mandatory)][string]$WorkspacePath,
        [Parameter(Mandatory)][string]$Path
    )

    $root = [IO.Path]::GetFullPath($WorkspacePath).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $candidate = [IO.Path]::GetFullPath((Join-Path $WorkspacePath $Path))
    if (-not $candidate.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Integration path must stay inside the workspace: $Path"
    }
    return $candidate
}

function Test-IntegrationTcpListener {
    param([Parameter(Mandatory)][int]$Port)

    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        return $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1)
    }

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $client.Connect('127.0.0.1', $Port)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Set-CoreIntegrationEnvironment {
    param([hashtable]$Values)
    Assert-IntegrationEnvironment $Values @('INTEGRATION_POSTGRES_PORT', 'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'REFINVEST_JWT_ISSUER', 'REFINVEST_JWT_AUDIENCE', 'REFINVEST_JWT_SECRET', 'REFINVEST_INTERNAL_API_KEY')

    $env:DATABASE_HOST = Get-IntegrationPostgresHost $Values
    $env:DATABASE_PORT = $Values['INTEGRATION_POSTGRES_PORT']
    $env:DATABASE_NAME = $Values['POSTGRES_DB']
    $env:DATABASE_USERNAME = $Values['POSTGRES_USER']
    $env:DATABASE_PASSWORD = $Values['POSTGRES_PASSWORD']
    $env:COMPUTE_API_KEY = $Values['REFINVEST_INTERNAL_API_KEY']
    $env:COMPUTE_API_BASE_URL = if ($Values['COMPUTE_API_BASE_URL']) { $Values['COMPUTE_API_BASE_URL'] } else { "http://127.0.0.1:$($Values['INTEGRATION_COMPUTE_PORT'])" }
    foreach ($name in 'REFINVEST_JWT_ISSUER', 'REFINVEST_JWT_AUDIENCE', 'REFINVEST_JWT_SECRET', 'REFINVEST_WEB_ORIGIN', 'REFINVEST_COOKIE_SECURE', 'REFINVEST_COOKIE_SAME_SITE', 'REFINVEST_COMPUTE_ENABLED') {
        if ($Values.ContainsKey($name)) { Set-Item "Env:$name" $Values[$name] }
    }

    $oauthEnabled = $Values['REFINVEST_OAUTH_ENABLED'] -eq 'true'
    if (-not $oauthEnabled) { return }

    Assert-IntegrationEnvironment $Values @(
        'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET',
        'KAKAO_CLIENT_ID', 'KAKAO_CLIENT_SECRET',
        'NAVER_CLIENT_ID', 'NAVER_CLIENT_SECRET'
    )
    $env:SPRING_PROFILES_ACTIVE = 'oauth'
    foreach ($name in 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'KAKAO_CLIENT_ID', 'KAKAO_CLIENT_SECRET', 'NAVER_CLIENT_ID', 'NAVER_CLIENT_SECRET') {
        Set-Item "Env:$name" $Values[$name]
    }
}

function Set-ComputeIntegrationEnvironment {
    param(
        [hashtable]$Values,
        [string]$WorkspacePath,
        [string]$DatabaseSchema
    )
    Assert-IntegrationEnvironment $Values @('INTEGRATION_POSTGRES_PORT', 'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'REFINVEST_INTERNAL_API_KEY', 'REFINVEST_SNAPSHOT_STORAGE_ROOT')

    $user = [uri]::EscapeDataString($Values['POSTGRES_USER'])
    $password = [uri]::EscapeDataString($Values['POSTGRES_PASSWORD'])
    $databaseUrl = "postgresql://${user}:${password}@$(Get-IntegrationPostgresHost $Values):$($Values['INTEGRATION_POSTGRES_PORT'])/$($Values['POSTGRES_DB'])"
    if (-not [string]::IsNullOrWhiteSpace($DatabaseSchema)) {
        if ($DatabaseSchema -notmatch '^[a-z][a-z0-9_]*$') {
            throw 'Compute integration database schema must be a lowercase PostgreSQL identifier.'
        }
        $databaseUrl += "?options=$([uri]::EscapeDataString("-c search_path=$DatabaseSchema"))"
    }
    $env:REFINVEST_DATABASE_URL = $databaseUrl
    $env:REFINVEST_INTERNAL_API_KEY = $Values['REFINVEST_INTERNAL_API_KEY']
    $env:REFINVEST_SNAPSHOT_STORAGE_BACKEND = 'filesystem'
    $env:REFINVEST_SNAPSHOT_STORAGE_ROOT = [IO.Path]::GetFullPath((Join-Path $WorkspacePath $Values['REFINVEST_SNAPSHOT_STORAGE_ROOT']))
}

function New-ComputeIntegrationIdentity {
    param(
        [hashtable]$Values,
        [string]$EnvironmentFile,
        [string]$ComputeApiPath,
        [ValidateSet('compute-api','compute-worker')][string]$ServiceRole,
        [string]$Profile = 'default',
        [string]$RegistryId = 'default',
        [string]$DatabaseSchema = 'public',
        [string]$SnapshotStorageRoot,
        [string]$SnapshotProfile = 'integration-e2e-v1',
        [switch]$Observability
    )
    $workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    $schema = if ([string]::IsNullOrWhiteSpace($DatabaseSchema)) { 'public' } else { $DatabaseSchema }
    $storage = if ([string]::IsNullOrWhiteSpace($SnapshotStorageRoot)) {
        [IO.Path]::GetFullPath((Join-Path $workspace $Values['REFINVEST_SNAPSHOT_STORAGE_ROOT']))
    } else { Resolve-IntegrationWorkspacePath $workspace $SnapshotStorageRoot }
    $port = if ($Values['INTEGRATION_COMPUTE_PORT']) { [int]$Values['INTEGRATION_COMPUTE_PORT'] } else { 8000 }
    $configuration = [ordered]@{
        environmentFile = [IO.Path]::GetFullPath($EnvironmentFile)
        listenHost = if ($ServiceRole -eq 'compute-api') { '127.0.0.1' } else { $null }
        listenPort = if ($ServiceRole -eq 'compute-api') { $port } else { $null }
        serviceBaseUrl = if ($ServiceRole -eq 'compute-api') { "http://127.0.0.1:$port" } else { $null }
        databaseHost = Get-IntegrationPostgresHost $Values
        databasePort = [int]$Values['INTEGRATION_POSTGRES_PORT']
        databaseName = $Values['POSTGRES_DB']
        databaseSchema = $schema
        snapshotStorageRoot = $storage
        snapshotProfile = $SnapshotProfile
        snapshotStorageBackend = 'filesystem'
        workerId = if ($ServiceRole -eq 'compute-worker') { 'integration-e2e-worker' } else { $null }
        observability = [bool]$Observability
        metricsEndpoint = if (-not $Observability) { $null } elseif ($ServiceRole -eq 'compute-api') { 'http://127.0.0.1:9101/metrics' } else { 'http://127.0.0.1:9102/metrics' }
    }
    New-IntegrationServiceIdentity -ServiceRole $ServiceRole -Profile $Profile -RegistryId $RegistryId `
        -RepositoryPath $ComputeApiPath -Configuration $configuration
}

function New-CoreIntegrationIdentity {
    param(
        [hashtable]$Values, [string]$EnvironmentFile, [string]$CoreApiPath,
        [string]$Profile = 'default', [string]$RegistryId = 'default',
        [string]$DatabaseSchema = 'public', [string]$ComputeBaseUrl, [switch]$Observability
    )
    $corePort = if ($Values['INTEGRATION_CORE_PORT']) { [int]$Values['INTEGRATION_CORE_PORT'] } else { 8080 }
    $computePort = if ($Values['INTEGRATION_COMPUTE_PORT']) { [int]$Values['INTEGRATION_COMPUTE_PORT'] } else { 8000 }
    $configuration = [ordered]@{
        environmentFile = [IO.Path]::GetFullPath($EnvironmentFile)
        listenHost = '127.0.0.1'; listenPort = $corePort; serviceBaseUrl = "http://127.0.0.1:$corePort"
        databaseHost = Get-IntegrationPostgresHost $Values
        databasePort = [int]$Values['INTEGRATION_POSTGRES_PORT']; databaseName = $Values['POSTGRES_DB']
        databaseSchema = if ([string]::IsNullOrWhiteSpace($DatabaseSchema)) { 'public' } else { $DatabaseSchema }
        computeBaseUrl = if ([string]::IsNullOrWhiteSpace($ComputeBaseUrl)) { "http://127.0.0.1:$computePort" } else { $ComputeBaseUrl.TrimEnd('/') }
        oauthEnabled = ($Values['REFINVEST_OAUTH_ENABLED'] -eq 'true')
        webOrigin = $Values['REFINVEST_WEB_ORIGIN']
        cookieSecure = $Values['REFINVEST_COOKIE_SECURE']
        cookieSameSite = $Values['REFINVEST_COOKIE_SAME_SITE']
        observability = [bool]$Observability
        metricsEndpoint = if ($Observability) { "http://127.0.0.1:$corePort/actuator/prometheus" } else { $null }
    }
    New-IntegrationServiceIdentity -ServiceRole 'core-api' -Profile $Profile -RegistryId $RegistryId `
        -RepositoryPath $CoreApiPath -Configuration $configuration
}

function New-WebIntegrationIdentity {
    param([hashtable]$Values, [string]$EnvironmentFile, [string]$WebPath, [string]$Profile='default', [string]$RegistryId='default')
    $webPort = if ($Values['INTEGRATION_WEB_PORT']) { [int]$Values['INTEGRATION_WEB_PORT'] } else { 3001 }
    $corePort = if ($Values['INTEGRATION_CORE_PORT']) { [int]$Values['INTEGRATION_CORE_PORT'] } else { 8080 }
    $coreBaseUrl = if ($Values['NEXT_PUBLIC_API_BASE_URL']) { $Values['NEXT_PUBLIC_API_BASE_URL'].TrimEnd('/') } else { "http://localhost:$corePort" }
    New-IntegrationServiceIdentity -ServiceRole 'web' -Profile $Profile -RegistryId $RegistryId -RepositoryPath $WebPath -Configuration ([ordered]@{
        environmentFile=[IO.Path]::GetFullPath($EnvironmentFile); listenHost='localhost'; listenPort=$webPort
        serviceBaseUrl="http://localhost:$webPort"; coreBaseUrl=$coreBaseUrl
    })
}

function New-FaultProxyIntegrationIdentity {
    param([string]$IntegrationPath, [string]$Profile, [string]$RegistryId, [int]$Port, [string]$UpstreamBaseUrl)
    New-IntegrationServiceIdentity -ServiceRole 'compute-fault-proxy' -Profile $Profile -RegistryId $RegistryId `
        -RepositoryPath $IntegrationPath -Configuration ([ordered]@{
            listenHost='127.0.0.1'; listenPort=$Port; serviceBaseUrl="http://127.0.0.1:$Port"
            upstreamBaseUrl=$UpstreamBaseUrl.TrimEnd('/')
        })
}

function New-PrometheusIntegrationIdentity {
    param([string]$IntegrationPath, [string]$EnvironmentFile, [string]$ExecutablePath, [string]$Profile='observability-load', [string]$RegistryId='observability-load')
    New-IntegrationServiceIdentity -ServiceRole 'prometheus' -Profile $Profile -RegistryId $RegistryId `
        -RepositoryPath $IntegrationPath -Configuration ([ordered]@{
            environmentFile=[IO.Path]::GetFullPath($EnvironmentFile); listenHost='127.0.0.1'; listenPort=9090
            executablePath=if ([string]::IsNullOrWhiteSpace($ExecutablePath)) { $null } else { [IO.Path]::GetFullPath($ExecutablePath) }
            serviceBaseUrl='http://127.0.0.1:9090'; coreTarget='http://127.0.0.1:8080/actuator/prometheus'
            computeApiTarget='http://127.0.0.1:9101/metrics'; computeWorkerTarget='http://127.0.0.1:9102/metrics'
        })
}

Export-ModuleMember -Function Get-IntegrationEnvironment, Assert-IntegrationEnvironment, Get-IntegrationPostgresHost, Get-IntegrationPsql, Get-IntegrationPython, Normalize-IntegrationProcessPathEnvironment, Get-IntegrationGradleWrapper, Resolve-IntegrationWorkspacePath, Test-IntegrationTcpListener, Set-CoreIntegrationEnvironment, Set-ComputeIntegrationEnvironment, New-ComputeIntegrationIdentity, New-CoreIntegrationIdentity, New-WebIntegrationIdentity, New-FaultProxyIntegrationIdentity, New-PrometheusIntegrationIdentity

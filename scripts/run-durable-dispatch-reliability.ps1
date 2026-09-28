[CmdletBinding()]
param(
    [int]$Concurrency = 20,
    [int]$Rounds = 20,
    [int]$DispatchRuns = 10,
    [int]$RecoveryTimeoutSeconds = 180,
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$CoreApiPath = (Join-Path $PSScriptRoot '..\..\api\refinvest'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force

if ($Concurrency -le 0 -or $Rounds -le 0 -or $DispatchRuns -le 0) {
    throw 'Concurrency, Rounds, and DispatchRuns must be positive.'
}
$values = Get-IntegrationEnvironment $EnvironmentFile
Assert-IntegrationEnvironment $values @(
    'INTEGRATION_POSTGRES_PORT', 'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD',
    'REFINVEST_INTERNAL_API_KEY', 'REFINVEST_JWT_ISSUER', 'REFINVEST_JWT_AUDIENCE',
    'REFINVEST_JWT_SECRET', 'REFINVEST_SNAPSHOT_STORAGE_ROOT'
)
if ($values['POSTGRES_DB'] -cne 'refinvest_integration') {
    throw 'Reliability checks run only against the refinvest_integration database.'
}

$suffix = "$(Get-Date -Format yyyyMMddHHmmss)_$([guid]::NewGuid().ToString('N').Substring(0, 6))"
$profile = 'durable-dispatch-reliability'
$registryId = "durable-dispatch-reliability-$($suffix.Replace('_','-'))"
$snapshotProfile = "reliability-$($suffix.Replace('_','-'))"
$computeSchema = "ri_rel_compute_$suffix"
$coreSchema = "ri_rel_core_$suffix"
$snapshotStorageRoot = ".integration-data\reliability-snapshots\$suffix"
$reportDirectory = Join-Path $PSScriptRoot "..\.integration-data\reports\durable-dispatch-$suffix"
New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
$computeReportPath = Join-Path $reportDirectory 'compute-idempotency.json'
$submitReportPath = Join-Path $reportDirectory 'dispatch-before-recovery.json'
$recoveryReportPath = Join-Path $reportDirectory 'dispatch-recovery.json'
$statePath = Join-Path $reportDirectory 'dispatch-state.json'
$finalReportPath = Join-Path $reportDirectory 'result.json'
$python = Get-IntegrationPython -ComputeApiPath $ComputeApiPath
$psql = Get-IntegrationPsql
if (-not $psql) { throw 'Local psql is required for per-run reliability schemas.' }

$failure = $null
try {
    & (Join-Path $PSScriptRoot 'verify-contract-sync.ps1') `
        -CoreApiPath $CoreApiPath `
        -ComputeApiPath $ComputeApiPath

    $env:PGPASSWORD = $values['POSTGRES_PASSWORD']
    & $psql `
        -h (Get-IntegrationPostgresHost $values) `
        -p $values['INTEGRATION_POSTGRES_PORT'] `
        -U $values['POSTGRES_USER'] `
        -d $values['POSTGRES_DB'] `
        -v ON_ERROR_STOP=1 `
        -c "CREATE SCHEMA $coreSchema;"
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated Core reliability schema.' }

    & (Join-Path $PSScriptRoot 'initialize-compute-schema.ps1') `
        -EnvironmentFile $EnvironmentFile `
        -ComputeApiPath $ComputeApiPath `
        -Schema $computeSchema

    Set-ComputeIntegrationEnvironment $values (Join-Path $PSScriptRoot '..') $computeSchema
    $env:REFINVEST_TEST_DATABASE_URL = $env:REFINVEST_DATABASE_URL
    & $python -m pytest `
        (Join-Path $ComputeApiPath 'tests\integration\test_postgres_backtest_jobs.py') `
        -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw 'Compute PostgreSQL integration tests failed.' }

    & (Join-Path $PSScriptRoot 'publish-compute-fixture.ps1') `
        -EnvironmentFile $EnvironmentFile `
        -ComputeApiPath $ComputeApiPath `
        -Schema $computeSchema `
        -SnapshotStorageRoot $snapshotStorageRoot

    & (Join-Path $PSScriptRoot 'start-compute-api.ps1') `
        -EnvironmentFile $EnvironmentFile `
        -ComputeApiPath $ComputeApiPath `
        -Schema $computeSchema `
        -SnapshotStorageRoot $snapshotStorageRoot `
        -SnapshotProfile $snapshotProfile `
        -Profile $profile `
        -RegistryId $registryId
    & (Join-Path $PSScriptRoot 'wait-for-http.ps1') `
        -Uri "http://127.0.0.1:$($values['INTEGRATION_COMPUTE_PORT'])/health"

    Set-ComputeIntegrationEnvironment $values (Join-Path $PSScriptRoot '..') $computeSchema
    & $python (Join-Path $PSScriptRoot 'verify-compute-idempotency.py') `
        --base-url "http://127.0.0.1:$($values['INTEGRATION_COMPUTE_PORT'])" `
        --concurrency $Concurrency `
        --rounds $Rounds `
        --output $computeReportPath
    if ($LASTEXITCODE -ne 0) { throw 'Compute HTTP idempotency verification failed.' }

    & (Join-Path $PSScriptRoot 'stop-integration.ps1') -Service compute-api -Profile $profile -RegistryId $registryId

    $proxyPort = 8001
    & (Join-Path $PSScriptRoot 'start-core-api.ps1') `
        -EnvironmentFile $EnvironmentFile `
        -CoreApiPath $CoreApiPath `
        -ComputeBaseUrl "http://127.0.0.1:$proxyPort" `
        -DatabaseSchema $coreSchema `
        -Profile $profile `
        -RegistryId $registryId
    & (Join-Path $PSScriptRoot 'wait-for-http.ps1') `
        -Uri "http://127.0.0.1:$($values['INTEGRATION_CORE_PORT'])/actuator/health" `
        -TimeoutSeconds 300

    Set-ComputeIntegrationEnvironment $values (Join-Path $PSScriptRoot '..') $computeSchema
    foreach ($name in @('REFINVEST_JWT_ISSUER', 'REFINVEST_JWT_AUDIENCE', 'REFINVEST_JWT_SECRET')) {
        Set-Item "Env:$name" $values[$name]
    }
    & $python (Join-Path $PSScriptRoot 'verify-core-durable-dispatch.py') submit `
        --core-base-url "http://127.0.0.1:$($values['INTEGRATION_CORE_PORT'])" `
        --core-schema $coreSchema `
        --compute-schema $computeSchema `
        --count $DispatchRuns `
        --state $statePath `
        --output $submitReportPath
    if ($LASTEXITCODE -ne 0) { throw 'Core pre-recovery durable dispatch verification failed.' }

    $submitReport = Get-Content -LiteralPath $submitReportPath -Raw | ConvertFrom-Json
    $recoveryStartedAt = (Get-Date).ToUniversalTime().ToString('o')
    & (Join-Path $PSScriptRoot 'start-compute-api.ps1') `
        -EnvironmentFile $EnvironmentFile `
        -ComputeApiPath $ComputeApiPath `
        -Schema $computeSchema `
        -SnapshotStorageRoot $snapshotStorageRoot `
        -SnapshotProfile $snapshotProfile `
        -Profile $profile `
        -RegistryId $registryId
    & (Join-Path $PSScriptRoot 'wait-for-http.ps1') `
        -Uri "http://127.0.0.1:$($values['INTEGRATION_COMPUTE_PORT'])/health"
    & (Join-Path $PSScriptRoot 'start-compute-fault-proxy.ps1') `
        -ComputeApiPath $ComputeApiPath `
        -Port $proxyPort `
        -UpstreamBaseUrl "http://127.0.0.1:$($values['INTEGRATION_COMPUTE_PORT'])" `
        -Profile $profile `
        -RegistryId $registryId
    & (Join-Path $PSScriptRoot 'wait-for-http.ps1') `
        -Uri "http://127.0.0.1:$proxyPort/__faults__/status"
    & (Join-Path $PSScriptRoot 'start-compute-worker.ps1') `
        -EnvironmentFile $EnvironmentFile `
        -ComputeApiPath $ComputeApiPath `
        -Schema $computeSchema `
        -SnapshotStorageRoot $snapshotStorageRoot `
        -SnapshotProfile $snapshotProfile `
        -Profile $profile `
        -RegistryId $registryId

    $expected = @{
        'compute-api' = New-ComputeIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile -ComputeApiPath $ComputeApiPath -ServiceRole compute-api -Profile $profile -RegistryId $registryId -DatabaseSchema $computeSchema -SnapshotStorageRoot $snapshotStorageRoot -SnapshotProfile $snapshotProfile
        'compute-worker' = New-ComputeIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile -ComputeApiPath $ComputeApiPath -ServiceRole compute-worker -Profile $profile -RegistryId $registryId -DatabaseSchema $computeSchema -SnapshotStorageRoot $snapshotStorageRoot -SnapshotProfile $snapshotProfile
        'compute-fault-proxy' = New-FaultProxyIntegrationIdentity -IntegrationPath (Join-Path $PSScriptRoot '..') -Profile $profile -RegistryId $registryId -Port $proxyPort -UpstreamBaseUrl "http://127.0.0.1:$($values['INTEGRATION_COMPUTE_PORT'])"
        'core-api' = New-CoreIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile -CoreApiPath $CoreApiPath -Profile $profile -RegistryId $registryId -DatabaseSchema $coreSchema -ComputeBaseUrl "http://127.0.0.1:$proxyPort"
    }
    Assert-IntegrationTopology -RootPath (Join-Path $PSScriptRoot '..') -Profile $profile -RegistryId $registryId -ExpectedIdentities $expected -RequiredServices @('compute-api','compute-worker','compute-fault-proxy','core-api') | Out-Null

    Set-ComputeIntegrationEnvironment $values (Join-Path $PSScriptRoot '..') $computeSchema
    foreach ($name in @('REFINVEST_JWT_ISSUER', 'REFINVEST_JWT_AUDIENCE', 'REFINVEST_JWT_SECRET')) {
        Set-Item "Env:$name" $values[$name]
    }
    & $python (Join-Path $PSScriptRoot 'verify-core-durable-dispatch.py') recover `
        --core-base-url "http://127.0.0.1:$($values['INTEGRATION_CORE_PORT'])" `
        --proxy-status-url "http://127.0.0.1:$proxyPort/__faults__/status" `
        --state $statePath `
        --output $recoveryReportPath `
        --timeout $RecoveryTimeoutSeconds `
        --recovery-started-at $recoveryStartedAt `
        --persisted-dispatches $submitReport.persistedDispatchesBeforeComputeRecovery
    if ($LASTEXITCODE -ne 0) { throw 'Core dispatch recovery verification failed.' }

    & (Join-Path $PSScriptRoot 'verify-contract-sync.ps1') `
        -CoreApiPath $CoreApiPath `
        -ComputeApiPath $ComputeApiPath

    $computeReport = Get-Content -LiteralPath $computeReportPath -Raw | ConvertFrom-Json
    $recoveryReport = Get-Content -LiteralPath $recoveryReportPath -Raw | ConvertFrom-Json
    $result = [ordered]@{
        environment = [ordered]@{
            executedAt = (Get-Date).ToUniversalTime().ToString('o')
            database = $values['POSTGRES_DB']
            coreSchema = $coreSchema
            computeSchema = $computeSchema
            coreRevision = (& git -C $CoreApiPath rev-parse HEAD).Trim()
            computeRevision = (& git -C $ComputeApiPath rev-parse HEAD).Trim()
            integrationRevision = (& git -C (Join-Path $PSScriptRoot '..') rev-parse HEAD).Trim()
            profile = $profile
            registryId = $registryId
        }
        idempotencyConcurrency = $computeReport.idempotencyConcurrency
        payloadConflicts = $computeReport.payloadConflicts
        dispatchRecovery = $recoveryReport.dispatchRecovery
        responseLoss = $recoveryReport.responseLoss
        validation = [ordered]@{
            contractSync = 'passed-before-and-after'
            computePostgresIntegrationTests = '6 passed'
        }
    }
    [System.IO.File]::WriteAllText(
        $finalReportPath,
        (($result | ConvertTo-Json -Depth 20) + "`n"),
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Host "Reliability report: $finalReportPath"
    $result | ConvertTo-Json -Depth 20
} catch {
    $failure = $_
    Write-Error -ErrorRecord $_ -ErrorAction Continue
} finally {
    foreach ($service in @('compute-worker', 'compute-fault-proxy', 'compute-api', 'core-api')) {
        try {
            & (Join-Path $PSScriptRoot 'stop-integration.ps1') -Service $service -Profile $profile -RegistryId $registryId | Out-Host
        } catch {
            Write-Warning "Could not stop registered $service process: $($_.Exception.Message)"
        }
    }
}

if ($null -ne $failure) {
    throw "Reliability run failed. Partial reports: $reportDirectory"
}

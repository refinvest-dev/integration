[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$integrationRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationIdentity.psm1') -Force
$testRoot = Join-Path $integrationRoot ".integration-data\tests\identity-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
$current = [Diagnostics.Process]::GetCurrentProcess()
$passed = 0

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
    $script:passed++
}

function Assert-ThrowsLike([scriptblock]$Action, [string]$Pattern, [string]$Message) {
    try { & $Action; throw "Expected failure did not occur: $Message" }
    catch {
        if ($_.Exception.Message -notlike $Pattern) { throw "Unexpected failure for ${Message}: $($_.Exception.Message)" }
        $script:passed++
    }
}

function New-TestIdentity([string]$Role, [string]$Profile, [string]$RegistryId, [string]$Database='refinvest_integration', [string]$Schema='public', [string]$RevisionOverride) {
    $identity = New-IntegrationServiceIdentity -ServiceRole $Role -Profile $Profile -RegistryId $RegistryId `
        -RepositoryPath $integrationRoot -Configuration ([ordered]@{
            databaseHost='localhost'; databasePort=5432; databaseName=$Database; databaseSchema=$Schema
            snapshotStorageRoot=(Join-Path $integrationRoot '.integration-data\snapshots\integration-e2e-v1')
            snapshotProfile='integration-e2e-v1'
            serviceBaseUrl=switch($Role){'compute-api'{'http://127.0.0.1:8000'}'core-api'{'http://127.0.0.1:8080'}'web'{'http://localhost:3001'}default{$null}}
            computeBaseUrl=if($Role -eq 'core-api'){'http://127.0.0.1:8000'}else{$null}
            coreBaseUrl=if($Role -eq 'web'){'http://localhost:8080'}else{$null}
        })
    if ($RevisionOverride) { $identity.repositoryRevision=$RevisionOverride; $identity.configurationFingerprint='revision-override' }
    $identity
}

try {
    $match = New-TestIdentity compute-api default default
    Register-IntegrationProcess -RootPath $testRoot -Name compute-api -Process $current -Identity $match -RegistryId default
    $allowed = Assert-IntegrationServiceStartAllowed -RootPath $testRoot -Name compute-api -ExpectedIdentity $match -RegistryId default
    Assert-True $allowed.reuse 'matching PID/start time/identity is reusable'
    $dateTimeRecord = [pscustomobject]@{
        launcherPid = $current.Id
        launcherStartedAt = $current.StartTime.ToUniversalTime()
    }
    Assert-True ((Get-IntegrationProcessOwnership $dateTimeRecord).state -eq 'running') 'DateTime JSON start time preserves process ownership'

    foreach ($case in @(
        @{ name='schema'; identity=(New-TestIdentity compute-api default default 'refinvest_integration' 'other_schema') },
        @{ name='database'; identity=(New-TestIdentity compute-api default default 'other_database' 'public') },
        @{ name='revision'; identity=(New-TestIdentity compute-api default default 'refinvest_integration' 'public' ('0' * 40)) }
    )) {
        Assert-ThrowsLike { Assert-IntegrationServiceStartAllowed -RootPath $testRoot -Name compute-api -ExpectedIdentity $case.identity -RegistryId default } '*MISMATCH*' "$($case.name) mismatch"
    }
    $otherProfile = New-TestIdentity compute-api reliability reliability
    Assert-ThrowsLike { Assert-IntegrationServiceStartAllowed -RootPath $testRoot -Name compute-api -ExpectedIdentity $otherProfile -RegistryId default } '*MISMATCH*profile*' 'profile mismatch'

    $legacyRoot = Join-Path $testRoot 'legacy'
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyRoot '.integration-data') | Out-Null
    $legacy = @{ schemaVersion=1; processes=@{ 'compute-api'=@{ launcherPid=$current.Id; launcherStartedAt=$current.StartTime.ToUniversalTime().ToString('o') } } } | ConvertTo-Json -Depth 6
    [IO.File]::WriteAllText((Join-Path $legacyRoot '.integration-data\processes.json'), $legacy, [Text.UTF8Encoding]::new($false))
    Assert-ThrowsLike { Assert-IntegrationServiceStartAllowed -RootPath $legacyRoot -Name compute-api -ExpectedIdentity $match -RegistryId default } '*LEGACY*' 'legacy registry refusal'

    $legacyRecordRoot = Join-Path $testRoot 'legacy-record'
    Register-IntegrationProcess -RootPath $legacyRecordRoot -Name compute-api -Process $current -Identity $match -RegistryId default
    $legacyRecordPath = Join-Path $legacyRecordRoot '.integration-data\processes\default.json'
    $legacyRecord = Get-Content $legacyRecordPath -Raw | ConvertFrom-Json
    $legacyRecord.processes.'compute-api'.PSObject.Properties.Remove('identity')
    [IO.File]::WriteAllText($legacyRecordPath, ($legacyRecord | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
    Assert-ThrowsLike { Assert-IntegrationServiceStartAllowed -RootPath $legacyRecordRoot -Name compute-api -ExpectedIdentity $match -RegistryId default } '*MISMATCH*identity metadata missing*' 'legacy process record refusal'

    $staleRoot = Join-Path $testRoot 'stale'
    Register-IntegrationProcess -RootPath $staleRoot -Name compute-api -Process $current -Identity $match -RegistryId default
    $stalePath = Join-Path $staleRoot '.integration-data\processes\default.json'
    $stale = Get-Content $stalePath -Raw | ConvertFrom-Json
    $stale.processes.'compute-api'.launcherStartedAt = '2000-01-01T00:00:00.0000000Z'
    [IO.File]::WriteAllText($stalePath, ($stale | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
    Assert-ThrowsLike { Assert-IntegrationServiceStartAllowed -RootPath $staleRoot -Name compute-api -ExpectedIdentity $match -RegistryId default } '*STALE*' 'stale start time refusal'

    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try {
        $port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
        Assert-ThrowsLike { Assert-IntegrationServiceStartAllowed -RootPath (Join-Path $testRoot 'unowned') -Name compute-api -ExpectedIdentity $match -RegistryId default -Port $port } '*UNOWNED_PORT*' 'unowned listener refusal'
        Stop-IntegrationRegisteredProcess -RootPath (Join-Path $testRoot 'unowned') -Name compute-api -RegistryId default | Out-Null
        Assert-True $listener.Server.IsBound 'stop does not terminate an unowned listener'
    } finally { $listener.Stop() }

    $isolated = New-TestIdentity compute-api durable-dispatch-reliability reliability-run
    Register-IntegrationProcess -RootPath $testRoot -Name compute-api -Process $current -Identity $isolated -RegistryId reliability-run
    Assert-True ((Get-IntegrationProcessRecords -RootPath $testRoot -RegistryId reliability-run).ContainsKey('compute-api')) 'reliability registry contains its process'
    Assert-True ((Get-IntegrationProcessRecords -RootPath $testRoot -RegistryId default)['compute-api'].identity.profile -eq 'default') 'default registry is not polluted by reliability profile'

    foreach ($topologyCase in @('schema','database','web')) {
        $registryId = "topology-$topologyCase"
        $api = New-TestIdentity compute-api default $registryId
        $worker = if($topologyCase -eq 'schema'){New-TestIdentity compute-worker default $registryId 'refinvest_integration' 'wrong_schema'}else{New-TestIdentity compute-worker default $registryId}
        $core = if($topologyCase -eq 'database'){New-TestIdentity core-api default $registryId 'wrong_database'}else{New-TestIdentity core-api default $registryId}
        $web = New-TestIdentity web default $registryId
        if($topologyCase -eq 'web'){$web.configuration.coreBaseUrl='http://localhost:9999'}
        foreach($entry in @(@('compute-api',$api),@('compute-worker',$worker),@('core-api',$core),@('web',$web))){Register-IntegrationProcess -RootPath $testRoot -Name $entry[0] -Process $current -Identity $entry[1] -RegistryId $registryId}
        $expected=@{'compute-api'=$api;'compute-worker'=$worker;'core-api'=$core;'web'=$web}
        Assert-ThrowsLike { Assert-IntegrationTopology -RootPath $testRoot -Profile default -RegistryId $registryId -ExpectedIdentities $expected -RequiredServices @('compute-api','compute-worker','core-api','web') } '*mismatch*' "$topologyCase topology mismatch"
    }

    $secretValues = @{
        INTEGRATION_POSTGRES_HOST='localhost'; INTEGRATION_POSTGRES_PORT='5432'; POSTGRES_DB='refinvest_integration'
        POSTGRES_USER='user-sentinel'; POSTGRES_PASSWORD='password-sentinel'; REFINVEST_INTERNAL_API_KEY='api-key-sentinel'
        REFINVEST_JWT_SECRET='jwt-sentinel'; REFINVEST_SNAPSHOT_STORAGE_ROOT='.integration-data\snapshots'
    }
    $secretFreeIdentity = New-ComputeIntegrationIdentity -Values $secretValues -EnvironmentFile (Join-Path $testRoot '.env.local') `
        -ComputeApiPath $integrationRoot -ServiceRole compute-api
    $serialized = $secretFreeIdentity | ConvertTo-Json -Depth 12
    foreach($secret in @('user-sentinel','password-sentinel','api-key-sentinel','jwt-sentinel','POSTGRES_PASSWORD','REFINVEST_INTERNAL_API_KEY')) {
        Assert-True ($serialized -notmatch [regex]::Escape($secret)) "metadata excludes $secret"
    }
    "PASS: $passed process/configuration identity assertions"
} finally {
    if ([IO.Path]::GetFullPath($testRoot).StartsWith([IO.Path]::GetFullPath((Join-Path $integrationRoot '.integration-data\tests')), [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

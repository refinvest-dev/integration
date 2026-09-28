[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$CoreApiPath = (Join-Path $PSScriptRoot '..\..\api\refinvest'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest'),
    [string]$WebPath = (Join-Path $PSScriptRoot '..\..\web\web'),
    [string]$Profile = 'default',
    [string]$RegistryId = 'default',
    [Alias('Schema')][string]$ComputeSchema = 'public',
    [string]$CoreSchema = 'public',
    [string]$SnapshotProfile = 'integration-e2e-v1',
    [string]$SnapshotStorageRoot,
    [switch]$Observability
)

$ErrorActionPreference = 'Stop'
$rootPath = Join-Path $PSScriptRoot '..'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
$records = Get-IntegrationProcessRecords -RootPath $rootPath -RegistryId $RegistryId
$computePort = if ($values['INTEGRATION_COMPUTE_PORT']) { [int]$values['INTEGRATION_COMPUTE_PORT'] } else { 8000 }
$coreComputeBaseUrl = if ($records.ContainsKey('compute-fault-proxy')) { $records['compute-fault-proxy'].identity.configuration.serviceBaseUrl } else { "http://127.0.0.1:$computePort" }
$expected = @{
    'compute-api' = New-ComputeIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile -ComputeApiPath $ComputeApiPath -ServiceRole compute-api -Profile $Profile -RegistryId $RegistryId -DatabaseSchema $ComputeSchema -SnapshotStorageRoot $SnapshotStorageRoot -SnapshotProfile $SnapshotProfile -Observability:$Observability
    'compute-worker' = New-ComputeIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile -ComputeApiPath $ComputeApiPath -ServiceRole compute-worker -Profile $Profile -RegistryId $RegistryId -DatabaseSchema $ComputeSchema -SnapshotStorageRoot $SnapshotStorageRoot -SnapshotProfile $SnapshotProfile -Observability:$Observability
    'core-api' = New-CoreIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile -CoreApiPath $CoreApiPath -Profile $Profile -RegistryId $RegistryId -DatabaseSchema $CoreSchema -ComputeBaseUrl $coreComputeBaseUrl -Observability:$Observability
    'web' = New-WebIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile -WebPath $WebPath -Profile $Profile -RegistryId $RegistryId
}
if ($records.ContainsKey('compute-fault-proxy')) {
    $proxyConfig = $records['compute-fault-proxy'].identity.configuration
    $expected['compute-fault-proxy'] = New-FaultProxyIntegrationIdentity -IntegrationPath $rootPath -Profile $Profile -RegistryId $RegistryId -Port ([int]$proxyConfig.listenPort) -UpstreamBaseUrl $proxyConfig.upstreamBaseUrl
}
if ($records.ContainsKey('prometheus')) {
    $promConfig = $records['prometheus'].identity.configuration
    $expected['prometheus'] = New-PrometheusIntegrationIdentity -IntegrationPath $rootPath -EnvironmentFile $EnvironmentFile -ExecutablePath $promConfig.executablePath -Profile $Profile -RegistryId $RegistryId
}
$ports = @{
    'compute-api' = if ($values['INTEGRATION_COMPUTE_PORT']) { [int]$values['INTEGRATION_COMPUTE_PORT'] } else { 8000 }
    'core-api' = if ($values['INTEGRATION_CORE_PORT']) { [int]$values['INTEGRATION_CORE_PORT'] } else { 8080 }
    'web' = if ($values['INTEGRATION_WEB_PORT']) { [int]$values['INTEGRATION_WEB_PORT'] } else { 3001 }
    'compute-fault-proxy' = 8001
    'prometheus' = 9090
}
Get-IntegrationServiceStatus -RootPath $rootPath -Profile $Profile -RegistryId $RegistryId `
    -ExpectedIdentities $expected -DefaultPorts $ports |
    Format-List service,profile,pid,pidState,port,listener,health,recordedRevision,currentRevision,database,schema,identity,nextCommand

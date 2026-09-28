[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$ContextPath = (Join-Path $PSScriptRoot '..\..\context'),
    [string]$CoreApiPath = (Join-Path $PSScriptRoot '..\..\api\refinvest'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest'),
    [string]$Profile = 'api-smoke',
    [string]$RegistryId = 'api-smoke'
)

$ErrorActionPreference = 'Stop'

& (Join-Path $PSScriptRoot 'verify-contract-sync.ps1') -ContextPath $ContextPath -CoreApiPath $CoreApiPath -ComputeApiPath $ComputeApiPath
& (Join-Path $PSScriptRoot 'initialize-compute-schema.ps1') -EnvironmentFile $EnvironmentFile -ComputeApiPath $ComputeApiPath
& (Join-Path $PSScriptRoot 'publish-compute-fixture.ps1') -EnvironmentFile $EnvironmentFile -ComputeApiPath $ComputeApiPath

Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
$computePort = if ($values['INTEGRATION_COMPUTE_PORT']) { $values['INTEGRATION_COMPUTE_PORT'] } else { '8000' }
$corePort = if ($values['INTEGRATION_CORE_PORT']) { $values['INTEGRATION_CORE_PORT'] } else { '8080' }
& (Join-Path $PSScriptRoot 'start-compute-api.ps1') -EnvironmentFile $EnvironmentFile -ComputeApiPath $ComputeApiPath -Profile $Profile -RegistryId $RegistryId
& (Join-Path $PSScriptRoot 'start-compute-worker.ps1') -EnvironmentFile $EnvironmentFile -ComputeApiPath $ComputeApiPath -Profile $Profile -RegistryId $RegistryId
& (Join-Path $PSScriptRoot 'wait-for-http.ps1') -Uri "http://127.0.0.1:$computePort/health"

& (Join-Path $PSScriptRoot 'start-core-api.ps1') -EnvironmentFile $EnvironmentFile -CoreApiPath $CoreApiPath -Profile $Profile -RegistryId $RegistryId
# A cold GitHub runner first downloads Gradle and compiles all Core modules before
# Spring can expose its health endpoint. Keep the CI limit above that one-time cost.
& (Join-Path $PSScriptRoot 'wait-for-http.ps1') -Uri "http://127.0.0.1:$corePort/actuator/health" -TimeoutSeconds 600

$accessToken = & (Join-Path $PSScriptRoot 'seed-core-member.ps1') -EnvironmentFile $EnvironmentFile -Tier PRO | Select-Object -Last 1
if ([string]::IsNullOrWhiteSpace($accessToken)) { throw 'Could not mint the API smoke Access Token.' }
& (Join-Path $PSScriptRoot 'run-api-smoke.ps1') -CoreBaseUrl "http://127.0.0.1:$corePort" -AccessToken $accessToken -TimeoutSeconds 120 `
    -EnvironmentFile $EnvironmentFile -CoreApiPath $CoreApiPath -ComputeApiPath $ComputeApiPath -Profile $Profile -RegistryId $RegistryId

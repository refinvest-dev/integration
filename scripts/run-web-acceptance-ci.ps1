[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$WebPath = (Join-Path $PSScriptRoot '..\..\web\web'),
    [string]$CoreApiPath = (Join-Path $PSScriptRoot '..\..\api\refinvest'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest'),
    [string]$Profile = 'web-acceptance',
    [string]$RegistryId = 'web-acceptance'
)

$ErrorActionPreference = 'Stop'
$root = Join-Path $PSScriptRoot '..'
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$recordsBefore = Get-IntegrationProcessRecords -RootPath $root -RegistryId $RegistryId

try {
# This retains the contract-sync, immutable-fixture, Core-to-Compute, and
# terminal-result checks before exercising the browser against that same stack.
& (Join-Path $PSScriptRoot 'run-api-smoke-ci.ps1') -EnvironmentFile $EnvironmentFile -CoreApiPath $CoreApiPath -ComputeApiPath $ComputeApiPath -Profile $Profile -RegistryId $RegistryId

& (Join-Path $PSScriptRoot 'start-web.ps1') -EnvironmentFile $EnvironmentFile -WebPath $WebPath -Profile $Profile -RegistryId $RegistryId

Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
$webPort = if ($values['INTEGRATION_WEB_PORT']) { $values['INTEGRATION_WEB_PORT'] } else { '3001' }
$corePort = if ($values['INTEGRATION_CORE_PORT']) { $values['INTEGRATION_CORE_PORT'] } else { '8080' }
& (Join-Path $PSScriptRoot 'wait-for-http.ps1') -Uri "http://localhost:$webPort/login"

Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$expected = @{
    'compute-api' = New-ComputeIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile -ComputeApiPath $ComputeApiPath -ServiceRole compute-api -Profile $Profile -RegistryId $RegistryId
    'compute-worker' = New-ComputeIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile -ComputeApiPath $ComputeApiPath -ServiceRole compute-worker -Profile $Profile -RegistryId $RegistryId
    'core-api' = New-CoreIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile -CoreApiPath $CoreApiPath -Profile $Profile -RegistryId $RegistryId
    'web' = New-WebIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile -WebPath $WebPath -Profile $Profile -RegistryId $RegistryId
}
Assert-IntegrationTopology -RootPath (Join-Path $PSScriptRoot '..') -Profile $Profile -RegistryId $RegistryId -ExpectedIdentities $expected -RequiredServices @('compute-api','compute-worker','core-api','web') | Out-Null

# Keep the browser fixture principal isolated from the API smoke principal.
$accessToken = & (Join-Path $PSScriptRoot 'seed-core-member.ps1') -EnvironmentFile $EnvironmentFile -MemberId '900000000000002' -Tier PRO | Select-Object -Last 1
if ([string]::IsNullOrWhiteSpace($accessToken)) { throw 'Could not mint the Web acceptance Access Token.' }

$pnpm = if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) { 'pnpm.cmd' } else { 'pnpm' }
try {
    # The Playwright process receives the integration-only JWT only in memory.
    # Do not print it or persist it in an artifact, trace, or test report.
    $env:REFINVEST_E2E_ACCESS_TOKEN = $accessToken
    $env:REFINVEST_E2E_CORE_URL = "http://localhost:$corePort"
    $env:REFINVEST_E2E_WEB_URL = "http://localhost:$webPort"
    & $pnpm --dir $WebPath test:e2e
    if ($LASTEXITCODE -ne 0) { throw 'Web browser acceptance failed.' }
} finally {
    Remove-Item Env:REFINVEST_E2E_ACCESS_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:REFINVEST_E2E_CORE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:REFINVEST_E2E_WEB_URL -ErrorAction SilentlyContinue
}
} finally {
    $recordsAfter = Get-IntegrationProcessRecords -RootPath $root -RegistryId $RegistryId
    $startedByWrapper = @(@('web','core-api','compute-worker','compute-api') | Where-Object {
        -not $recordsBefore.ContainsKey($_) -and $recordsAfter.ContainsKey($_)
    })
    if ($startedByWrapper.Count -gt 0) {
        try {
            Stop-IntegrationRegisteredProcess -RootPath $root -Name $startedByWrapper -RegistryId $RegistryId | Out-Host
        } catch { Write-Warning "Could not stop all Web acceptance processes started by this wrapper: $($_.Exception.Message)" }
    }
}

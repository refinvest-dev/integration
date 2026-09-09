[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$WebPath = (Join-Path $PSScriptRoot '..\..\web\web')
)

$ErrorActionPreference = 'Stop'

# This retains the contract-sync, immutable-fixture, Core-to-Compute, and
# terminal-result checks before exercising the browser against that same stack.
& (Join-Path $PSScriptRoot 'run-api-smoke-ci.ps1') -EnvironmentFile $EnvironmentFile

& (Join-Path $PSScriptRoot 'start-web.ps1') -EnvironmentFile $EnvironmentFile -WebPath $WebPath

Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
$webPort = if ($values['INTEGRATION_WEB_PORT']) { $values['INTEGRATION_WEB_PORT'] } else { '3001' }
$corePort = if ($values['INTEGRATION_CORE_PORT']) { $values['INTEGRATION_CORE_PORT'] } else { '8080' }
& (Join-Path $PSScriptRoot 'wait-for-http.ps1') -Uri "http://localhost:$webPort/login"

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

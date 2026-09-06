[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot "..\.env.local"),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot "..\..\compute-api\refinvest")
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
Set-ComputeIntegrationEnvironment $values (Join-Path $PSScriptRoot '..')

& (Get-IntegrationPython -ComputeApiPath $ComputeApiPath) -c "from refinvest.app.integration_fixture import integration_fixture_main; integration_fixture_main()"
if ($LASTEXITCODE -ne 0) { throw 'Could not publish the Compute integration fixture.' }

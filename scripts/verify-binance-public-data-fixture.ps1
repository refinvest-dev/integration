[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot "..\.env.local"),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot "..\..\compute-api\refinvest"),
    [string]$Schema = "integration_binance_fixture"
)

$ErrorActionPreference = "Stop"

Import-Module (Join-Path $PSScriptRoot "IntegrationEnvironment.psm1") -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
& (Join-Path $PSScriptRoot "initialize-compute-schema.ps1") -EnvironmentFile $EnvironmentFile -ComputeApiPath $ComputeApiPath -Schema $Schema
$fixtureValues = @{}
foreach ($key in $values.Keys) { $fixtureValues[$key] = $values[$key] }
$fixtureValues["REFINVEST_SNAPSHOT_STORAGE_ROOT"] = ".integration-data/binance-fixture-snapshots"
Set-ComputeIntegrationEnvironment $fixtureValues (Join-Path $PSScriptRoot "..") -DatabaseSchema $Schema

& (Get-IntegrationPython -ComputeApiPath $ComputeApiPath) (Join-Path $PSScriptRoot "verify-binance-public-data-fixture.py")
if ($LASTEXITCODE -ne 0) { throw "Binance Public Data fixture verification failed." }

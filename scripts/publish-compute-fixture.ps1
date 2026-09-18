[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot "..\.env.local"),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot "..\..\compute-api\refinvest"),
    [string]$Schema,
    [string]$SnapshotStorageRoot
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
Set-ComputeIntegrationEnvironment $values (Join-Path $PSScriptRoot '..') $Schema
if (-not [string]::IsNullOrWhiteSpace($SnapshotStorageRoot)) {
    $env:REFINVEST_SNAPSHOT_STORAGE_ROOT = Resolve-IntegrationWorkspacePath `
        (Join-Path $PSScriptRoot '..') $SnapshotStorageRoot
}

& (Get-IntegrationPython -ComputeApiPath $ComputeApiPath) -c "from refinvest.app.integration_fixture import integration_fixture_main; integration_fixture_main()"
if ($LASTEXITCODE -ne 0) { throw 'Could not publish the Compute integration fixture.' }

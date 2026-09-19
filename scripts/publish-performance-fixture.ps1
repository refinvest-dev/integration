[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot "..\.env.local"),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot "..\..\compute-api\refinvest"),
    [string]$Schema,
    [string]$SnapshotStorageRoot = ".integration-data\performance-snapshots"
)

$ErrorActionPreference = "Stop"
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$compute = [IO.Path]::GetFullPath($ComputeApiPath)
if (-not (Test-Path -LiteralPath (Join-Path $compute 'pyproject.toml'))) {
    throw "Compute checkout does not exist: $compute"
}

Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
if ($values['POSTGRES_DB'] -cne 'refinvest_integration') {
    throw 'Performance fixture publication is allowed only in the refinvest_integration database.'
}

& (Join-Path $PSScriptRoot 'initialize-compute-schema.ps1') `
    -EnvironmentFile $EnvironmentFile `
    -ComputeApiPath $compute `
    -Schema $Schema
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize the Compute integration schema.' }

Set-ComputeIntegrationEnvironment $values $workspace $Schema
$storageRoot = Resolve-IntegrationWorkspacePath $workspace $SnapshotStorageRoot
$env:REFINVEST_SNAPSHOT_STORAGE_ROOT = $storageRoot
$python = Get-IntegrationPython -ComputeApiPath $compute

& $python (Join-Path $PSScriptRoot 'publish-performance-fixture.py') `
    --definition (Join-Path $workspace 'performance\fixture-definition.json') `
    --fixture-manifest (Join-Path $workspace 'performance\fixture-manifest.json') `
    --storage-root $storageRoot `
    --compute-api-path $compute `
    --publish
if ($LASTEXITCODE -ne 0) { throw 'Could not publish the performance fixture.' }

& $python (Join-Path $PSScriptRoot 'verify-performance-fixture.py') `
    --definition (Join-Path $workspace 'performance\fixture-definition.json') `
    --fixture-manifest (Join-Path $workspace 'performance\fixture-manifest.json') `
    --scenario-directory (Join-Path $workspace 'performance\scenarios') `
    --snapshot-path (Join-Path $storageRoot 'performance-mvp-daily-v1') `
    --compute-api-path $compute
if ($LASTEXITCODE -ne 0) { throw 'Published performance fixture verification failed.' }

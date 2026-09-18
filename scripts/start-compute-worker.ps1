[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest'),
    [string]$Schema,
    [string]$SnapshotStorageRoot
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
Set-ComputeIntegrationEnvironment $values (Join-Path $PSScriptRoot '..') $Schema
if (-not [string]::IsNullOrWhiteSpace($SnapshotStorageRoot)) {
    $env:REFINVEST_SNAPSHOT_STORAGE_ROOT = Resolve-IntegrationWorkspacePath `
        (Join-Path $PSScriptRoot '..') $SnapshotStorageRoot
}
$env:REFINVEST_WORKER_ID = 'integration-e2e-worker'
$logDir = Join-Path $PSScriptRoot '..\.integration-data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$startParameters = @{
    FilePath = Get-IntegrationPython -ComputeApiPath $ComputeApiPath
    ArgumentList = '-c "from refinvest.app.worker import worker_main; worker_main()"'
    WorkingDirectory = $ComputeApiPath
    RedirectStandardOutput = Join-Path $logDir 'compute-worker.out.log'
    RedirectStandardError = Join-Path $logDir 'compute-worker.err.log'
    PassThru = $true
}
if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) { $startParameters.WindowStyle = 'Hidden' }
Normalize-IntegrationProcessPathEnvironment
$process = Start-Process @startParameters
Register-IntegrationProcess -RootPath (Join-Path $PSScriptRoot '..') -Name 'compute-worker' -Process $process -LogPath (Join-Path $logDir 'compute-worker.out.log')
[pscustomobject]@{ processId = $process.Id; log = (Join-Path $logDir 'compute-worker.out.log') }

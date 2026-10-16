[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest'),
    [string]$Schema,
    [string]$SnapshotStorageRoot,
    [string]$SnapshotProfile = 'integration-e2e-v1',
    [string]$Profile = 'default',
    [string]$RegistryId = 'default',
    [switch]$TradingReview,
    [switch]$Observability
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
$schemaName = if ([string]::IsNullOrWhiteSpace($Schema)) { 'public' } else { $Schema }
$identity = New-ComputeIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile `
    -ComputeApiPath $ComputeApiPath -ServiceRole compute-worker -Profile $Profile -RegistryId $RegistryId `
    -DatabaseSchema $schemaName -SnapshotStorageRoot $SnapshotStorageRoot `
    -SnapshotProfile $SnapshotProfile -Observability:$Observability
$allowed = Assert-IntegrationServiceStartAllowed -RootPath (Join-Path $PSScriptRoot '..') `
    -Name 'compute-worker' -ExpectedIdentity $identity -RegistryId $RegistryId
if ($allowed.reuse) {
    [pscustomobject]@{ processId=$allowed.record.servicePid; log=$allowed.record.logPath; reused=$true; profile=$Profile }
    return
}
Set-ComputeIntegrationEnvironment $values (Join-Path $PSScriptRoot '..') $Schema
if ($Observability) { $env:REFINVEST_WORKER_METRICS_ENABLED = 'true'; $env:REFINVEST_WORKER_METRICS_BIND = '127.0.0.1'; $env:REFINVEST_WORKER_METRICS_PORT = '9102' }
if (-not [string]::IsNullOrWhiteSpace($SnapshotStorageRoot)) {
    $env:REFINVEST_SNAPSHOT_STORAGE_ROOT = Resolve-IntegrationWorkspacePath `
        (Join-Path $PSScriptRoot '..') $SnapshotStorageRoot
}
$env:REFINVEST_WORKER_ID = 'integration-e2e-worker'
$logDir = Join-Path $PSScriptRoot '..\.integration-data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$startParameters = @{
    FilePath = Get-IntegrationPython -ComputeApiPath $ComputeApiPath
    ArgumentList = if ($TradingReview) {
        '-c "from refinvest.app.trading_review_worker import trading_review_worker_main; trading_review_worker_main()"'
    } else { '-c "from refinvest.app.worker import worker_main; worker_main()"' }
    WorkingDirectory = $ComputeApiPath
    RedirectStandardOutput = Join-Path $logDir "$RegistryId-compute-worker.out.log"
    RedirectStandardError = Join-Path $logDir "$RegistryId-compute-worker.err.log"
    PassThru = $true
}
if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) { $startParameters.WindowStyle = 'Hidden' }
Normalize-IntegrationProcessPathEnvironment
$process = Start-Process @startParameters
Register-IntegrationProcess -RootPath (Join-Path $PSScriptRoot '..') -Name 'compute-worker' -Process $process `
    -Identity $identity -RegistryId $RegistryId -LogPath (Join-Path $logDir "$RegistryId-compute-worker.out.log")
[pscustomobject]@{ processId = $process.Id; log = (Join-Path $logDir "$RegistryId-compute-worker.out.log"); reused=$false; profile=$Profile }

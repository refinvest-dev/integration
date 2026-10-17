[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest'),
    [string]$Schema,
    [string]$SnapshotStorageRoot,
    [string]$SnapshotProfile = 'integration-e2e-v1',
    [string]$Profile = 'default',
    [string]$RegistryId = 'default',
    [switch]$Observability
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
$schemaName = if ([string]::IsNullOrWhiteSpace($Schema)) { 'public' } else { $Schema }
$identity = New-ComputeIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile `
    -ComputeApiPath $ComputeApiPath -ServiceRole compute-api -Profile $Profile -RegistryId $RegistryId `
    -DatabaseSchema $schemaName -SnapshotStorageRoot $SnapshotStorageRoot `
    -SnapshotProfile $SnapshotProfile -Observability:$Observability
$port = if ($values['INTEGRATION_COMPUTE_PORT']) { [int]$values['INTEGRATION_COMPUTE_PORT'] } else { 8000 }
$allowed = Assert-IntegrationServiceStartAllowed -RootPath (Join-Path $PSScriptRoot '..') `
    -Name 'compute-api' -ExpectedIdentity $identity -RegistryId $RegistryId -Port $port
if ($allowed.reuse) {
    [pscustomobject]@{ processId=$allowed.record.servicePid; healthUrl=$allowed.record.healthUrl; reused=$true; profile=$Profile }
    return
}
Set-ComputeIntegrationEnvironment $values (Join-Path $PSScriptRoot '..') $Schema
if ($Observability) { $env:REFINVEST_API_METRICS_ENABLED = 'true'; $env:REFINVEST_API_METRICS_BIND = '127.0.0.1'; $env:REFINVEST_API_METRICS_PORT = '9101' }
if (-not [string]::IsNullOrWhiteSpace($SnapshotStorageRoot)) {
    $env:REFINVEST_SNAPSHOT_STORAGE_ROOT = Resolve-IntegrationWorkspacePath `
        (Join-Path $PSScriptRoot '..') $SnapshotStorageRoot
}
$logDir = Join-Path $PSScriptRoot '..\.integration-data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$startParameters = @{
    FilePath = Get-IntegrationPython -ComputeApiPath $ComputeApiPath
    ArgumentList = "-m uvicorn refinvest.app.main:app --host 127.0.0.1 --port $port"
    WorkingDirectory = $ComputeApiPath
    RedirectStandardOutput = Join-Path $logDir "$RegistryId-compute-api.out.log"
    RedirectStandardError = Join-Path $logDir "$RegistryId-compute-api.err.log"
    PassThru = $true
}
if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
    # Windows' default Proactor loop can fail creating its internal socketpair
    # (WinError 10014) when launched through Start-Process on this host.
    # Uvicorn 0.36+ selects Proactor explicitly for loop=auto even after a
    # Selector policy is installed. loop=none lets asyncio.Runner use that policy.
    $pythonCode = "import asyncio; asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()); import uvicorn; uvicorn.run('refinvest.app.main:app', host='127.0.0.1', port=$port, loop='none')"
    $startParameters.ArgumentList = @('-c', ('"' + $pythonCode + '"'))
    $startParameters.WindowStyle = 'Hidden'
}
Normalize-IntegrationProcessPathEnvironment
$process = Start-Process @startParameters
Register-IntegrationProcess -RootPath (Join-Path $PSScriptRoot '..') -Name 'compute-api' -Process $process `
    -Identity $identity -RegistryId $RegistryId -Port $port -HealthUrl "http://127.0.0.1:$port/health" `
    -LogPath (Join-Path $logDir "$RegistryId-compute-api.out.log")
[pscustomobject]@{ processId = $process.Id; healthUrl = "http://127.0.0.1:$port/health"; reused=$false; profile=$Profile }

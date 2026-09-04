[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest')
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
Set-ComputeIntegrationEnvironment $values (Join-Path $PSScriptRoot '..')
$env:REFINVEST_WORKER_ID = 'integration-e2e-worker'
$logDir = Join-Path $PSScriptRoot '..\.integration-data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$process = Start-Process -FilePath (Join-Path $ComputeApiPath '.venv\Scripts\python.exe') -ArgumentList '-c "from refinvest.app.worker import worker_main; worker_main()"' -WorkingDirectory $ComputeApiPath -RedirectStandardOutput (Join-Path $logDir 'compute-worker.out.log') -RedirectStandardError (Join-Path $logDir 'compute-worker.err.log') -WindowStyle Hidden -PassThru
[pscustomobject]@{ processId = $process.Id; log = (Join-Path $logDir 'compute-worker.out.log') }

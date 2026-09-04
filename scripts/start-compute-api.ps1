[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest')
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
Set-ComputeIntegrationEnvironment $values (Join-Path $PSScriptRoot '..')
$port = if ($values['INTEGRATION_COMPUTE_PORT']) { $values['INTEGRATION_COMPUTE_PORT'] } else { '8000' }
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "Port $port is already in use." }

$logDir = Join-Path $PSScriptRoot '..\.integration-data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$process = Start-Process -FilePath (Join-Path $ComputeApiPath '.venv\Scripts\python.exe') -ArgumentList "-m uvicorn refinvest.app.main:app --host 127.0.0.1 --port $port" -WorkingDirectory $ComputeApiPath -RedirectStandardOutput (Join-Path $logDir 'compute-api.out.log') -RedirectStandardError (Join-Path $logDir 'compute-api.err.log') -WindowStyle Hidden -PassThru
[pscustomobject]@{ processId = $process.Id; healthUrl = "http://127.0.0.1:$port/health" }

[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest')
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
Set-ComputeIntegrationEnvironment $values (Join-Path $PSScriptRoot '..')
$port = if ($values['INTEGRATION_COMPUTE_PORT']) { $values['INTEGRATION_COMPUTE_PORT'] } else { '8000' }
if (Test-IntegrationTcpListener -Port $port) { throw "Port $port is already in use." }

$logDir = Join-Path $PSScriptRoot '..\.integration-data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$startParameters = @{
    FilePath = Get-IntegrationPython -ComputeApiPath $ComputeApiPath
    ArgumentList = "-m uvicorn refinvest.app.main:app --host 127.0.0.1 --port $port"
    WorkingDirectory = $ComputeApiPath
    RedirectStandardOutput = Join-Path $logDir 'compute-api.out.log'
    RedirectStandardError = Join-Path $logDir 'compute-api.err.log'
    PassThru = $true
}
if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) { $startParameters.WindowStyle = 'Hidden' }
$process = Start-Process @startParameters
Register-IntegrationProcess -RootPath (Join-Path $PSScriptRoot '..') -Name 'compute-api' -Process $process -Port $port -HealthUrl "http://127.0.0.1:$port/health" -LogPath (Join-Path $logDir 'compute-api.out.log')
[pscustomobject]@{ processId = $process.Id; healthUrl = "http://127.0.0.1:$port/health" }

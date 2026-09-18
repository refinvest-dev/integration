[CmdletBinding()]
param(
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest'),
    [int]$Port = 8001,
    [string]$UpstreamBaseUrl = 'http://127.0.0.1:8000'
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
if (Test-IntegrationTcpListener -Port $Port) { throw "Port $Port is already in use." }

$logDir = Join-Path $PSScriptRoot '..\.integration-data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$startParameters = @{
    FilePath = Get-IntegrationPython -ComputeApiPath $ComputeApiPath
    ArgumentList = @(
        (Join-Path $PSScriptRoot 'compute-response-loss-proxy.py'),
        '--port', [string]$Port,
        '--upstream', $UpstreamBaseUrl
    )
    WorkingDirectory = (Join-Path $PSScriptRoot '..')
    RedirectStandardOutput = Join-Path $logDir 'compute-fault-proxy.out.log'
    RedirectStandardError = Join-Path $logDir 'compute-fault-proxy.err.log'
    PassThru = $true
}
if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
    $startParameters.WindowStyle = 'Hidden'
}
Normalize-IntegrationProcessPathEnvironment
$process = Start-Process @startParameters
Register-IntegrationProcess `
    -RootPath (Join-Path $PSScriptRoot '..') `
    -Name 'compute-fault-proxy' `
    -Process $process `
    -Port $Port `
    -HealthUrl "http://127.0.0.1:$Port/__faults__/status" `
    -LogPath (Join-Path $logDir 'compute-fault-proxy.out.log')
[pscustomobject]@{ processId = $process.Id; statusUrl = "http://127.0.0.1:$Port/__faults__/status" }

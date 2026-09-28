[CmdletBinding()]
param(
    [string]$ComputeApiPath = (Join-Path $PSScriptRoot '..\..\compute-api\refinvest'),
    [int]$Port = 8001,
    [string]$UpstreamBaseUrl = 'http://127.0.0.1:8000',
    [string]$Profile = 'default',
    [string]$RegistryId = 'default'
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$root = Join-Path $PSScriptRoot '..'
$identity = New-FaultProxyIntegrationIdentity -IntegrationPath $root -Profile $Profile `
    -RegistryId $RegistryId -Port $Port -UpstreamBaseUrl $UpstreamBaseUrl
$allowed = Assert-IntegrationServiceStartAllowed -RootPath $root -Name 'compute-fault-proxy' `
    -ExpectedIdentity $identity -RegistryId $RegistryId -Port $Port
if ($allowed.reuse) {
    [pscustomobject]@{ processId=$allowed.record.servicePid; statusUrl=$allowed.record.healthUrl; reused=$true; profile=$Profile }
    return
}

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
    RedirectStandardOutput = Join-Path $logDir "$RegistryId-compute-fault-proxy.out.log"
    RedirectStandardError = Join-Path $logDir "$RegistryId-compute-fault-proxy.err.log"
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
    -Identity $identity `
    -RegistryId $RegistryId `
    -Port $Port `
    -HealthUrl "http://127.0.0.1:$Port/__faults__/status" `
    -LogPath (Join-Path $logDir "$RegistryId-compute-fault-proxy.out.log")
[pscustomobject]@{ processId = $process.Id; statusUrl = "http://127.0.0.1:$Port/__faults__/status"; reused=$false; profile=$Profile }

[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$WebPath = (Join-Path $PSScriptRoot '..\..\web\web')
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
$port = if ($values['INTEGRATION_WEB_PORT']) { $values['INTEGRATION_WEB_PORT'] } else { '3001' }
if (Test-IntegrationTcpListener -Port $port) { throw "Port $port is already in use." }
if (-not (Test-Path -LiteralPath (Join-Path $WebPath 'package.json'))) { throw "Web checkout does not exist: $WebPath" }

$corePort = if ($values['INTEGRATION_CORE_PORT']) { $values['INTEGRATION_CORE_PORT'] } else { '8080' }
$env:NEXT_PUBLIC_API_BASE_URL = if ($values['NEXT_PUBLIC_API_BASE_URL']) {
    $values['NEXT_PUBLIC_API_BASE_URL']
} else {
    "http://localhost:$corePort"
}
if ($env:NEXT_PUBLIC_API_BASE_URL -notmatch '^http://localhost(:\d+)?$') {
    throw 'NEXT_PUBLIC_API_BASE_URL must use localhost for the Lax SameSite integration-cookie setup.'
}
$expectedWebOrigin = "http://localhost:$port"
if ($values['REFINVEST_WEB_ORIGIN'] -and $values['REFINVEST_WEB_ORIGIN'].TrimEnd('/') -ne $expectedWebOrigin) {
    throw "REFINVEST_WEB_ORIGIN must be $expectedWebOrigin to match the local Web origin."
}

$logDir = Join-Path $PSScriptRoot '..\.integration-data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$pnpm = if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) { 'pnpm.cmd' } else { 'pnpm' }
$process = Start-Process -FilePath $pnpm -ArgumentList @('dev', '--port', $port) -WorkingDirectory $WebPath -RedirectStandardOutput (Join-Path $logDir 'web.out.log') -RedirectStandardError (Join-Path $logDir 'web.err.log') -WindowStyle Hidden -PassThru
Register-IntegrationProcess -RootPath (Join-Path $PSScriptRoot '..') -Name 'web' -Process $process -Port $port -HealthUrl "http://localhost:$port/login" -LogPath (Join-Path $logDir 'web.out.log')
[pscustomobject]@{ processId = $process.Id; webUrl = "http://localhost:$port"; apiBaseUrl = $env:NEXT_PUBLIC_API_BASE_URL }

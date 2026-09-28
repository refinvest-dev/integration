[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$WebPath = (Join-Path $PSScriptRoot '..\..\web\web'),
    [string]$Profile = 'default',
    [string]$RegistryId = 'default'
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
$port = if ($values['INTEGRATION_WEB_PORT']) { [int]$values['INTEGRATION_WEB_PORT'] } else { 3001 }
if (-not (Test-Path -LiteralPath (Join-Path $WebPath 'package.json'))) { throw "Web checkout does not exist: $WebPath" }
$identity = New-WebIntegrationIdentity -Values $values -EnvironmentFile $EnvironmentFile -WebPath $WebPath `
    -Profile $Profile -RegistryId $RegistryId
$allowed = Assert-IntegrationServiceStartAllowed -RootPath (Join-Path $PSScriptRoot '..') `
    -Name 'web' -ExpectedIdentity $identity -RegistryId $RegistryId -Port $port
if ($allowed.reuse) {
    [pscustomobject]@{ processId=$allowed.record.servicePid; webUrl=$allowed.record.identity.configuration.serviceBaseUrl; apiBaseUrl=$allowed.record.identity.configuration.coreBaseUrl; reused=$true; profile=$Profile }
    return
}

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
$startParameters = @{
    FilePath = $pnpm
    ArgumentList = @('dev', '--port', $port)
    WorkingDirectory = $WebPath
    RedirectStandardOutput = Join-Path $logDir "$RegistryId-web.out.log"
    RedirectStandardError = Join-Path $logDir "$RegistryId-web.err.log"
    PassThru = $true
}
if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
    $startParameters.WindowStyle = 'Hidden'
}
$process = Start-Process @startParameters
Register-IntegrationProcess -RootPath (Join-Path $PSScriptRoot '..') -Name 'web' -Process $process `
    -Identity $identity -RegistryId $RegistryId -Port $port -HealthUrl "http://localhost:$port/login" `
    -LogPath (Join-Path $logDir "$RegistryId-web.out.log")
[pscustomobject]@{ processId = $process.Id; webUrl = "http://localhost:$port"; apiBaseUrl = $env:NEXT_PUBLIC_API_BASE_URL; reused=$false; profile=$Profile }

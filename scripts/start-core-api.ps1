[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$CoreApiPath = (Join-Path $PSScriptRoot '..\..\api\refinvest'),
    [string]$ComputeBaseUrl,
    [string]$DatabaseSchema,
    [switch]$Observability
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
Set-CoreIntegrationEnvironment $values
if ($Observability) {
    Assert-IntegrationEnvironment $values @('REFINVEST_METRICS_SCRAPE_PASSWORD')
    $env:REFINVEST_METRICS_SCRAPE_PASSWORD = $values['REFINVEST_METRICS_SCRAPE_PASSWORD']
    $env:SPRING_PROFILES_ACTIVE = if ($env:SPRING_PROFILES_ACTIVE) { "$($env:SPRING_PROFILES_ACTIVE),dev-observability" } else { 'dev-observability' }
    $env:SERVER_ADDRESS = '127.0.0.1'
}
if (-not [string]::IsNullOrWhiteSpace($ComputeBaseUrl)) {
    $env:COMPUTE_API_BASE_URL = $ComputeBaseUrl
}
if (-not [string]::IsNullOrWhiteSpace($DatabaseSchema)) {
    if ($DatabaseSchema -notmatch '^[a-z][a-z0-9_]*$') {
        throw 'Core integration database schema must be a lowercase PostgreSQL identifier.'
    }
    if ($values['POSTGRES_DB'] -cne 'refinvest_integration') {
        throw 'Core reliability schema is allowed only inside refinvest_integration.'
    }
    $env:DATABASE_NAME = "$($values['POSTGRES_DB'])?currentSchema=$DatabaseSchema"
}
$port = if ($values['INTEGRATION_CORE_PORT']) { $values['INTEGRATION_CORE_PORT'] } else { '8080' }
if (Test-IntegrationTcpListener -Port $port) { throw "Port $port is already in use." }

$logDir = Join-Path $PSScriptRoot '..\.integration-data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$gradleUserHome = Join-Path $PSScriptRoot '..\.integration-data\gradle'
New-Item -ItemType Directory -Force -Path $gradleUserHome | Out-Null
$env:GRADLE_USER_HOME = [IO.Path]::GetFullPath($gradleUserHome)
$gradleWrapper = Get-IntegrationGradleWrapper -CoreApiPath $CoreApiPath
$gradleArguments = @(':app:bootRun', '--no-daemon', '--console=plain', "--args=--server.port=$port")
$startParameters = @{
    FilePath = $gradleWrapper
    ArgumentList = $gradleArguments
    WorkingDirectory = $CoreApiPath
    RedirectStandardOutput = Join-Path $logDir 'core-api.out.log'
    RedirectStandardError = Join-Path $logDir 'core-api.err.log'
    PassThru = $true
}
$runningOnWindows = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
if ($runningOnWindows) {
    $startParameters.WindowStyle = 'Hidden'
} else {
    $startParameters.FilePath = (Get-Command bash -ErrorAction Stop).Source
    $startParameters.ArgumentList = @($gradleWrapper) + $gradleArguments
}
Normalize-IntegrationProcessPathEnvironment
$process = Start-Process @startParameters
Register-IntegrationProcess -RootPath (Join-Path $PSScriptRoot '..') -Name 'core-api' -Process $process -Port $port -HealthUrl "http://127.0.0.1:$port/actuator/health" -LogPath (Join-Path $logDir 'core-api.out.log')
[pscustomobject]@{ processId = $process.Id; healthUrl = "http://127.0.0.1:$port/actuator/health" }

[CmdletBinding()]
param(
    [string]$EnvironmentFile = (Join-Path $PSScriptRoot '..\.env.local'),
    [string]$CoreApiPath = (Join-Path $PSScriptRoot '..\..\api\refinvest')
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'IntegrationProcess.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
Set-CoreIntegrationEnvironment $values
$port = if ($values['INTEGRATION_CORE_PORT']) { $values['INTEGRATION_CORE_PORT'] } else { '8080' }
if (Test-IntegrationTcpListener -Port $port) { throw "Port $port is already in use." }

$logDir = Join-Path $PSScriptRoot '..\.integration-data\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
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
$isWindows = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
if ($isWindows) {
    $startParameters.WindowStyle = 'Hidden'
} else {
    $startParameters.FilePath = (Get-Command bash -ErrorAction Stop).Source
    $startParameters.ArgumentList = @($gradleWrapper) + $gradleArguments
}
$process = Start-Process @startParameters
Register-IntegrationProcess -RootPath (Join-Path $PSScriptRoot '..') -Name 'core-api' -Process $process -Port $port -HealthUrl "http://127.0.0.1:$port/actuator/health" -LogPath (Join-Path $logDir 'core-api.out.log')
[pscustomobject]@{ processId = $process.Id; healthUrl = "http://127.0.0.1:$port/actuator/health" }

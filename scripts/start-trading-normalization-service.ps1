[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [string]$CorePath,
    [string]$ComputePath,
    [string]$ComputeBaseUrl,
    [ValidateSet('core-api','compute-api')][string]$Service = 'core-api',
    [Parameter(Mandatory)][string]$RegistryId,
    [Parameter(Mandatory)][string]$EvidenceDirectory,
    [string]$DatabaseSchema
)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'IntegrationEnvironment.psm1') -Force
$values = Get-IntegrationEnvironment $EnvironmentFile
if ($Service -eq 'core-api') {
    $uri = "http://127.0.0.1:$($values.INTEGRATION_CORE_PORT)/actuator/health"
    $arguments = @{ EnvironmentFile=$EnvironmentFile; CoreApiPath=$CorePath; ComputeBaseUrl=$ComputeBaseUrl
                    RegistryId=$RegistryId; Profile=$RegistryId }
    if ($env:TRADING_RUNTIME_CORE_JAR) { $arguments.JarPath = $env:TRADING_RUNTIME_CORE_JAR }
    if ($DatabaseSchema) { $arguments.DatabaseSchema = $DatabaseSchema }
    $startScript = 'start-core-api.ps1'
} else {
    $uri = "http://127.0.0.1:$($values.INTEGRATION_COMPUTE_PORT)/health"
    $arguments = @{ EnvironmentFile=$EnvironmentFile; ComputeApiPath=$ComputePath; Schema=$DatabaseSchema
                    RegistryId=$RegistryId; Profile=$RegistryId }
    $startScript = 'start-compute-api.ps1'
}
$logRoot = Join-Path $PSScriptRoot '../.integration-data/logs'
$observations = @()
for ($attempt = 1; $attempt -le 3; $attempt++) {
    $started = & (Join-Path $PSScriptRoot $startScript) @arguments
    $deadline = (Get-Date).AddSeconds(600)
    $ready = $false
    do {
        try {
            $response = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 3
            $ready = $response.StatusCode -eq 200
        } catch { }
        if ($ready -or -not (Get-Process -Id $started.processId -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    if ($ready) {
        $observations += @{ attempt=$attempt; result='READY' }
        $observations | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $EvidenceDirectory "$Service-startup.json") -Encoding UTF8
        Write-Host "Ready: $uri (200)"
        return
    }
    $stdout = Join-Path $logRoot "$RegistryId-$Service.out.log"
    $stderr = Join-Path $logRoot "$RegistryId-$Service.err.log"
    $logText = @($stdout,$stderr) | Where-Object { Test-Path -LiteralPath $_ } | ForEach-Object { Get-Content -LiteralPath $_ -Raw }
    $knownSocketFailure = ($logText -join "`n") -match 'java.net.SocketException: Bad address: listen|WinError 10014'
    $observations += @{ attempt=$attempt; result=if ($knownSocketFailure) { 'WINDOWS_SOCKET_LISTEN_FAILED' } else { 'STARTUP_FAILED' } }
    foreach ($suffix in @('out','err')) {
        $source = Join-Path $logRoot "$RegistryId-$Service.$suffix.log"
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $EvidenceDirectory "$Service-startup-$attempt.$suffix.log")
        }
    }
    $observations | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $EvidenceDirectory "$Service-startup.json") -Encoding UTF8
    & (Join-Path $PSScriptRoot 'stop-integration.ps1') -RegistryId $RegistryId -Service $Service | Out-Null
    if (-not $knownSocketFailure -or $attempt -eq 3) { throw "Owned $Service startup failed; see startup evidence and preserved logs." }
    Write-Host "Retrying owned $Service startup after Windows socket listen failure ($attempt/3)."
}
